# -*- coding: utf-8 -*-
"""本地假 OpenAI 兼容服务（仅用于测试/验证，不参与正式功能）。

为什么要它：真实云端要 Key、要联网、要花钱，而「云端适配得对不对」必须验证。
这个模块用标准库起一个 OpenAI 兼容的 HTTP 服务，**把收到的请求体原样记录下来**
（``REQUESTS``），于是既能跑通链路，又能断言请求体是否符合 OpenAI 规范。

被以下验证脚本复用：
- ``tools/verify_cloud.py``：云端通道本身（认证/URL/请求体/降级重试/错误处理/端到端）
- ``tools/verify_workflow.py``：把云端当"引擎"，验证 CLI 批量导入与 GUI 全流程

用法::

    srv = MockCloud(port=8899).start()
    ...  # 访问 http://127.0.0.1:8899/v1
    srv.stop()
    srv.requests  # 收到的请求列表
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 默认返回的识别结果 / 分析报告（可由调用方覆盖：srv.recognize = {...}）
DEFAULT_RECOGNIZE = {
    "subject": "数学", "subject_detail": "函数与导数", "grade": "高中",
    "questions": [
        {"index": 1, "type": "选择题", "status": "correct",
         "stem": "已知函数 f(x)=2x^2-3x+1，则 f(2) 的值为（　　）",
         "options": ["A. 1", "B. 3", "C. 7", "D. 9"], "student_answer": "B",
         "student_steps": "", "correct_answer": "B", "knowledge_points": ["函数值"],
         "difficulty": "简单", "confidence": 0.95},
        {"index": 2, "type": "选择题", "status": "wrong",
         "stem": "函数 f(x)=x^3-3x^2+2 的极小值是（　　）",
         "options": ["A. 2", "B. 0", "C. -2", "D. -4"], "student_answer": "B",
         "student_steps": "求导后令 f'(x)=0 得 x=0 或 2", "correct_answer": "C",
         "knowledge_points": ["导数", "函数极值"], "difficulty": "中等", "confidence": 0.92},
        {"index": 3, "type": "选择题", "status": "blank",
         "stem": "若 sinα=3/5，且 α 是第二象限角，则 cosα 的值是（　　）",
         "options": ["A. 3/5", "B. -3/5", "C. 4/5", "D. -4/5"], "student_answer": "",
         "student_steps": "", "correct_answer": "D", "knowledge_points": ["三角函数"],
         "difficulty": "中等", "confidence": 0.9},
    ],
}

DEFAULT_ANALYZE = {
    "restate": "题目要求函数 $f(x)=x^3-3x^2+2$ 的极小值。学生求导后得到驻点 $x=0$、$x=2$，"
               "但把极小值位置判断错了，选了 B（$0$），正确答案是 C（$-2$）。",
    "causes": [
        {"type": "知识点盲区", "detail": "没有用二阶导数 $f''(x)$ 或单调性判断极大/极小，只凭驻点猜"},
        {"type": "计算失误", "detail": "$f(2)=2^3-3\\times2^2+2=-2$ 这一步算成了 $f(0)=2$"},
    ],
    "knowledge_points": [
        {"name": "利用导数求极值", "textbook": "高中数学·导数及其应用",
         "note": "先求 $f'(x)=0$ 的驻点，再用 $f''(x)$ 的符号（或单调性）判断极大/极小"},
    ],
    "solution_steps": [
        "① 求导：$f'(x)=3x^2-6x=3x(x-2)$，驻点为 $x=0$ 与 $x=2$。",
        "② 判断：$f''(x)=6x-6$，$f''(0)=-6<0$ 故 $x=0$ 为极大值点；$f''(2)=6>0$ 故 $x=2$ 为极小值点。",
        "③ 代入：$f(2)=2^3-3\\times2^2+2=8-12+2=-2$。",
    ],
    "final_answer": "$-2$",
    "avoid_tips": ["求完驻点一定判断极大还是极小，别默认第一个驻点就是极小值点",
                   "代入时把三次项和二次项分开算，避免正负号出错"],
    "variants": [
        {"stem": "求函数 $g(x)=x^3-6x^2+9x+1$ 的极大值与极小值。",
         "answer": "极大值 $5$（$x=1$），极小值 $1$（$x=3$）",
         "hint": "$g'(x)=3(x-1)(x-3)$，再看二阶导数符号"},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 由服务器实例注入
    server: "MockCloudServer"

    def log_message(self, *args):  # 静音，避免污染验证输出
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server.mock
        srv.requests.append({"method": "GET", "path": self.path,
                             "auth": self.headers.get("Authorization", "")})
        if self.path.endswith("/models"):
            # 注意：这里**不强制**要求 Authorization。真实的 LM Studio 默认不需要 Key，
            # 早期版本强制校验会让「local.kind=lmstudio」的探活直接失败（假服务比真服务还严）。
            self._send(200, {"object": "list", "data": [
                {"id": srv.model, "object": "model"},
                {"id": "mock-text", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": f"no such path {self.path}"}})

    def do_POST(self):
        srv = self.server.mock
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            self._send(400, {"error": {"message": "invalid json"}})
            return
        srv.requests.append({"method": "POST", "path": self.path,
                             "auth": self.headers.get("Authorization", ""), "body": body})

        if srv.fail_times > 0:            # 模拟服务端故障
            srv.fail_times -= 1
            self._send(500, {"error": {"message": "mock internal error"}})
            return
        if srv.reject_format_times > 0 and "response_format" in body:
            srv.reject_format_times -= 1
            self._send(400, {"error": {"message": "response_format is not supported"}})
            return
        if srv.latency:
            time.sleep(srv.latency)

        blob = json.dumps(body.get("messages", []), ensure_ascii=False)
        payload = srv.analyze if "错题" in blob or "错误原因" in blob else srv.recognize
        text = json.dumps(payload, ensure_ascii=False)
        self._send(200, {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", srv.model),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801},
        })


class MockCloudServer(ThreadingHTTPServer):
    """带请求记录的假云端服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 8899, model: str = "mock-vl"):
        super().__init__(("127.0.0.1", port), _Handler)
        self.mock = self          # 处理器里用 self.server.mock 取配置
        self.model = model
        self.recognize = json.loads(json.dumps(DEFAULT_RECOGNIZE, ensure_ascii=False))
        self.analyze = json.loads(json.dumps(DEFAULT_ANALYZE, ensure_ascii=False))
        self.requests: list = []
        self.fail_times = 0
        self.reject_format_times = 0
        self.latency = 0.0


class MockCloud:
    """便捷包装：start()/stop()/requests/base_url。"""

    def __init__(self, port: int = 8899, model: str = "mock-vl"):
        self.port = port
        self._server = MockCloudServer(port, model)
        self._thread = None

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self) -> "MockCloud":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.25)
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    def __enter__(self) -> "MockCloud":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- 访问 -------------------------------------------------------------- #
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def requests(self) -> list:
        return self._server.requests

    @property
    def model(self) -> str:
        return self._server.model

    def clear(self) -> None:
        self._server.requests.clear()

    # -- 故障注入 ---------------------------------------------------------- #
    @property
    def fail_times(self) -> int:
        """接下来 N 次请求返回 500（用来验证界面的失败表现）。"""
        return self._server.fail_times

    @fail_times.setter
    def fail_times(self, value: int) -> None:
        self._server.fail_times = int(value)

    @property
    def reject_format_times(self) -> int:
        """接下来 N 次「带 response_format 的请求」返回 400（验证自动降级重试）。"""
        return self._server.reject_format_times

    @reject_format_times.setter
    def reject_format_times(self, value: int) -> None:
        self._server.reject_format_times = int(value)

    @property
    def latency(self) -> float:
        """每次请求的人为延迟（秒）。"""
        return self._server.latency

    @latency.setter
    def latency(self, value: float) -> None:
        self._server.latency = float(value)

    def set_recognize(self, payload: dict) -> None:
        """替换识别返回值（例如让模型返回空题目，测试失败处理）。"""
        self._server.recognize = payload

    def reset_recognize(self) -> None:
        self._server.recognize = json.loads(json.dumps(DEFAULT_RECOGNIZE, ensure_ascii=False))

    def last_post(self) -> dict:
        for req in reversed(self.requests):
            if req.get("method") == "POST":
                return req
        return {}


if __name__ == "__main__":  # 手动起一个，方便用 curl 试请求
    srv = MockCloud(port=8899).start()
    print(f"假云端已启动：{srv.base_url}")
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
