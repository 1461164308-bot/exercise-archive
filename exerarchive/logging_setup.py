# -*- coding: utf-8 -*-
"""日志：同时写入 ``data/logs/app.log``（滚动，最多 3 个 1MB 文件）与内存环形缓冲。

内存缓冲供 GUI 的「运行日志」面板实时显示，方便小白用户排查问题。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque

from . import paths

_RING: deque = deque(maxlen=500)
_RING_LOCK = threading.Lock()
_CONFIGURED = False


class _RingHandler(logging.Handler):
    """把日志追加到内存环形缓冲，并回调 GUI（若有注册）。"""

    def __init__(self):
        super().__init__()
        self.listeners = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with _RING_LOCK:
            _RING.append(msg)
        for cb in list(self.listeners):
            try:
                cb(msg)
            except Exception:
                pass


_ring_handler = _RingHandler()


def enable_utf8_console() -> None:
    """让标准输出/标准错误用 UTF-8，避免中文与 ✅ 在中文 Windows 上把程序打断。

    背景：``sys.stdout`` 连着真正的控制台时是 UTF-8（PEP 528），
    但只要被**重定向或接进管道**（``python tools\\selfcheck.py > 报告.txt``、
    ``... | findstr RESULT``），Python 就退回本地编码（cp936/GBK），
    此时打印 ``✅`` 会抛 ``UnicodeEncodeError`` —— 用户拿到的是一个空文件和一段
    traceback，而不是自检报告。

    这里统一改成 ``utf-8`` + ``errors="replace"``：万一真有编不出的字符，
    降级成 ``?`` 就好，绝不因为一个符号把整条诊断流程打断。
    在 pythonw（无控制台，``sys.stdout is None``）下直接跳过，GUI 不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:          # pythonw / GUI 子系统
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")   # 退一步：至少不再抛异常
            except Exception:
                pass


def setup(level: int = logging.INFO) -> None:
    """初始化全局日志（重复调用无副作用）。"""
    enable_utf8_console()
    global _CONFIGURED
    if _CONFIGURED:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    try:
        fh = logging.handlers.RotatingFileHandler(
            paths.log_file(), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except Exception:
        pass  # 日志文件写不了也不能影响主程序

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(level)
    root.addHandler(ch)

    _ring_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    _ring_handler.setLevel(level)
    root.addHandler(_ring_handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)


def tail(n: int = 200) -> str:
    """读取内存里最近的日志（GUI 用）。"""
    with _RING_LOCK:
        items = list(_RING)[-n:]
    return "\n".join(items)


def add_listener(cb) -> None:
    """注册日志回调 cb(str)；GUI 关闭时用 remove_listener 注销。"""
    _ring_handler.listeners.append(cb)


def remove_listener(cb) -> None:
    try:
        _ring_handler.listeners.remove(cb)
    except ValueError:
        pass


def read_log_file(max_chars: int = 200000) -> str:
    """读取磁盘日志文件尾部（「打开日志」用）。"""
    try:
        text = paths.log_file().read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "（暂无日志文件）"
    return text[-max_chars:]
