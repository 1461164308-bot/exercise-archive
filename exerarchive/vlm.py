# -*- coding: utf-8 -*-
"""模型通道：本地轻量化视觉模型 + 云端强力模型，统一成一套调用接口。

支持的后端
----------
- ``ollama``            ：本地 Ollama（默认，``/api/chat``，支持图片）
- ``lmstudio``          ：本地 LM Studio（OpenAI 兼容，``/v1/chat/completions``）
- ``openai_compatible`` ：任意 OpenAI 兼容服务（云端 DeepSeek / 通义 / 智谱 / Kimi / OneAPI …）

关键实现细节（都是踩过的坑）
--------------------------
1. **思考链**：qwen3-vl 这类模型即使传 ``think=false`` 也可能把正式答案写进
   ``message.thinking`` 而 ``message.content`` 为空。所以 :meth:`ModelClient.chat`
   统一取「content 为空就用 thinking」，再交给 :func:`extract_json` 解析。
2. **JSON 容错**：小模型经常输出 ```json 代码块、前后废话、尾逗号。:func:`extract_json`
   先用括号配对截出 JSON，再逐级尝试修复。
3. **自动拉起服务**：本地模型服务没开时，自动执行 ``ollama serve`` / ``lms server start``
   （可在设置里关掉），并轮询等待就绪，小白用户只管点「开始识别」。
4. **纯文本模型**：云端若是不支持视觉的模型（如 deepseek-chat），只发送本地识别出的
   文字，不发送图片（图片在本地解析，隐私更好）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .logging_setup import get_logger

log = get_logger("vlm")

# --------------------------------------------------------------------------- #
# JSON 解析容错
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _first_json_span(text: str, open_ch: str = "{", close_ch: str = "}") -> Optional[str]:
    """用括号配对找出第一个完整的 JSON 对象/数组（跳过字符串里的括号）。"""
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]  # 截断的 JSON，交给修复逻辑补全


def _repair_json(s: str) -> str:
    """常见小模型 JSON 毛病的兜底修复。"""
    out = s.strip()
    out = out.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    out = re.sub(r",\s*([}\]])", r"\1", out)          # 尾逗号
    out = re.sub(r"//[^\n]*", "", out)                 # // 注释
    out = re.sub(r"/\*.*?\*/", "", out, flags=re.S)    # /* 注释 */
    out = re.sub(r"\bNone\b", "null", out)
    out = re.sub(r"\bTrue\b", "true", out)
    out = re.sub(r"\bFalse\b", "false", out)
    return out


def _balance(s: str) -> str:
    """补全被截断的引号/括号（模型输出到一半被 max_tokens 截断时很常见）。"""
    if not s:
        return s
    # 去掉最后一个不完整的键值（以逗号结尾的残缺片段）
    s = re.sub(r",\s*\"[^\"{}]*$", "", s)
    s = re.sub(r",\s*$", "", s)
    quotes = s.count('"') - len(re.findall(r'\\"', s))
    if quotes % 2 == 1:
        s += '"'
    opens = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            opens.append(ch)
        elif ch in "}]" and opens:
            opens.pop()
    for ch in reversed(opens):
        s += "}" if ch == "{" else "]"
    return s


def extract_json(text: str, default=None):
    """从模型输出里抠出 JSON 对象（容忍代码块/废话/尾逗号/截断）。"""
    if not text:
        return default
    candidates: List[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    obj = _first_json_span(text, "{", "}")
    if obj:
        candidates.append(obj)
    arr = _first_json_span(text, "[", "]")
    if arr:
        candidates.append(arr)
    candidates.append(text)
    for cand in candidates:
        for attempt in (cand, _repair_json(cand), _balance(_repair_json(cand))):
            try:
                return json.loads(attempt)
            except Exception:
                continue
    log.debug("JSON 解析失败，原文前 500 字：%s", text[:500])
    return default


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class ChatResult:
    """一次模型调用的结果。"""

    text: str = ""
    engine: str = ""              # 例如 "local:qwen3-vl:4b" / "cloud:deepseek-chat"
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)
    used_images: int = 0
    thinking: str = ""

    @property
    def tokens_per_sec(self) -> float:
        return (self.output_tokens / self.elapsed) if self.elapsed > 0.05 and self.output_tokens else 0.0

    def json(self, default=None):
        return extract_json(self.text, default)


class ModelError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# HTTP 小工具（只用标准库）
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict, timeout: float, headers: Optional[dict] = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception as exc:
        raise ModelError(f"服务返回的不是 JSON：{raw[:200]}") from exc


def _get_json(url: str, timeout: float, headers: Optional[dict] = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_base(base_url: str):
    """把 http://host:port/xxx 拆成 (origin, path)。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        b = "http://127.0.0.1:11434"
    m = re.match(r"^(https?://[^/]+)(/.*)?$", b)
    if not m:
        return "http://127.0.0.1:11434", ""
    origin = m.group(1)
    path = (m.group(2) or "").rstrip("/")
    return origin, path


def _host_port(base_url: str):
    origin, _ = _split_base(base_url)
    m = re.match(r"^https?://([^:/]+)(?::(\d+))?$", origin)
    if not m:
        return "127.0.0.1", 11434
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else (443 if origin.startswith("https") else 80)
    return host, port


# --------------------------------------------------------------------------- #
# 本地模型服务管理
# --------------------------------------------------------------------------- #
class LocalServerManager:
    """负责「本地模型服务没开就自动拉起来」以及模型列表查询。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._starting = False
        self._lock = threading.Lock()

    # -- 状态 -------------------------------------------------------------- #
    def alive(self) -> bool:
        kind = str(self.cfg.get("local.kind", "ollama")).lower()
        base = str(self.cfg.get("local.base_url", ""))
        host, port = _host_port(base)
        if not _port_open(host, port):
            return False
        try:
            if kind == "ollama":
                _get_json(f"{_split_base(base)[0]}/api/version", 4)
            else:
                _get_json(self._openai_base() + "/models", 4, self._auth_headers())
            return True
        except Exception:
            return False

    def _openai_base(self) -> str:
        origin, path = _split_base(str(self.cfg.get("local.base_url", "")))
        if path.endswith("/v1"):
            return origin + path
        return origin + "/v1"

    def _auth_headers(self) -> dict:
        key = str(self.cfg.get("local.api_key", "") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    # -- 启动 -------------------------------------------------------------- #
    def ensure_running(self, wait_seconds: int = 60, on_log: Optional[Callable[[str], None]] = None) -> bool:
        """确保本地服务可用；返回是否可用。"""
        say = on_log or (lambda m: log.info("%s", m))
        if self.alive():
            return True
        if not bool(self.cfg.get("local.auto_start", True)):
            say("本地模型服务未启动，且已关闭自动启动（设置里可打开）")
            return False
        kind = str(self.cfg.get("local.kind", "ollama")).lower()
        with self._lock:
            if self.alive():
                return True
            if kind == "ollama":
                exe = str(self.cfg.get("local.server_exe", "") or "").strip()
                if not exe or not os.path.exists(exe):
                    from .config import default_ollama_paths

                    found = default_ollama_paths()
                    exe = found.get("exe", "")
                    if exe:
                        self.cfg.set("local.server_exe", exe)
                        self.cfg.save()
                if not exe:
                    say("找不到 ollama 可执行文件，请在「设置」里指定路径")
                    return False
                env = os.environ.copy()
                models = str(self.cfg.get("local.models_dir", "") or "").strip()
                home = str(self.cfg.get("local.home_dir", "") or "").strip()
                # 只在目录真实存在时才注入：配置是从别的机器拷过来的话，
                # 硬塞一个不存在的 OLLAMA_MODELS 会让 Ollama 找不到已下载的模型。
                if models and os.path.isdir(models):
                    env["OLLAMA_MODELS"] = models
                elif models:
                    say(f"配置的模型目录不存在，已忽略：{models}")
                if home and os.path.isdir(home):
                    env["USERPROFILE"] = home
                    env["HOME"] = home
                elif home:
                    say(f"配置的 Ollama 数据目录不存在，已忽略：{home}")
                say(f"正在启动本地模型服务：{exe} serve")
                # 关键：只能用 CREATE_NO_WINDOW，**不要**加 DETACHED_PROCESS。
                # 实测教训：DETACHED_PROCESS 会让 ollama 完全没有控制台，于是它随后启动的
                # 推理子进程（llama-server.exe，本身就是控制台程序）各自新建一个控制台窗口；
                # Windows 11 默认终端是 Windows Terminal，表现就是「每识别一次弹一堆黑色
                # 终端窗口、有的还残留」（实测一次启动弹了 15 个）。
                # 用 CREATE_NO_WINDOW 给 ollama 一个**隐藏**控制台，子进程继承它，
                # 全程不出现任何可见窗口（已实测：加载模型前后新增 0 个窗口）。
                flags = 0x08000000 if sys.platform == "win32" else 0
                try:
                    self._proc = subprocess.Popen(
                        [exe, "serve"],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
                except Exception as exc:
                    say(f"启动失败：{exc}")
                    return False
            else:
                # LM Studio：用官方 CLI
                say("正在启动 LM Studio 本地服务：lms server start")
                try:
                    subprocess.Popen(
                        ["lms", "server", "start"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0x08000000 if sys.platform == "win32" else 0,
                    )
                except Exception as exc:
                    say(f"启动失败（可手动打开 LM Studio 后点击 Start Server）：{exc}")

        deadline = time.time() + max(5, wait_seconds)
        while time.time() < deadline:
            if self.alive():
                say("本地模型服务已就绪")
                return True
            time.sleep(1.0)
        say("等待本地模型服务超时，请手动打开 Ollama/LM Studio 后重试")
        return False

    # -- 模型列表 ---------------------------------------------------------- #
    def list_models(self) -> List[str]:
        kind = str(self.cfg.get("local.kind", "ollama")).lower()
        base = str(self.cfg.get("local.base_url", ""))
        try:
            if kind == "ollama":
                data = _get_json(f"{_split_base(base)[0]}/api/tags", 6)
                return sorted({m.get("name", "") for m in data.get("models", []) if m.get("name")})
            data = _get_json(self._openai_base() + "/models", 6, self._auth_headers())
            return sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})
        except Exception as exc:
            log.debug("获取本地模型列表失败：%s", exc)
            return []

    def model_loaded(self) -> bool:
        return bool(self.list_models())


# --------------------------------------------------------------------------- #
# 统一客户端
# --------------------------------------------------------------------------- #
class ModelClient:
    """同时支持本地与云端的聊天/视觉客户端。

    参数
    ----
    target : "local" | "cloud"
    cfg    : :class:`exerarchive.config.Config`
    """

    def __init__(self, target: str, cfg):
        self.target = target
        self.cfg = cfg
        self.manager = LocalServerManager(cfg)

    # -- 配置 -------------------------------------------------------------- #
    def _settings(self) -> Dict:
        if self.target == "local":
            return {
                "kind": str(self.cfg.get("local.kind", "ollama")).lower(),
                "base_url": str(self.cfg.get("local.base_url", "http://127.0.0.1:11434")),
                "model": str(self.cfg.get("local.model", "qwen3-vl:4b")),
                "timeout": float(self.cfg.get("local.timeout", 300)),
                "temperature": float(self.cfg.get("local.temperature", 0.1)),
                "num_ctx": int(self.cfg.get("local.num_ctx", 8192)),
                "max_side": int(self.cfg.get("local.max_image_side", 1600)),
                "quality": int(self.cfg.get("local.jpeg_quality", 88)),
                "keep_alive": str(self.cfg.get("local.keep_alive", "10m")),
                "think": not bool(self.cfg.get("local.disable_thinking", True)),
                "api_key": str(self.cfg.get("local.api_key", "") or ""),
                "vision": True,
            }
        prof = self.cfg.active_cloud() or {}
        return {
            "kind": "openai",
            "base_url": str(prof.get("base_url", "https://api.deepseek.com/v1")),
            "model": str(prof.get("model", "deepseek-chat")),
            "timeout": float(prof.get("timeout", 180)),
            "temperature": float(prof.get("temperature", 0.3)),
            "max_tokens": int(prof.get("max_tokens", 4096)),
            "max_side": int(self.cfg.get("local.max_image_side", 1600)),
            "quality": int(self.cfg.get("local.jpeg_quality", 88)),
            "api_key": self.cfg.effective_api_key(),
            "vision": bool(prof.get("supports_vision", False))
            and bool(self.cfg.get("privacy.send_image_to_cloud", True)),
            "name": str(prof.get("name", "云端")),
        }

    def engine_name(self) -> str:
        s = self._settings()
        return f"{self.target}:{s['model']}"

    def is_configured(self) -> bool:
        s = self._settings()
        if self.target == "local":
            return bool(s["base_url"] and s["model"])
        return bool(s["api_key"] and s["base_url"] and s["model"])

    # -- 主入口 ------------------------------------------------------------ #
    def chat(
        self,
        prompt: str,
        images: Optional[List] = None,
        system: str = "",
        json_mode: bool = True,
        timeout: Optional[float] = None,
        on_log: Optional[Callable[[str], None]] = None,
        allow_images: bool = True,
    ) -> ChatResult:
        """调用模型。``images`` 可以是文件路径、:class:`~exerarchive.imageops.Image` 或字节流。"""
        say = on_log or (lambda m: log.info("%s", m))
        s = self._settings()
        engine = f"{self.target}:{s['model']}"
        payload_images = self._encode_images(images or [], s, say) if allow_images else []
        if self.target == "local":
            ok = self.manager.ensure_running(on_log=say)
            if not ok:
                return ChatResult(engine=engine, ok=False, error="本地模型服务不可用")
        t0 = time.time()
        try:
            if s["kind"] == "ollama":
                res = self._call_ollama(prompt, payload_images, system, json_mode, s, timeout, say)
            else:
                res = self._call_openai(prompt, payload_images, system, json_mode, s, timeout, say)
            res.engine = engine
            res.elapsed = time.time() - t0
            res.used_images = len(payload_images)
            return res
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            msg = f"HTTP {exc.code}：{detail or exc.reason}"
            log.warning("模型调用失败（%s）：%s", engine, msg)
            return ChatResult(engine=engine, ok=False, error=msg, elapsed=time.time() - t0)
        except Exception as exc:
            log.warning("模型调用异常（%s）：%s", engine, exc)
            return ChatResult(engine=engine, ok=False, error=f"{type(exc).__name__}: {exc}",
                              elapsed=time.time() - t0)

    # -- 图片编码 ---------------------------------------------------------- #
    def _encode_images(self, images: List, s: Dict, say: Callable[[str], None]) -> List[str]:
        from . import imageops

        out: List[str] = []
        for item in images:
            try:
                if isinstance(item, (bytes, bytearray)):
                    b64 = base64.b64encode(bytes(item)).decode("ascii")
                elif hasattr(item, "to_base64"):  # Image 对象
                    img = item
                    if max(img.w, img.h) > s["max_side"]:
                        img = img.scale_to_fit(s["max_side"])
                    b64 = img.to_base64(s["quality"], "jpeg")
                elif isinstance(item, str):
                    if item.lower().startswith("data:image") and ";base64," in item:
                        b64 = item.split(";base64,", 1)[1]
                    elif os.path.exists(item):
                        img = imageops.load(item)
                        if max(img.w, img.h) > s["max_side"]:
                            img = img.scale_to_fit(s["max_side"])
                        b64 = img.to_base64(s["quality"], "jpeg")
                    else:
                        continue
                else:
                    continue
                out.append(b64)
            except Exception as exc:
                say(f"图片编码失败（已跳过）：{exc}")
        return out

    # -- Ollama ------------------------------------------------------------ #
    def _call_ollama(
        self, prompt: str, images: List[str], system: str, json_mode: bool, s: Dict, timeout, say
    ) -> ChatResult:
        origin, _ = _split_base(s["base_url"])
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        user_msg = {"role": "user", "content": prompt}
        if images:
            user_msg["images"] = images
        messages.append(user_msg)
        body = {
            "model": s["model"],
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": s["temperature"],
                "num_ctx": s.get("num_ctx", 8192),
            },
            "keep_alive": s.get("keep_alive", "10m"),
            "think": bool(s.get("think", False)),
        }
        if json_mode:
            body["format"] = "json"
        data = _post_json(f"{origin}/api/chat", body, timeout or s["timeout"])
        msg = data.get("message") or {}
        content = (msg.get("content") or "").strip()
        thinking = (msg.get("thinking") or "").strip()
        # 坑：think=false 时部分模型仍把答案写进 thinking，content 反而为空
        text = content if content else thinking
        return ChatResult(
            text=text,
            thinking=thinking,
            ok=bool(text),
            error="" if text else "模型返回空内容",
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            raw={"done_reason": data.get("done_reason", ""), "model": data.get("model", "")},
        )

    # -- OpenAI 兼容（云端 / LM Studio） ----------------------------------- #
    def _call_openai(
        self, prompt: str, images: List[str], system: str, json_mode: bool, s: Dict, timeout, say
    ) -> ChatResult:
        origin, path = _split_base(s["base_url"])
        url = origin + (path if path.endswith("/chat/completions") else (
            path + "/chat/completions" if path else "/v1/chat/completions"
        ))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        if images and s.get("vision", True):
            content = [{"type": "text", "text": prompt}]
            for b64 in images:
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                )
            messages.append({"role": "user", "content": content})
        else:
            if images and not s.get("vision", True):
                say("当前云端模型未开启视觉能力，只发送本地识别出的文字")
            messages.append({"role": "user", "content": prompt})
        body = {
            "model": s["model"],
            "messages": messages,
            "temperature": s["temperature"],
            "stream": False,
        }
        if s.get("max_tokens"):
            body["max_tokens"] = s["max_tokens"]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {}
        if s.get("api_key"):
            headers["Authorization"] = f"Bearer {s['api_key']}"
        try:
            data = _post_json(url, body, timeout or s["timeout"], headers)
        except urllib.error.HTTPError as exc:
            # 有些服务不认 response_format，去掉重试一次
            if exc.code in (400, 422) and json_mode:
                body.pop("response_format", None)
                data = _post_json(url, body, timeout or s["timeout"], headers)
            else:
                raise
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"云端返回缺少 choices：{str(data)[:200]}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if isinstance(content, list):  # 少数服务返回分段内容
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        usage = data.get("usage") or {}
        return ChatResult(
            text=content,
            ok=bool(content),
            error="" if content else "模型返回空内容",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw={"finish_reason": choices[0].get("finish_reason", "")},
        )

    # -- 自检 -------------------------------------------------------------- #
    def ping(self, timeout: float = 20.0) -> ChatResult:
        """最小连通性测试（用于「测试连接」按钮与自检）。"""
        return self.chat(
            "只回答两个字：正常",
            images=[],
            system="",
            json_mode=False,
            timeout=timeout,
        )


def make_client(target: str, cfg) -> ModelClient:
    return ModelClient(target, cfg)
