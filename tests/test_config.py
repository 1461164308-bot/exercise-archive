# -*- coding: utf-8 -*-
"""配置合并语义回归（用户反馈「设置改了却不生效」的根因就在 Config.update）。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests._util import scratch, cleanup
from exerarchive.config import Config


class ConfigUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(scratch("config_update"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        cleanup(str(self.tmp))

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_dotted_key_update_writes_the_nested_path(self):
        """``update({"run.x": v})`` 必须按**路径**写，而不是造一个顶层影子键。"""
        cfg = Config({}, self.path)
        cfg.update({"run.confidence_threshold": 0.55, "ui.math_scale": 1.4})
        self.assertEqual(cfg.get("run.confidence_threshold"), 0.55)
        self.assertEqual(cfg.get("ui.math_scale"), 1.4)
        self.assertNotIn("run.confidence_threshold", cfg.as_dict(),
                         "又写出了影子键：get() 读的是嵌套段，这个键永远不会生效")
        cfg.save()
        on_disk = self.read()
        self.assertNotIn("run.confidence_threshold", on_disk)
        self.assertNotIn("ui.math_scale", on_disk)
        self.assertEqual(on_disk["run"]["confidence_threshold"], 0.55)
        self.assertEqual(on_disk["ui"]["math_scale"], 1.4)

    def test_nested_patch_still_deep_merges(self):
        """嵌套补丁（如 DEFAULTS）仍要递归合并，不能把同段其它键清掉。"""
        cfg = Config({}, self.path)
        cfg.update({"run": {"auto_analyze": False}})
        self.assertFalse(cfg.get("run.auto_analyze"))
        self.assertEqual(cfg.get("run.mode"), "hybrid", "嵌套合并把同段其它键弄丢了")
        cfg.update({"cloud": {"enabled": True}})
        self.assertTrue(cfg.get("cloud.enabled"))
        self.assertTrue(cfg.get("cloud.profiles"), "嵌套合并不该丢掉列表")

    def test_dotted_and_nested_can_be_mixed_in_one_patch(self):
        cfg = Config({}, self.path)
        cfg.update({"run.auto_analyze": False, "privacy": {"encrypt_db": True}})
        self.assertFalse(cfg.get("run.auto_analyze"))
        self.assertTrue(cfg.get("privacy.encrypt_db"))

    def test_shadow_keys_in_existing_files_are_dropped(self):
        """老配置文件里已经有影子键：加载时清掉，并以嵌套值为准。"""
        self.path.write_text(json.dumps({"run.confidence_threshold": 0.1,
                                         "run": {"confidence_threshold": 0.9}}),
                             encoding="utf-8")
        cfg = Config(self.read(), self.path)
        self.assertEqual(cfg.get("run.confidence_threshold"), 0.9, "嵌套值才是生效值")
        self.assertNotIn("run.confidence_threshold", cfg.as_dict())
        cfg.save()
        self.assertNotIn("run.confidence_threshold", self.read())

    def test_settings_patch_shape_roundtrips_through_disk(self):
        """模拟设置对话框 ``_collect()`` 的补丁形状（全是扁平点号键）：每一项都要落地。"""
        cfg = Config({}, self.path)
        patch = {"run.mode": "local_only", "run.confidence_threshold": 0.5,
                 "run.quality_threshold": 0.2, "run.auto_analyze": False,
                 "run.max_questions_per_image": 6, "local.model": "qwen3-vl:4b",
                 "privacy.encrypt_db": True, "archive.pattern": "{subject}/{qtype}",
                 "ui.math_supersample": 3, "ui.animations": False}
        cfg.update(patch)
        cfg.save()
        again = Config(self.read(), self.path)
        for key, value in patch.items():
            self.assertEqual(again.get(key), value, "补丁项没有生效：%s" % key)

    def test_defaults_restore_patch_works(self):
        """「恢复默认」走的是嵌套 DEFAULTS，必须能把改动过的值改回去。"""
        from exerarchive.config import DEFAULTS

        cfg = Config({}, self.path)
        cfg.update({"run.confidence_threshold": 0.11, "local.model": "别的模型"})
        cfg.update({k: v for k, v in DEFAULTS.items() if k != "cloud"})
        cfg.update({"cloud": DEFAULTS["cloud"]})
        self.assertEqual(cfg.get("run.confidence_threshold"),
                         DEFAULTS["run"]["confidence_threshold"])
        self.assertEqual(cfg.get("local.model"), DEFAULTS["local"]["model"])


if __name__ == "__main__":
    unittest.main()
