# -*- coding: utf-8 -*-
"""「模型有多少真的装在显存里」的回归测试（慢档检测 + 一键重载）。

为什么要单独测这块：这是接管复核时挖出来的**最贵的坑** ——
Ollama 只在**加载模型的那一刻**决定 GPU/CPU 分层，之后即使显存空出来也**不会**重新规划。
于是「模型是在显存紧张时加载的」这个事实，会让之后每一次识别都慢一个数量级，
而界面上完全看不出来：

| 状态 | 上卡比例 | 实测速度 |
| --- | --- | --- |
| 显存被占满时加载 | 451 MB / 4.2 GB ≈ 10% | 6.6 tok/s（单图 78 秒） |
| 卸载后重新加载 | 4.2 GB / 4.2 GB = 100% | 74~80 tok/s（单图 6.7 秒） |

同一份代码、同一张图，差 11 倍。所以这里守两件事：
1. ``_classify_resident`` 能把这个比例翻译成一句人话（阈值边界都测到）；
2. ``reload_model`` 真的走完「卸载 → 等它卸载干净 → 重新加载 → 回读比例」，
   而且**卸载没等干净就不许加载**（否则 Ollama 会复用旧 runner，重载等于白做）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from exerarchive import config as config_mod  # noqa: E402
from exerarchive.vlm import ModelClient, _classify_resident  # noqa: E402
from mock_ollama_server import MockOllama  # noqa: E402

SIZE = 3295636135          # 假 Ollama 报的模型大小（约 3.1 GB）


def _ps(size_vram: float, name: str = "qwen3-vl:4b") -> dict:
    return {"models": [{"name": name, "size": SIZE, "size_vram": int(size_vram)}]}


def _ps_exact(size: int, size_vram: int, name: str = "qwen3-vl:4b") -> dict:
    """精确比例用的小工具：边界值必须能整除，否则会被 int() 截断成 0.8999… 而测不到边界。"""
    return {"models": [{"name": name, "size": size, "size_vram": size_vram}]}


class ClassifyResidentTest(unittest.TestCase):
    """纯函数：把 /api/ps 的原始返回翻译成结论（不碰网络，边界值逐个测）。"""

    def test_01_full_offload_is_ok(self):
        st = _classify_resident(_ps(SIZE), "qwen3-vl:4b")
        self.assertEqual(st["level"], "ok")
        self.assertAlmostEqual(st["ratio"], 1.0, places=3)
        self.assertIn("全部装进显存", st["text"])
        self.assertEqual(st["hint"], "", "正常状态不该给建议")

    def test_02_exactly_at_threshold_is_ok(self):
        """90% 是 ok 的下边界（含）。"""
        self.assertEqual(_classify_resident(_ps_exact(1000, 900), "qwen3-vl:4b")["level"], "ok")
        self.assertEqual(_classify_resident(_ps_exact(1000, 899), "qwen3-vl:4b")["level"], "partial")

    def test_03_partial_offload_warns(self):
        st = _classify_resident(_ps(SIZE * 0.62), "qwen3-vl:4b")
        self.assertEqual(st["level"], "partial")
        self.assertIn("62%", st["text"])
        self.assertIn("重新加载模型", st["hint"], "必须告诉用户点哪里")

    def test_04_measured_bad_case_from_the_real_incident(self):
        """真实那次事故的原始数字：451789455 字节 / 4235345264 字节（约 10.7%）。"""
        st = _classify_resident({"models": [{"name": "qwen3-vl:4b",
                                             "size": 4235345264, "size_vram": 451789455}]},
                                "qwen3-vl:4b")
        self.assertEqual(st["level"], "bad")
        self.assertLess(st["ratio"], 0.11)
        self.assertIn("装在显存里", st["text"])
        self.assertIn("慢一个数量级", st["text"])
        self.assertIn("431 MB", st["text"], "字节要换算成 MB 再显示（451789455 B ≈ 431 MB）")

    def test_05_below_half_is_bad_above_is_partial(self):
        self.assertEqual(_classify_resident(_ps_exact(1000, 499), "m")["level"], "bad")
        self.assertEqual(_classify_resident(_ps_exact(1000, 500), "m")["level"], "partial")

    def test_06_not_resident_is_not_a_problem(self):
        st = _classify_resident({"models": []}, "qwen3-vl:4b")
        self.assertEqual(st["level"], "not_loaded")
        self.assertFalse(st["found"])
        self.assertEqual(st["hint"], "", "没加载不算毛病，不该报警")

    def test_07_picks_the_configured_model_when_several_resident(self):
        payload = {"models": [
            {"name": "llama3.2:3b", "size": 2019393189, "size_vram": 2019393189},
            {"name": "qwen3-vl:4b", "size": SIZE, "size_vram": int(SIZE * 0.2)},
        ]}
        st = _classify_resident(payload, "qwen3-vl:4b")
        self.assertEqual(st["model"], "qwen3-vl:4b")
        self.assertEqual(st["level"], "bad")
        # 没指定名字时退化成第一个，但绝不能报错
        self.assertEqual(_classify_resident(payload, "")["model"], "llama3.2:3b")

    def test_08_garbage_input_does_not_crash(self):
        for payload in ({}, {"models": None}, {"models": [{}]}, {"models": [{"size": 0}]}):
            st = _classify_resident(payload, "m")
            self.assertIn(st["level"], ("not_loaded", "ok", "partial", "bad"))
        st = _classify_resident({"models": [{"name": "m", "size": 0, "size_vram": 0}]}, "m")
        self.assertEqual(st["level"], "bad", "size 为 0 时不能除零，按 bad 处理")


class OffloadStatusOverHttpTest(unittest.TestCase):
    """走真实 HTTP（假 Ollama）读驻留状态。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp", "offload")
        os.makedirs(cls.tmp, exist_ok=True)
        os.environ["EXER_HOME"] = cls.tmp
        cls.ollama = MockOllama(port=11531).start()
        cls.cfg = config_mod.load_config()
        cls.cfg.set("local.kind", "ollama")
        cls.cfg.set("local.base_url", cls.ollama.base_url)
        cls.cfg.set("local.model", "qwen3-vl:4b")
        cls.client = ModelClient("local", cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.ollama.stop()
        os.environ.pop("EXER_HOME", None)

    def test_10_reads_ratio_from_api_ps(self):
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1
        st = self.client.manager.offload_status()
        self.assertTrue(st["found"])
        self.assertEqual(st["level"], "bad")
        self.assertAlmostEqual(st["ratio"], 0.1, places=2)

    def test_11_unsupported_backend_is_not_an_error(self):
        """LM Studio / 其它 OpenAI 兼容后端没有这个接口，必须优雅跳过而不是报错。"""
        cfg = config_mod.load_config()
        cfg.set("local.kind", "lmstudio")
        st = ModelClient("local", cfg).manager.offload_status()
        self.assertFalse(st["supported"])
        self.assertEqual(st["level"], "unsupported")
        self.assertIn("lmstudio", st["text"])


class ReloadModelTest(unittest.TestCase):
    """一键重载：慢档必须真的被修好，而且顺序不能错。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp", "reload")
        os.makedirs(cls.tmp, exist_ok=True)
        os.environ["EXER_HOME"] = cls.tmp
        cls.ollama = MockOllama(port=11532).start()
        cls.cfg = config_mod.load_config()
        cls.cfg.set("local.kind", "ollama")
        cls.cfg.set("local.base_url", cls.ollama.base_url)
        cls.cfg.set("local.model", "qwen3-vl:4b")
        cls.cfg.set("local.timeout", 30)
        cls.client = ModelClient("local", cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.ollama.stop()
        os.environ.pop("EXER_HOME", None)

    def setUp(self):
        self.ollama.clear()
        self.ollama.fail_times = 0
        self.ollama.load_ratio = 1.0

    def test_20_reload_fixes_the_slow_state(self):
        """慢档（10% 上卡）→ 重载后显存够 → 变成满速，并如实报出前后比例。"""
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1      # 当前是慢档
        self.ollama.load_ratio = 1.0            # 但重载时显存已经空出来了

        r = self.client.manager.reload_model()
        self.assertTrue(r["ok"], r["message"])
        self.assertAlmostEqual(r["before"]["ratio"], 0.1, places=2)
        self.assertAlmostEqual(r["after"]["ratio"], 1.0, places=2)
        self.assertIn("全部装进显存", r["message"])

        calls = self.ollama.generate_calls()
        self.assertGreaterEqual(len(calls), 2, "必须既有卸载请求、也有重新加载请求")
        self.assertEqual(calls[0]["body"].get("keep_alive"), 0, "第一步必须是卸载（keep_alive=0）")
        self.assertNotEqual(calls[-1]["body"].get("keep_alive"), 0, "最后一步必须是重新加载")
        self.assertEqual(self.ollama.load_count, 1, "只该加载一次")

    def test_21_reload_waits_until_unloaded_before_loading(self):
        """卸载与加载之间必须真的观察到「模型已消失」，否则 Ollama 会复用旧 runner。"""
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1
        self.ollama.load_ratio = 1.0
        self.client.manager.reload_model()
        seq = [(r["method"], str(r.get("path", "")).rsplit("/", 1)[-1]) for r in self.ollama.requests]
        # 卸载之后、加载之前，至少要有一次 /api/ps 的确认（GET）
        gen = [i for i, s in enumerate(seq) if s[1] == "generate"]
        ps_between = [i for i, s in enumerate(seq) if s[1] == "ps" and gen[0] < i < gen[-1]]
        self.assertTrue(ps_between, f"卸载与加载之间没有确认卸载状态：{seq}")

    def test_22_reload_reports_honestly_when_vram_still_tight(self):
        """显存仍然不够时要如实说「还是不行」，不能假装成功。"""
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1
        self.ollama.load_ratio = 0.15           # 重载后依然只有 15%
        r = self.client.manager.reload_model()
        self.assertFalse(r["ok"])
        self.assertIn("15%", r["message"])
        self.assertIn("仍不够", r["message"], "没修好就必须如实说，不能假装成功")

    def test_23_reload_survives_unload_failure(self):
        """卸载请求失败也要继续尝试重载（不能因为一步失败就整个功能不可用）。"""
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1
        self.ollama.fail_times = 1              # 第一个 POST（卸载）返回 500
        self.ollama.load_ratio = 1.0
        r = self.client.manager.reload_model()
        self.assertTrue(r["ok"], f"卸载失败不该让重载整体失败：{r['message']}")

    def test_24_reload_on_non_ollama_backend_gives_actionable_message(self):
        cfg = config_mod.load_config()
        cfg.set("local.kind", "lmstudio")
        r = ModelClient("local", cfg).manager.reload_model()
        self.assertFalse(r["ok"])
        self.assertIn("LM Studio", r["message"])


class SlowWarningTest(unittest.TestCase):
    """识别偏慢时要在日志里说清楚原因（而不是让用户以为软件坏了）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp", "slowwarn")
        os.makedirs(cls.tmp, exist_ok=True)
        os.environ["EXER_HOME"] = cls.tmp
        cls.ollama = MockOllama(port=11533).start()
        cls.cfg = config_mod.load_config()
        cls.cfg.set("local.kind", "ollama")
        cls.cfg.set("local.base_url", cls.ollama.base_url)
        cls.cfg.set("local.model", "qwen3-vl:4b")
        cls.client = ModelClient("local", cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.ollama.stop()
        os.environ.pop("EXER_HOME", None)

    def setUp(self):
        from exerarchive import vlm

        vlm._SLOW_WARNED = False        # 每个用例都从"还没提示过"开始
        self.ollama.resident = True
        self.ollama.size_vram_ratio = 0.1

    def _fake(self, tok_per_sec: float):
        from exerarchive.vlm import ChatResult

        res = ChatResult(text="{}", ok=True, elapsed=1.0, output_tokens=max(1, int(tok_per_sec)))
        return res

    def test_30_slow_and_partially_offloaded_warns_with_action(self):
        lines = []
        self.client._maybe_warn_slow(self._fake(6.6), lines.append)
        joined = "\n".join(lines)
        self.assertIn("6.6 tok/s", joined)
        self.assertIn("只有 10% 装在显存里", joined)
        self.assertIn("重新加载模型", joined, "提示里必须给出可操作的办法")

    def test_31_fast_but_partially_offloaded_stays_quiet(self):
        lines = []
        self.client._maybe_warn_slow(self._fake(75.0), lines.append)
        self.assertEqual(lines, [], "速度正常就不要打扰用户")

    def test_32_slow_but_fully_offloaded_stays_quiet(self):
        """本来就慢的机器（模型已全部上卡、可能纯 CPU）不该被误报。"""
        self.ollama.size_vram_ratio = 1.0
        lines = []
        self.client._maybe_warn_slow(self._fake(4.0), lines.append)
        self.assertEqual(lines, [], "上卡比例正常时不许误报")

    def test_33_warns_only_once_per_run(self):
        first, second = [], []
        self.client._maybe_warn_slow(self._fake(6.0), first.append)
        self.client._maybe_warn_slow(self._fake(6.0), second.append)
        self.assertTrue(first)
        self.assertEqual(second, [], "批量识别时不能每张图都刷一遍同样的提示")


if __name__ == "__main__":
    unittest.main()
