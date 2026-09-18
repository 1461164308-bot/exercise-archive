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
            # 驻留状态可被测试改：resident=False 表示模型已卸载；
            # size_vram_ratio 用来模拟「只有一部分装在显存里」的慢档（Ollama 的真实行为）
            if not srv.resident:
                self._json(200, {"models": []})
            else:
                size = 3295636135
                self._json(200, {"models": [{
                    "name": srv.model, "size": size,
                    "size_vram": int(size * float(srv.size_vram_ratio)),
                }]})
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

        # /api/generate：本项目只用它做「卸载（keep_alive=0）」与「重新加载（空跑一次）」。
        # 这里忠实模拟真实 Ollama 的两条关键行为：
        #   ① keep_alive=0 → 模型被移出显存（/api/ps 变空）
        #   ② 重新加载时按**当前**空闲显存决定分层 → 用 load_ratio 表示
        if self.path.endswith("/api/generate"):
            if body.get("keep_alive") == 0 or body.get("keep_alive") == "0":
                srv.resident = False
                self._json(200, {"model": body.get("model", srv.model), "done": True,
                                 "done_reason": "unload", "response": ""})
                return
            srv.resident = True
            srv.size_vram_ratio = float(srv.load_ratio)     # 加载那一刻决定分层
            srv.load_count += 1
            self._json(200, {"model": body.get("model", srv.model), "done": True,
                             "done_reason": "stop", "response": "ok",
                             "prompt_eval_count": 1, "eval_count": 1})
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
        # 显存分层模拟（对应真实 Ollama 的 /api/ps 与加载行为）
        self.resident = True             # 模型当前是否驻留
        self.size_vram_ratio = 1.0       # 当前有多少装在显存里（1.0 = 全部）
        self.load_ratio = 1.0            # 「下一次加载」时会得到多少上卡比例
        self.load_count = 0              # 被加载了几次（用来断言重载真的发生了）


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

    # -- 显存分层模拟 ------------------------------------------------------ #
    @property
    def resident(self) -> bool:
        return self._server.resident

    @resident.setter
    def resident(self, value: bool) -> None:
        self._server.resident = bool(value)

    @property
    def size_vram_ratio(self) -> float:
        return self._server.size_vram_ratio

    @size_vram_ratio.setter
    def size_vram_ratio(self, value: float) -> None:
        self._server.size_vram_ratio = float(value)

    @property
    def load_ratio(self) -> float:
        """模拟「下次加载时会拿到多少上卡比例」——显存紧张时真实 Ollama 就是这个行为。"""
        return self._server.load_ratio

    @load_ratio.setter
    def load_ratio(self, value: float) -> None:
        self._server.load_ratio = float(value)

    @property
    def load_count(self) -> int:
        return self._server.load_count

    def generate_calls(self) -> list:
        """所有打到 /api/generate 的请求（卸载与重载都走它）。"""
        return [r for r in self.requests
                if r.get("method") == "POST" and str(r.get("path", "")).endswith("/api/generate")]

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
