# -*- coding: utf-8 -*-
"""命令行输出的编码回归测试：中文 Windows 下重定向/管道不能把程序打断。

真实缺陷（接手项目时复现）：``python tools\\selfcheck.py --no-model > 报告.txt``
在中文 Windows 上直接崩 —— 自检报告里有 ``✅/⚠️/❌``，而 ``sys.stdout`` 一旦不是
真正的控制台（被重定向或接进管道），Python 就退回本地编码 cp936/GBK，
打印 ``✅`` 抛 ``UnicodeEncodeError``：用户拿到的是一个**空文件**加一段 traceback，
退出码还是 1。当时只在 ``--json`` 分支里做了 ``reconfigure``，人类可读的
``Report.print()`` 这条主路径漏了。

修法：``logging_setup.enable_utf8_console()`` 统一把 stdout/stderr 切成
UTF-8 + ``errors="replace"``，并挂进 ``logging_setup.setup()``（所有 CLI 工具都会调它）。

这两个用例分别守两件事：
1. 助手本身真的改了编码，且 ``pythonw``（stdout 为 None）下不炸；
2. 真起一个子进程、真用 GBK 管道收输出 —— 也就是用户重定向到文件的那条路。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import logging_setup  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOJI = "✅"          # U+2705：GBK 编不出来，正是当年崩掉的那个字符


class Utf8ConsoleHelperTest(unittest.TestCase):
    """助手本身的直接测试（不依赖子进程，快）。"""

    def test_01_gbk_stream_is_switched_to_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk")
        saved = sys.stdout
        sys.stdout = stream
        try:
            # 换之前：GBK 编不出 ✅，必须抛错（否则这个用例就没在测真问题）
            with self.assertRaises(UnicodeEncodeError):
                stream.write(EMOJI)
                stream.flush()

            logging_setup.enable_utf8_console()
            self.assertEqual(stream.encoding.lower(), "utf-8")
            stream.write(EMOJI)          # 换之后必须能写
            stream.flush()
        finally:
            sys.stdout = saved
        self.assertIn(EMOJI, raw.getvalue().decode("utf-8"))

    def test_02_unencodable_char_degrades_instead_of_crashing(self):
        """errors="replace" 兜底：即使编码没换成（reconfigure 不被支持），也不能抛异常。"""
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="gbk")
        saved = sys.stdout
        sys.stdout = stream
        try:
            logging_setup.enable_utf8_console()
            self.assertEqual(stream.errors, "replace")
        finally:
            sys.stdout = saved

    def test_03_none_stream_is_tolerated(self):
        """pythonw 启动 GUI 时 sys.stdout 是 None，助手必须安静跳过而不是抛 AttributeError。"""
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = None
        try:
            logging_setup.enable_utf8_console()      # 不抛异常即通过
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err


class GbkPipeSubprocessTest(unittest.TestCase):
    """端到端：真子进程 + GBK 管道，等价于用户 ``> 报告.txt`` 或 ``| findstr``。"""

    def _run(self, code: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"       # 强制退回中文 Windows 的本地编码
        env["PYTHONPATH"] = ROOT
        env.pop("PYTHONUTF8", None)           # 别让 PYTHONUTF8=1 把这个场景掩盖掉
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_04_setup_makes_emoji_printable_through_gbk_pipe(self):
        proc = self._run(
            "from exerarchive import logging_setup\n"
            "logging_setup.setup()\n"
            f"print('{EMOJI} 中文正常')\n"
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"子进程失败：{err[-500:]}")
        self.assertIn(EMOJI, out, "UTF-8 输出里必须能拿到 ✅")
        self.assertNotIn("UnicodeEncodeError", err)

    def test_05_selfcheck_report_survives_gbk_pipe(self):
        """自检脚本的人类可读报告（当年崩的那条路径）必须能整段打印出来。

        用 ``--no-model`` 跳过模型实测，只走环境/存储/图像/导出检查，几秒内结束。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"
        env.pop("PYTHONUTF8", None)
        proc = subprocess.run([sys.executable, os.path.join("tools", "selfcheck.py"), "--no-model"],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=300)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        self.assertNotIn("UnicodeEncodeError", err, "自检报告又因为编码崩了")
        self.assertIn("环境自检", out, "报告正文没打出来（很可能是编码崩在中途）")
        self.assertIn("结论：", out, "报告没有打印到最后一行")


if __name__ == "__main__":
    unittest.main()
