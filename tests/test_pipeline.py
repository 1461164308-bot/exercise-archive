# -*- coding: utf-8 -*-
"""识别流水线与分析器测试（用假模型，不依赖真实算力）。

覆盖：
- 模型输出脏数据（代码块、尾逗号、中文键名、截断、confidence 写成 90）的归一化；
- 本地识别 → 入库 → 归档 的完整链路；
- hybrid 模式下「置信度低 / 图片模糊 / 本地失败」三种情况是否真的会转云端；
- local_only 模式绝不调用云端；
- 云端与本地结果合并（云端优先、本地补空缺）；
- 错题分析的规则兜底报告结构完整、且报告能被导出渲染。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import config, gdiplus, imageops  # noqa: E402
from exerarchive.analysis import (Analyzer, normalize_report, question_to_text,  # noqa: E402
                                  report_to_text, rule_based_report)
from exerarchive.pipeline import Pipeline, merge_results, normalize_result  # noqa: E402
from exerarchive.prompts import normalize_grade, normalize_qtype, normalize_status, normalize_subject  # noqa: E402
from exerarchive.vlm import ChatResult, extract_json  # noqa: E402
from tests._util import cleanup, scratch  # noqa: E402

LOCAL_JSON = """```json
{
  "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
  "questions": [
    {"index": 1, "type": "解答题", "status": "wrong",
     "stem": "计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
     "options": [], "student_answer": "7", "student_steps": "展开后得 7",
     "correct_answer": "-3", "knowledge_points": ["行列式"],
     "difficulty": "中等", "confidence": 0.55},
  ]
}
```"""

CLOUD_JSON = json.dumps({
    "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
    "questions": [
        {"index": 1, "type": "解答题", "status": "wrong", "stem": "计算三阶行列式 D 的值。",
         "student_answer": "7", "correct_answer": "-3",
         "knowledge_points": ["行列式", "对角线法则"], "confidence": 0.95},
    ],
}, ensure_ascii=False)


class FakeClient:
    """假模型客户端：记录调用次数并按脚本返回结果。"""

    calls = []

    def __init__(self, target, cfg, script=None):
        self.target = target
        self.cfg = cfg
        self.script = script or {}

    def engine_name(self):
        return f"{self.target}:fake"

    def is_configured(self):
        return True

    def chat(self, prompt, images=None, system="", json_mode=True, timeout=None, on_log=None, **kw):
        FakeClient.calls.append((self.target, len(images or [])))
        text = self.script.get(self.target, "")
        if isinstance(text, Exception):
            return ChatResult(engine=self.engine_name(), ok=False, error=str(text))
        return ChatResult(text=text, engine=self.engine_name(), ok=bool(text),
                          output_tokens=42, elapsed=0.5)


def make_cfg(**over):
    cfg = config.Config({})
    cfg.set("local.model", "fake-vl")
    cfg.set("local.auto_start", False)
    cfg.set("run.auto_preprocess", False)
    for k, v in over.items():
        cfg.set(k, v)
    return cfg


class NormalizeTest(unittest.TestCase):
    def test_01_extract_json_variants(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('前言 ```json\n{"a": 1}\n``` 后记'), {"a": 1})
        self.assertEqual(extract_json('{"a": 1,}'), {"a": 1})           # 尾逗号
        self.assertEqual(extract_json('{"a": "x", "b": [1,2,]}'), {"a": "x", "b": [1, 2]})
        self.assertEqual(extract_json('{"a": 1}\n{"a": 2}'), {"a": 1})   # 取第一个完整对象
        self.assertEqual(extract_json('{"a": "少了结尾"'), {"a": "少了结尾"})  # 截断自动补齐
        self.assertIsNone(extract_json("完全不是 JSON"))
        self.assertIsNone(extract_json(""))

    def test_02_field_normalization(self):
        self.assertEqual(normalize_subject("高中数学"), "数学")
        self.assertEqual(normalize_subject("English"), "英语")
        self.assertEqual(normalize_subject(""), "其他")
        self.assertEqual(normalize_status("错题（打叉）"), "wrong")
        self.assertEqual(normalize_status("✓"), "correct")
        self.assertEqual(normalize_status("未作答"), "blank")
        self.assertEqual(normalize_qtype("单项选择"), "选择题")
        self.assertEqual(normalize_qtype("解答题（12分）"), "解答题")
        self.assertEqual(normalize_grade("大一"), "大学")
        self.assertEqual(normalize_grade("高三"), "高中")

    def test_03_normalize_result(self):
        """小模型的脏输出也要能被救回来。"""
        obj = extract_json(LOCAL_JSON)
        norm = normalize_result(obj, "local:fake", max_questions=8)
        self.assertEqual(norm["subject"], "数学")
        self.assertEqual(norm["subject_detail"], "线性代数")
        self.assertEqual(len(norm["questions"]), 1)
        q = norm["questions"][0]
        self.assertEqual(q["status"], "wrong")
        self.assertEqual(q["qtype"], "解答题")
        self.assertAlmostEqual(q["confidence"], 0.55)

        # 中文键名 + confidence 写成百分数 + 单题无 questions 包裹
        messy = {"学科": "物理", "题目": [{"题干": "求加速度", "状态": "错", "置信度": 90,
                                        "知识点": "牛顿第二定律"}]}
        norm2 = normalize_result(messy, "local:fake")
        self.assertEqual(norm2["subject"], "物理")
        self.assertEqual(len(norm2["questions"]), 1)
        self.assertAlmostEqual(norm2["questions"][0]["confidence"], 0.9)

        # 空输入
        self.assertEqual(normalize_result(None, "x")["questions"], [])
        self.assertEqual(normalize_result({"questions": []}, "x")["questions"], [])

    def test_04_merge_local_cloud(self):
        local = normalize_result(extract_json(LOCAL_JSON), "local:fake")
        cloud = normalize_result(extract_json(CLOUD_JSON), "cloud:fake")
        merged = merge_results(local, cloud)
        self.assertEqual(len(merged["questions"]), 1)
        q = merged["questions"][0]
        self.assertAlmostEqual(q["confidence"], 0.95)          # 置信度用云端的
        self.assertIn("行列式", q["knowledge_points"])
        self.assertIn("D", q["stem"])                           # 题干取信息量更大的
        self.assertEqual(merged["grade"], "大学")


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch("pipeline")
        cls.img = os.path.join(cls.tmp, "page.png")
        if gdiplus.available():
            w, h, px = gdiplus.draw_text_bitmap(
                900, 600, lines=["1. 计算行列式 D 的值", "学生作答：7"], font_size=30)
            imageops.Image(w, h, px).save(cls.img)
        else:  # 无 GDI+ 时用纯 Python 编码器造图
            imageops.encode_png(200, 100, bytes([255] * (200 * 100 * 3)))
            with open(cls.img, "wb") as f:
                f.write(imageops.encode_png(200, 100, bytes([255] * (200 * 100 * 3))))

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def setUp(self):
        FakeClient.calls = []

    def _pipeline(self, cfg, script):
        def factory(target, cfg_):
            return FakeClient(target, cfg_, script)

        with mock.patch("exerarchive.pipeline.ModelClient", side_effect=factory):
            with mock.patch("exerarchive.analysis.ModelClient", side_effect=factory):
                return Pipeline(cfg, on_log=lambda m: None)

    def test_10_local_only(self):
        cfg = make_cfg(**{"run.mode": "local_only", "cloud.enabled": True,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": "sk-test"}]})
        pipe = self._pipeline(cfg, {"local": LOCAL_JSON, "cloud": CLOUD_JSON})
        out = pipe.process_image(self.img)
        self.assertEqual(len(out.questions), 1)
        self.assertEqual(out.route, "local")
        self.assertEqual(out.questions[0]["status"], "wrong")
        self.assertEqual(out.questions[0]["subject"], "数学")
        self.assertTrue(all(c[0] != "cloud" for c in FakeClient.calls), "纯本地模式不能调用云端")

    def test_11_hybrid_low_confidence_triggers_cloud(self):
        cfg = make_cfg(**{"run.mode": "hybrid", "cloud.enabled": True,
                          "run.confidence_threshold": 0.8,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": "sk-test"}]})
        pipe = self._pipeline(cfg, {"local": LOCAL_JSON, "cloud": CLOUD_JSON})
        out = pipe.process_image(self.img)
        self.assertEqual(out.route, "local→cloud", "低置信度应当触发云端兜底")
        self.assertEqual(out.questions[0]["confidence"], 0.95)
        self.assertTrue(any(c[0] == "cloud" for c in FakeClient.calls))

    def test_12_hybrid_high_confidence_stays_local(self):
        high = LOCAL_JSON.replace("0.55", "0.97")
        cfg = make_cfg(**{"run.mode": "hybrid", "cloud.enabled": True,
                          "run.confidence_threshold": 0.7,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": "sk-test"}]})
        pipe = self._pipeline(cfg, {"local": high, "cloud": CLOUD_JSON})
        out = pipe.process_image(self.img)
        self.assertEqual(out.route, "local")
        self.assertFalse(any(c[0] == "cloud" for c in FakeClient.calls), "高置信度不该花钱调云端")

    def test_13_cloud_first_and_fallback(self):
        cfg = make_cfg(**{"run.mode": "cloud_first", "cloud.enabled": True,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": "sk-test"}]})
        pipe = self._pipeline(cfg, {"local": LOCAL_JSON, "cloud": CLOUD_JSON})
        out = pipe.process_image(self.img)
        self.assertEqual(out.route, "cloud")
        self.assertEqual(FakeClient.calls[0][0], "cloud", "云端优先模式应先调云端")

        # 云端失败 → 自动回退本地（route 明确标成 "cloud→local"，便于排查与归档留痕）
        FakeClient.calls = []
        pipe2 = self._pipeline(cfg, {"local": LOCAL_JSON, "cloud": RuntimeError("网络不可达")})
        out2 = pipe2.process_image(self.img)
        self.assertEqual(len(out2.questions), 1)
        self.assertEqual(out2.route, "cloud→local")
        self.assertTrue(str(out2.engine).startswith("local:"), out2.engine)

    def test_14_local_failure_falls_back_to_cloud(self):
        cfg = make_cfg(**{"run.mode": "hybrid", "cloud.enabled": True,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": "sk-test"}]})
        pipe = self._pipeline(cfg, {"local": RuntimeError("模型未启动"), "cloud": CLOUD_JSON})
        out = pipe.process_image(self.img)
        self.assertEqual(out.route, "local→cloud")
        self.assertEqual(len(out.questions), 1)

    def test_15_no_questions_marks_error(self):
        cfg = make_cfg(**{"run.mode": "local_only"})
        pipe = self._pipeline(cfg, {"local": "抱歉，我看不到图片内容。"})
        out = pipe.process_image(self.img)
        self.assertFalse(out.questions)
        self.assertTrue(out.error)
        self.assertEqual(out.question_count, 0)
        self.assertIn("没有识别出", out.error)

    def test_16_unknown_status_becomes_blank(self):
        """模型判不出对错时按“未做题”归档，避免被误当作错题去分析。"""
        payload = json.dumps({"subject": "数学", "questions": [
            {"index": 1, "type": "填空题", "status": "unknown", "stem": "1+1=__",
             "confidence": 0.9}]}, ensure_ascii=False)
        cfg = make_cfg(**{"run.mode": "local_only"})
        pipe = self._pipeline(cfg, {"local": payload})
        out = pipe.process_image(self.img)
        self.assertEqual(out.questions[0]["status"], "blank")
        self.assertTrue(out.questions[0].get("status_guessed"))

    def test_17_cloud_first_without_key_blames_cloud_not_local(self):
        """只想用云端、但云端没配好时，提示必须指向**云端**。

        目标读者是"没有显卡 / 不想装本地模型"的用户：他们选了「云端优先」却没填
        API Key 时，如果只看到「本地模型服务不可用」，会以为是自己电脑缺东西。
        """
        logs = []
        cfg = make_cfg(**{"run.mode": "cloud_first", "cloud.enabled": True,
                          "cloud.profiles": [{"name": "t", "base_url": "https://x/v1",
                                              "model": "m", "api_key": ""}]})
        self.assertFalse(cfg.cloud_enabled(), "没填 Key 时 cloud_enabled 应为 False")
        with mock.patch("exerarchive.pipeline.ModelClient",
                        side_effect=lambda target, cfg_: FakeClient(target, cfg_, {"local": ""})):
            pipe = Pipeline(cfg, on_log=logs.append)
        out = pipe.process_image(self.img)
        self.assertFalse(out.questions)
        self.assertIn("云端", out.error, "错误信息应当说明是云端没配好：%r" % out.error)
        self.assertIn("设置", out.error, "错误信息要给出下一步动作：%r" % out.error)
        self.assertTrue(any("云端当前不可用" in line for line in logs), logs)


class AnalysisTest(unittest.TestCase):
    def _q(self, **over):
        q = {"uid": "Q1", "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
             "qtype": "解答题", "status": "wrong",
             "stem": "计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
             "student_answer": "7", "student_steps": "展开得 7", "correct_answer": "-3",
             "knowledge_points": ["行列式"]}
        q.update(over)
        return q

    def test_20_rule_based_report_is_complete(self):
        rep = rule_based_report(self._q())
        for key in ("restate", "causes", "knowledge_points", "solution_steps",
                    "final_answer", "avoid_tips", "variants"):
            self.assertTrue(rep.get(key), f"规则报告缺少字段 {key}")
        self.assertEqual(rep["depth"], "rule")
        self.assertIn("-3", rep["final_answer"])
        self.assertTrue(any(c["type"] in
                            ("审题失误", "公式记错", "知识点盲区", "计算失误", "思路偏差",
                             "粗心大意", "书写不规范", "时间不够")
                            for c in rep["causes"]))
        self.assertEqual(rep["knowledge_points"][0]["name"], "行列式")
        text = report_to_text(self._q(), rep)
        for seg in ("错题还原", "错误原因分析", "对应知识点", "标准解题步骤", "避错技巧", "举一反三"):
            self.assertIn(seg, text)

    def test_21_blank_question_cause(self):
        rep = rule_based_report(self._q(status="blank", student_answer="", student_steps=""))
        self.assertTrue(any(c["type"] == "知识点盲区" for c in rep["causes"]))

    def test_22_normalize_model_report(self):
        """模型返回的报告字段缺失/格式不对时，用规则版补齐。"""
        messy = {"restate": "第 1 题：行列式。学生写 7，正确 -3。",
                 "causes": [{"type": "公式记错", "detail": "符号写反"}],
                 "knowledge_points": ["二阶行列式", {"name": "符号法则", "note": "注意正负"}],
                 "solution_steps": "第一步 写公式\n第二步 代入",
                 "final_answer": "-3",
                 "avoid_tips": "先写公式",
                 "variants": [{"question": "计算 |2 1; 3 4|", "answer": "5"}]}
        rep = normalize_report(messy, self._q(), "cloud", "cloud:test")
        self.assertEqual(rep["depth"], "cloud")
        self.assertEqual(len(rep["causes"]), 1)
        self.assertEqual(len(rep["knowledge_points"]), 2)
        self.assertEqual(rep["knowledge_points"][1]["name"], "符号法则")
        self.assertGreaterEqual(len(rep["solution_steps"]), 1)
        self.assertTrue(rep["variants"][0]["stem"])

    def test_23_analyzer_falls_back_to_rules_without_model(self):
        cfg = make_cfg(**{"run.mode": "local_only"})
        with mock.patch("exerarchive.analysis.ModelClient",
                        side_effect=lambda target, cfg_: FakeClient(target, cfg_, {"local": "不是 JSON"})):
            analyzer = Analyzer(cfg, on_log=lambda m: None)
            rep = analyzer.analyze(self._q(), allow_cloud=False)
        self.assertEqual(rep["depth"], "rule", "模型输出不可解析时应回退规则版")

    def test_24_question_to_text(self):
        text = question_to_text(self._q())
        self.assertIn("计算三阶行列式", text)
        self.assertIn("我的答案" if "我的答案" in text else "学生答案", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
