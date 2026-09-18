# -*- coding: utf-8 -*-
"""本地假 Ollama 服务（仅用于测试/验证）。

为什么需要：Ollama 的 ``/api/chat`` 与 OpenAI 的 ``/v1/chat/completions`` 协议不同
（图片是**裸 base64 数组**而非 data URI、参数放在 ``options`` 里、还有 ``think``/``format``
这些 Ollama 专有字段），而且本机实测过一个坑：**``think=false`` 时 qwen3-vl 会把正式答案
写进 ``message.thinking``，``message.content`` 反而是空的**。这些行为光靠单测保证不了，
所以这里起一个假 Ollama，把请求体记下来逐项断言。

被 ``tools/verify_local_backends.py`` 使用。也支持故障注入：
``fail_times``（返回 500）、``empty_content``（只给 thinking、不给 content，
用来验证"从 thinking 里取答案"的回退逻辑）、``plain_text``（返回非 JSON 文本，
用来验证自动追问"只输出 JSON"的重试）。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_RECOGNIZE = {
    "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
    "questions": [{
        "index": 1, "type": "解答题", "status": "wrong",
        "stem": "计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
        "options": [], "student_answer": "7", "student_steps": "展开得 7",
        "correct_answer": "-3", "knowledge_points": ["行列式"],
        "difficulty": "中等", "confidence": 0.93,
    }],
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET --------------------------------------------------------------- #
    def do_GET(self):
        srv = self.server.mock
        srv.requests.append({"method": "GET", "path": self.path})
        if self.path.endswith("/api/version"):
            self._json(200, {"version": "0.34.0-mock"})
        elif self.path.endswith("/api/tags"):
            self._json(200, {"models": [
                {"name": srv.model, "model": srv.model, "size": 3295636135,
                 "details": {"family": "qwen3vl", "parameter_size": "4.4B",
                             "quantization_level": "Q4_K_M"},
                 "capabilities": ["vision", "completion", "thinking"]},
                {"name": "llama3.2:3b", "model": "llama3.2:3b", "size": 2019393189,
                 "details": {"family": "llama", "parameter_size": "3.2B"},
                 "capabilities": ["completion"]},
            ]})
        elif self.path.endswith("/api/ps"):
            self._json(200, {"models": [{"name": srv.model, "size": 3295636135,
                                         "size_vram": 3295636135}]})
        elif self.path.endswith("/api/show"):
            self._json(200, {"details": {"family": "qwen3vl", "parameter_size": "4.4B",
                                         "quantization_level": "Q4_K_M"},
                             "capabilities": ["vision", "completion", "thinking"]})
        else:
            self._json(404, {"error": f"no such path {self.path}"})

    # -- POST -------------------------------------------------------------- #
    def do_POST(self):
        srv = self.server.mock
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        srv.requests.append({"method": "POST", "path": self.path, "body": body})

        if srv.fail_times > 0:
            srv.fail_times -= 1
            self._json(500, {"error": "mock internal error"})
            return

        text = json.dumps(srv.recognize if srv.recognize else DEFAULT_RECOGNIZE, ensure_ascii=False)
        if srv.plain_text:
            # 第一遍故意返回非 JSON 的"话痨"文本，用来验证自动重试追问
            srv.plain_text = False
            self._json(200, {"model": body.get("model", srv.model), "done": True,
                             "message": {"role": "assistant",
                                         "content": "好的，我来看看这张图……（这里本该是 JSON）"},
                             "prompt_eval_count": 1500, "eval_count": 120,
                             "done_reason": "stop"})
            return

        message = {"role": "assistant", "content": text}
        if srv.empty_content:
            # 关键坑：content 为空、答案在 thinking 里
            message = {"role": "assistant", "content": "", "thinking": text}
        self._json(200, {
            "model": body.get("model", srv.model), "created_at": "2026-09-18T00:00:00Z",
            "done": True, "done_reason": "stop", "message": message,
            "prompt_eval_count": 1513, "eval_count": 488,
        })


class MockOllamaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 11499, model: str = "qwen3-vl:4b"):
        super().__init__(("127.0.0.1", port), _Handler)
        self.mock = self
        self.model = model
        self.recognize = None            # None → 用默认识别结果
        self.requests: list = []
        self.fail_times = 0
        self.empty_content = False       # 答案只出现在 thinking 字段
        self.plain_text = False          # 先返回非 JSON（触发重试）


class MockOllama:
    """便捷包装。"""

    def __init__(self, port: int = 11499, model: str = "qwen3-vl:4b"):
        self.port = port
        self._server = MockOllamaServer(port, model)

    def start(self) -> "MockOllama":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        time.sleep(0.25)
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    def __enter__(self) -> "MockOllama":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list:
        return self._server.requests

    @property
    def model(self) -> str:
        return self._server.model

    def clear(self) -> None:
        self._server.requests.clear()

    @property
    def fail_times(self) -> int:
        return self._server.fail_times

    @fail_times.setter
    def fail_times(self, value: int) -> None:
        self._server.fail_times = int(value)

    @property
    def empty_content(self) -> bool:
        return self._server.empty_content

    @empty_content.setter
    def empty_content(self, value: bool) -> None:
        self._server.empty_content = bool(value)

    @property
    def plain_text(self) -> bool:
        return self._server.plain_text

    @plain_text.setter
    def plain_text(self, value: bool) -> None:
        self._server.plain_text = bool(value)

    def last_post(self) -> dict:
        for req in reversed(self.requests):
            if req.get("method") == "POST":
                return req
        return {}


if __name__ == "__main__":
    srv = MockOllama(port=11499).start()
    print(f"假 Ollama 已启动：{srv.base_url}（/api/tags、/api/chat）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
