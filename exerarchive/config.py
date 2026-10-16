# -*- coding: utf-8 -*-
"""配置管理：``data/config.json``。

配置分四块：
- ``run``     运行策略（本地优先 / 自动兜底 / 纯云端 / 纯本地）
- ``local``   本地轻量化视觉模型（Ollama / LM Studio / 任意 OpenAI 兼容本地服务）
- ``cloud``   云端强力模型（OpenAI 兼容接口，可填任意厂商），支持多套配置档案
- ``ui``/``privacy``/``archive``  界面、隐私加密、归档方式

读取时用「默认值深合并」，因此旧配置文件缺少新字段也能自动补齐；
写回时原子替换（先写 .tmp 再 os.replace），避免断电/崩溃把配置写坏。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # ---------------- 运行策略 ----------------
    "run": {
        # local_only : 全程本地，绝不联网（隐私最强，速度取决于本机）
        # hybrid     : 本地优先，识别置信度低/图片质量差/识别失败时自动切云端（默认，推荐）
        # cloud_first: 直接云端处理（需填 API Key，最准但依赖网络）
        "mode": "hybrid",
        "confidence_threshold": 0.70,   # 低于该置信度触发云端兜底
        "quality_threshold": 0.35,      # 图片质量分（模糊/过暗/倾斜）低于该值直接云端
        "cloud_on_error": True,         # 本地报错时自动转云端
        "cloud_for_analysis": True,     # 错题深度分析是否交云端（本地已有基础版）
        "cloud_for_variants": True,     # 变式练习题是否交云端生成
        "max_concurrency": 1,           # 同时识别的图片数（显存小建议 1）
        "auto_preprocess": True,        # 导入后自动纠偏/提亮/去水印
        "auto_analyze": True,           # 识别出错题后自动生成分析报告
        "max_questions_per_image": 8,   # 单张图最多切分几道题（防止模型啰嗦）
    },
    # ---------------- 本地模型 ----------------
    "local": {
        "kind": "ollama",                       # ollama | lmstudio | openai_compatible
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3-vl:4b",
        "timeout": 300,                          # 秒，CPU 跑 4B 视觉模型要给足
        "num_ctx": 8192,
        "temperature": 0.1,
        "max_image_side": 1600,                  # 送模型前的最长边（越小越快越省显存）
        "jpeg_quality": 88,
        "auto_start": True,                      # 启动软件时自动拉起本地模型服务
        "server_exe": "",                        # 留空则自动查找
        "models_dir": "",                        # Ollama 模型目录（多盘部署时填）
        "home_dir": "",                          # Ollama 数据目录（受限环境时填）
        "keep_alive": "10m",
        "disable_thinking": True,                # 关闭思考链，识别更快（qwen3-vl 支持）
    },
    # ---------------- 云端模型 ----------------
    "cloud": {
        "active": 0,                             # 当前使用的档案下标
        "enabled": False,                        # 云端增强总开关（界面上「云端增强」勾选框）
        "cache_last_result": True,
        "profiles": [
            {
                "name": "DeepSeek（云端）",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "",
                "supports_vision": False,        # 纯文本模型：只发本地识别出的文字做深度分析
                "timeout": 180,
                "max_tokens": 4096,
                "temperature": 0.3,
            },
            {
                "name": "任意 OpenAI 兼容（可填通义/智谱/Kimi/本地大模型）",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key": "",
                "supports_vision": True,
                "timeout": 180,
                "max_tokens": 4096,
                "temperature": 0.3,
            },
        ],
    },
    # ---------------- 界面 ----------------
    "ui": {
        "theme": "light",
        "font_family": "微软雅黑",
        "font_size": 10,
        "thumb_size": 180,
        "window": "",
        "show_log_panel": False,
        "animations": True,                      # 按钮悬停与导航过渡，可在设置中关闭
        "image_display_max": 1200,               # 预览区图片最长边
        "math_scale": 1.15,                      # 报告里公式相对正文行高的倍率（1.0~1.6 比较合适）
        "math_supersample": 3,                   # 公式渲染超采样倍数（越大越清晰、越慢）
    },
    # ---------------- 隐私 ----------------
    "privacy": {
        "encrypt_db": True,                      # 题干/答案/报告在数据库里加密存放
        "send_image_to_cloud": True,             # 允许把（压缩后的）图片传云端；关掉则只传文字
        "delete_local_after_cloud": False,       # 云端处理完是否删本地原图（默认否）
    },
    # ---------------- 归档 ----------------
    "archive": {
        "root": "",                              # 留空 = data/归档
        "pattern": "{subject}/{grade}/{qtype}/{status}",   # 目录层级模板
        "write_text": True,                      # 同步写 .md 文字版
        "write_report": True,                    # 错题报告写入归档
        "copy_original": True,                   # 归档时复制原图（否则只放预处理图）
        "naming": "{uid}_{status}",              # 文件名模板
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base 的副本（递归字典合并，列表整体替换）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """配置对象：支持 cfg["run.mode"] 点号读取、自动补默认值、原子保存。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[Path] = None):
        self._data = _deep_merge(DEFAULTS, data or {})
        # 清掉历史遗留的"影子键"（顶层带点号的键）：get() 从不读它们，
        # 是旧版 update() 的 bug 写进去的（见 update 的说明），留着只会让配置文件自相矛盾。
        self._data = {key: value for key, value in self._data.items() if "." not in key}
        self._path = Path(path) if path else paths.config_path()

    # -- 读写 -------------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        p = Path(path) if path else paths.config_path()
        data: Dict[str, Any] = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                # 配置损坏时保留一份备份，避免用户改坏后无法恢复
                try:
                    p.with_suffix(".broken.json").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
                data = {}
        cfg = cls(data, p)
        return cfg

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # -- 访问 -------------------------------------------------------------- #
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self._data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    def update(self, patch: Dict[str, Any]) -> None:
        """合并配置补丁：**点号键按路径写入**，其余键做递归字典合并。

        ``{"run.auto_analyze": False}`` 等价于 ``set("run.auto_analyze", False)``；
        嵌套字典（如 ``DEFAULTS``）仍按原来的递归方式合并。

        以前这里一律交给 :func:`_deep_merge`：扁平点号键会被当成**普通键**塞进顶层，
        于是配置文件里长出一堆 ``"run.confidence_threshold": 0.5`` 这样的"影子键" ——
        而 :meth:`get` 读的是嵌套段 ``run.confidence_threshold``，两者互不相干。
        后果是设置窗口里点了「保存」（日志还写着"设置已保存"）却**一点都不生效**，
        用户反馈的「设置中改换设置无保存选项，无法正常改设置」正是它。
        """
        patch = patch or {}
        nested = {key: value for key, value in patch.items() if "." not in key}
        if nested:
            self._data = _deep_merge(self._data, nested)
        for key, value in patch.items():
            if "." in key:
                self.set(key, value)

    # -- 便捷属性 ---------------------------------------------------------- #
    @property
    def path(self) -> Path:
        return self._path

    def local(self) -> Dict[str, Any]:
        return self._data["local"]

    def run(self) -> Dict[str, Any]:
        return self._data["run"]

    def cloud_enabled(self) -> bool:
        """云端增强是否真的可用：总开关 + 有 API Key。"""
        if not self.get("cloud.enabled", False):
            return False
        return bool(str(self.active_cloud().get("api_key", "")).strip())

    def active_cloud(self) -> Dict[str, Any]:
        profiles = self.get("cloud.profiles", []) or []
        idx = int(self.get("cloud.active", 0) or 0)
        if not profiles:
            return {}
        idx = max(0, min(idx, len(profiles) - 1))
        return profiles[idx]

    def set_active_cloud(self, idx: int) -> None:
        profiles = self.get("cloud.profiles", []) or []
        if profiles:
            self.set("cloud.active", max(0, min(int(idx), len(profiles) - 1)))

    def mode(self) -> str:
        m = str(self.get("run.mode", "hybrid")).lower()
        return m if m in ("local_only", "hybrid", "cloud_first") else "hybrid"

    def mode_label(self) -> str:
        return {
            "local_only": "纯本地（离线）",
            "hybrid": "本地优先 + 云端兜底",
            "cloud_first": "云端优先",
        }.get(self.mode(), self.mode())

    def env_api_key(self) -> str:
        """支持用环境变量提供 Key，配置文件里留空时自动读取。"""
        for name in ("EXER_CLOUD_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            v = os.environ.get(name)
            if v and v.strip():
                return v.strip()
        return ""

    def effective_api_key(self) -> str:
        return str(self.active_cloud().get("api_key", "")).strip() or self.env_api_key()


_SINGLETON: Optional[Config] = None


def load_config(reload: bool = False) -> Config:
    """进程级单例配置。"""
    global _SINGLETON
    if _SINGLETON is None or reload:
        _SINGLETON = Config.load()
    return _SINGLETON


def save_config(cfg: Optional[Config] = None) -> None:
    (cfg or load_config()).save()


def _search_roots() -> list:
    """查找本地模型时按顺序尝试的搜索根。

    同时搜索**代码目录**与**数据目录**（含它们的上级、上上级）：绿色部署时
    ``ollama/``、``ollama-models/`` 常与程序同级；如果用户用 ``EXER_HOME`` 把数据
    挪到了别的盘，模型目录也可能放在那边。
    """
    roots = []
    for base in (paths.code_root(), paths.app_root()):
        for p in (base, base.parent, base.parent.parent):
            if p not in roots:
                roots.append(p)
    return roots


def default_ollama_paths() -> Dict[str, str]:
    """在常见位置查找 Ollama 可执行文件与模型目录（供自检/自动启动使用）。"""
    found = {"exe": "", "models_dir": "", "home_dir": ""}

    exe_cands = []
    for r in _search_roots():
        exe_cands.append(r / "ollama" / "ollama.exe")
        exe_cands.append(r / "ollama.exe")
    exe_cands += [
        Path(r"C:\Program Files\Ollama\ollama.exe"),
        Path(os.path.expanduser(r"~\AppData\Local\Programs\Ollama\ollama.exe")),
    ]
    for c in exe_cands:
        if Path(c).exists():
            found["exe"] = str(c)
            break
    if not found["exe"]:
        from shutil import which

        w = which("ollama")
        if w:
            found["exe"] = w

    for r in _search_roots():
        for name in ("ollama-models", "models"):
            c = r / name
            if (c / "manifests").is_dir() or (c / "blobs").is_dir():
                found["models_dir"] = str(c)
                break
        if found["models_dir"]:
            break
    if not found["models_dir"]:
        c = Path(os.path.expanduser(r"~\.ollama\models"))
        if c.is_dir():
            found["models_dir"] = str(c)

    for r in _search_roots():
        c = r / "ollama-home"
        if (c / ".ollama").is_dir():
            found["home_dir"] = str(c)
            break
    if not found["home_dir"]:
        c = Path(os.path.expanduser("~"))
        if (c / ".ollama").is_dir():
            found["home_dir"] = str(c)

    return found


def bootstrap_local_model(cfg: Config) -> None:
    """首次运行时，把探测到的本地模型路径写进配置（用户不用手填）。"""
    if str(cfg.get("local.server_exe", "")).strip() and str(cfg.get("local.models_dir", "")).strip():
        return
    found = default_ollama_paths()
    if found["exe"] and not str(cfg.get("local.server_exe", "")).strip():
        cfg.set("local.server_exe", found["exe"])
    if found["models_dir"] and not str(cfg.get("local.models_dir", "")).strip():
        cfg.set("local.models_dir", found["models_dir"])
    if found["home_dir"] and not str(cfg.get("local.home_dir", "")).strip():
        cfg.set("local.home_dir", found["home_dir"])
    cfg.save()
