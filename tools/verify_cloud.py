# -*- coding: utf-8 -*-
"""云端通道验证：用一个**本地假的 OpenAI 兼容服务**跑通云端全链路（不需要真 API Key）。

为什么值得单独验：
- 单测里用的是「假 Python 客户端」，只能证明逻辑分支对，证明不了**真实 HTTP 请求体/响应解析**对不对；
- 真实云端要花钱、要联网，本机也不一定有 Key；所以这里起一个 `http.server` 假装成
  OpenAI 兼容服务，把请求体**原样记录下来**，逐项断言：

  1. URL 拼接（`/v1` 有/无/写全三种写法都要能拼对）；
  2. 认证头 `Authorization: Bearer …`；
  3. 支持视觉的模型：图片以 `data:image/jpeg;base64,…` 放在 `content[]` 里；
     纯文本模型（`supports_vision=false`）或关掉「允许把图片传给云端」时**一张图都不发**；
  4. `response_format={"type":"json_object"}` 的发送与「服务端 400/422 时自动去掉重试」；
  5. 返回内容解析（含 Markdown 代码块包裹的 JSON）、`usage` token 统计；
  6. 端到端：`cloud_first` 模式识别一张图 → 出题；错题分析走云端 → 报告 depth=cloud；
  7. 失败可控：服务返回 500 时 `ok=False` 且有错误信息，不会抛异常炸掉界面。

用法::

    python tools/verify_cloud.py          # 全部用例（纯 CPU，几秒）
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import config, gdiplus, imageops, logging_setup  # noqa: E402
from exerarchive.analysis import Analyzer  # noqa: E402
from exerarchive.pipeline import Pipeline  # noqa: E402
from exerarchive.vlm import ModelClient, _split_base  # noqa: E402

PORT = 8899
RECOGNIZE_JSON = {
    "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
    "questions": [{
        "index": 1, "type": "解答题", "status": "wrong",
        "stem": "计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
        "options": [], "student_answer": "7", "student_steps": "展开得 7",
        "correct_answer": "-3", "knowledge_points": ["行列式"],
        "difficulty": "中等", "confidence": 0.96,
    }],
}
ANALYZE_JSON = {
    "restate": "云端复核：学生把三阶行列式的符号展开写错，得 7，正确为 -3。",
    "causes": [{"type": "公式记错", "detail": "萨吕斯法则后三项符号写反"}],
    "knowledge_points": [{"name": "三阶行列式展开", "textbook": "线性代数·行列式", "note": "符号 + - +"}],
    "solution_steps": ["列出六项", "主对角线求和 230", "副对角线求和 233", "D = -3"],
    "final_answer": "-3",
    "avoid_tips": ["先把符号写在草稿上"],
    "variants": [{"stem": "计算 |2 1; 3 4|", "answer": "5", "hint": "对角线法则"}],
}

REQUESTS = []       # 记录收到的请求体，供断言
FAIL_TIMES = [0]    # 让服务返回 500 的次数
REJECT_FORMAT = [0]  # 带 response_format 时返回 400 的次数


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if "authorization" not in {k.lower() for k in self.headers.keys()}:
                self._send(401, {"error": {"message": "missing key"}})
                return
            self._send(200, {"object": "list", "data": [
                {"id": "mock-vl", "object": "model"}, {"id": "mock-text", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": f"no such path {self.path}"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            self._send(400, {"error": {"message": "bad json"}})
            return
        REQUESTS.append({"path": self.path,
                         "auth": self.headers.get("Authorization", ""),
                         "body": body})
        if FAIL_TIMES[0] > 0:
            FAIL_TIMES[0] -= 1
            self._send(500, {"error": {"message": "mock internal error"}})
            return
        # 模拟"服务端不认 response_format"的服务
        if REJECT_FORMAT[0] > 0 and "response_format" in body:
            REJECT_FORMAT[0] -= 1
            self._send(400, {"error": {"message": "response_format is not supported"}})
            return
        text = json.dumps(ANALYZE_JSON if "错题" in json.dumps(body["messages"], ensure_ascii=False)
                          else RECOGNIZE_JSON, ensure_ascii=False)
        if body.get("mock_wrap_fence"):
            text = "```json\n" + text + "\n```"
        self._send(200, {
            "id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801},
        })


def make_cfg(base_url: str, vision: bool = True, mode: str = "cloud_first",
             send_images: bool = True) -> config.Config:
    cfg = config.Config({})
    cfg.set("run.mode", mode)
    cfg.set("run.auto_preprocess", False)
    cfg.set("local.auto_start", False)
    cfg.set("cloud.enabled", True)
    cfg.set("privacy.send_image_to_cloud", send_images)
    cfg.set("cloud.profiles", [{
        "name": "本地模拟服务", "base_url": base_url, "model": "mock-vl",
        "api_key": "sk-mock-key", "supports_vision": vision,
        "timeout": 30, "max_tokens": 1024, "temperature": 0.2,
    }])
    cfg.set("cloud.active", 0)
    return cfg


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"　{detail}" if detail else ""))
    return bool(cond)


def main() -> int:
    logging_setup.setup()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print(f"模拟云端服务已启动：http://127.0.0.1:{PORT}/v1（OpenAI 兼容）")
    ok = True

    # 造一张题目图（不需要模型，纯 GDI 渲染）
    img_path = os.path.join(ROOT, "verify", "_tmp_cloud.png")
    os.makedirs(os.path.dirname(img_path), exist_ok=True)
    if gdiplus.available():
        w, h, px = gdiplus.draw_text_bitmap(800, 500, font_size=30,
                                            lines=["1. 计算三阶行列式 D 的值", "学生作答：D = 7"])
        imageops.Image(w, h, px).save(img_path)
    else:
        imageops.Image(200, 100, bytearray(b"\xff" * (200 * 100 * 3))).save(img_path)

    # ---------- 1. 连通性与模型列表 ----------
    print("\n1) 连通性与模型列表")
    cfg = make_cfg(f"http://127.0.0.1:{PORT}/v1")
    client = ModelClient("cloud", cfg)
    ok &= check("is_configured()", client.is_configured())
    models = client.list_models() if hasattr(client, "list_models") else []
    ok &= check("ping（真实 HTTP 往返）", client.ping(timeout=15).ok)
    ok &= check("Authorization 头已带上", REQUESTS[-1]["auth"] == "Bearer sk-mock-key",
                REQUESTS[-1]["auth"])

    # ---------- 2. URL 三种写法 ----------
    print("\n2) Base URL 拼接（带 /v1、不带 /v1、写全）")
    for base in (f"http://127.0.0.1:{PORT}/v1", f"http://127.0.0.1:{PORT}",
                 f"http://127.0.0.1:{PORT}/v1/chat/completions"):
        REQUESTS.clear()
        res = ModelClient("cloud", make_cfg(base)).chat("只输出 OK", [], json_mode=False, timeout=15)
        path = REQUESTS[-1]["path"] if REQUESTS else "(无请求)"
        ok &= check(f"{base} → {path}", res.ok and path.endswith("/chat/completions"))

    # ---------- 3. 请求体结构 ----------
    print("\n3) 请求体结构（视觉模型）")
    REQUESTS.clear()
    res = client.chat("识别这张图", [img_path], system="你是识别引擎", json_mode=True, timeout=30)
    req = REQUESTS[-1]["body"]
    content = req["messages"][-1]["content"]
    ok &= check("system 消息已发送", req["messages"][0]["role"] == "system")
    ok &= check("content 是分段数组（含 text）", isinstance(content, list) and content[0]["type"] == "text")
    img_part = [c for c in content if c["type"] == "image_url"]
    ok &= check("图片以 data:image/jpeg;base64 发送", bool(img_part) and
                img_part[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"),
                f"{len(img_part[0]['image_url']['url'])} 字符" if img_part else "没有图片")
    ok &= check("response_format 已带上", req.get("response_format") == {"type": "json_object"})
    ok &= check("Bearer Key 正确", REQUESTS[-1]["auth"] == "Bearer sk-mock-key")
    ok &= check("解析出结构化结果", (res.json() or {}).get("questions") is not None)
    ok &= check("usage token 统计已解析", res.prompt_tokens == 1234 and res.output_tokens == 567,
                f"{res.prompt_tokens}/{res.output_tokens}")

    # ---------- 4. 纯文本模型 / 隐私开关 ----------
    print("\n4) 不发图片的两种情况")
    for label, kwargs in (("supports_vision=false", {"vision": False}),
                          ("privacy.send_image_to_cloud=false", {"send_images": False})):
        REQUESTS.clear()
        c = ModelClient("cloud", make_cfg(f"http://127.0.0.1:{PORT}/v1", **kwargs))
        c.chat("分析这道题的错因", [img_path], json_mode=True, timeout=30)
        body_txt = json.dumps(REQUESTS[-1]["body"], ensure_ascii=False)
        ok &= check(f"{label} → 请求里没有图片", "image_url" not in body_txt and "base64" not in body_txt)

    # ---------- 5. response_format 被拒时自动重试 ----------
    print("\n5) 服务端不认 response_format（返回 400）时自动去掉重试")
    REQUESTS.clear()
    REJECT_FORMAT[0] = 1
    res = client.chat("识别这张图", [img_path], json_mode=True, timeout=30)
    ok &= check("第一次带 format 被拒后仍成功", res.ok, res.error)
    ok &= check("重试请求里已无 response_format",
                len(REQUESTS) >= 2 and "response_format" not in REQUESTS[-1]["body"])

    # ---------- 6. 服务端 500 ----------
    print("\n6) 服务端 500 时不炸、返回可读错误")
    FAIL_TIMES[0] = 1
    res = client.chat("识别这张图", [img_path], timeout=30)
    ok &= check("ok=False 且有错误信息", (not res.ok) and bool(res.error), res.error[:60])

    # ---------- 7. 端到端：cloud_first 识别 ----------
    print("\n7) 端到端识别（cloud_first 模式，整条流水线走真实 HTTP）")
    REQUESTS.clear()
    pipe = Pipeline(cfg, on_log=lambda m: None)
    out = pipe.process_image(img_path, skip_preprocess=True)
    ok &= check("路由为 cloud", out.route == "cloud", out.route)
    ok &= check("识别出 1 道题且状态为错题",
                out.question_count == 1 and out.questions[0]["status"] == "wrong")
    ok &= check("学科/细分学科正确",
                out.questions[0]["subject"] == "数学" and out.questions[0]["subject_detail"] == "线性代数")
    ok &= check("引擎标记为 cloud:*", str(out.engine).startswith("cloud:"), out.engine)

    # ---------- 8. 端到端：错题分析走云端 ----------
    print("\n8) 端到端错题分析（云端深度版）")
    analyzer = Analyzer(cfg, on_log=lambda m: None)
    q = dict(out.questions[0], uid="QTEST-0001", source_path=img_path)
    report = analyzer.analyze(q, allow_cloud=True)
    ok &= check("报告 depth=cloud", report.get("depth") == "cloud", str(report.get("depth")))
    ok &= check("六段内容齐全", all(report.get(k) for k in
                                    ("restate", "causes", "knowledge_points", "solution_steps",
                                     "final_answer", "avoid_tips", "variants")))
    ok &= check("云端给出的最终答案正确", report.get("final_answer") == "-3", report.get("final_answer"))

    # ---------- 9. 混合模式：本地不可用时也能给出结果（云端兜底） ----------
    print("\n9) hybrid 模式下本地不可用 → 自动转云端")
    cfg_hybrid = make_cfg(f"http://127.0.0.1:{PORT}/v1", mode="hybrid")
    cfg_hybrid.set("local.base_url", "http://127.0.0.1:1")   # 故意指向一个不存在的本地服务
    out2 = Pipeline(cfg_hybrid, on_log=lambda m: None).process_image(img_path, skip_preprocess=True)
    ok &= check("本地失败后仍拿到云端结果", out2.question_count == 1, f"route={out2.route}")

    try:
        os.remove(img_path)
    except OSError:
        pass
    server.shutdown()
    print("\n" + "=" * 70)
    print(f"云端通道验证：{'全部通过 ✅' if ok else '存在失败项 ❌'}")
    print(f"RESULT: {'OK' if ok else 'FAILED'} (cloud path over real HTTP)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
