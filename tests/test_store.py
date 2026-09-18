# -*- coding: utf-8 -*-
"""数据层测试：加密读写、加密全文检索、统计、备份。

运行：``python -m unittest tests.test_store -v``（在项目根目录执行）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive.store import Store  # noqa: E402
from tests._util import cleanup, scratch  # noqa: E402

Q1 = {
    "index": 1, "type": "选择题", "status": "wrong",
    "stem": "已知函数 f(x)=x^2+2x，则 f(1)=(  )",
    "options": ["A. 2", "B. 3", "C. 4"],
    "student_answer": "B", "correct_answer": "B",
    "knowledge_points": ["导数", "函数值"],
    "confidence": 0.88, "subject": "数学", "subject_detail": "微积分",
    "grade": "大学", "qtype": "选择题",
}
Q2 = {
    "index": 2, "type": "填空题", "status": "blank",
    "stem": "计算定积分 ∫₀¹ x² dx = ______",
    "knowledge_points": ["定积分"], "confidence": 0.62,
    "subject": "数学", "grade": "大学", "qtype": "填空题",
}


class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch("store")
        cls.db = os.path.join(cls.tmp, "t.db")
        cls.st = Store(cls.db, encrypt=True)
        cls.img = cls.st.add_image("x.png", source="单元测试", batch="B1")
        cls.iid = cls.st.image_id(cls.img)
        cls.q1 = cls.st.add_question(Q1, cls.iid, "x.png")
        cls.q2 = cls.st.add_question(Q2, cls.iid, "x.png")

    @classmethod
    def tearDownClass(cls):
        cls.st.close()
        cleanup(cls.tmp)

    def test_01_write_read(self):
        q = self.st.get_question(self.q1)
        self.assertEqual(q["stem"], Q1["stem"])
        self.assertEqual(q["options"], Q1["options"])
        self.assertEqual(q["knowledge_points"], ["导数", "函数值"])
        self.assertEqual(q["status"], "wrong")
        self.assertEqual(q["student_answer"], "B")

    def test_02_ciphertext_on_disk(self):
        """正文必须不以明文形式落盘。"""
        with open(self.db, "rb") as f:
            raw = f.read()
        self.assertNotIn("已知函数".encode("utf-8"), raw)
        self.assertNotIn(Q1["student_answer"].encode("utf-8"), raw)

    def test_03_search_fulltext(self):
        for kw in ("积分", "函数", "导数", "x^2", "定积分", "f(1)"):
            hits = [q["uid"] for q in self.st.search(keyword=kw)]
            self.assertIn(self.q1 if kw != "定积分" else self.q2, hits, f"关键词 {kw} 应能搜到")
        # 2 字与 3 字、英文数字都要覆盖
        self.assertEqual(self.st.search(keyword="量子力学"), [])

    def test_04_search_single_char_and_multi_term(self):
        self.assertIn(self.q1, [q["uid"] for q in self.st.search(keyword="函")])
        both = [q["uid"] for q in self.st.search(keyword="函数 导数")]
        self.assertEqual(both, [self.q1])
        # 两个词分别落在不同题目上时必须为空（AND 语义）
        self.assertEqual(self.st.search(keyword="函数 不定积分"), [])

    def test_05_filters(self):
        self.assertEqual([q["uid"] for q in self.st.search(status="wrong")], [self.q1])
        self.assertEqual([q["uid"] for q in self.st.search(qtype="填空题")], [self.q2])
        self.assertEqual(self.st.search(subject="英语"), [])
        self.assertEqual([q["uid"] for q in self.st.search(keyword="积分", qtype="填空题")], [self.q2])

    def test_06_report_roundtrip(self):
        report = {
            "restate": "学生把行列式展开的符号写错了",
            "causes": [{"type": "公式记错", "detail": "符号规则记反"}],
            "knowledge_points": [{"name": "二阶行列式", "textbook": "线性代数", "note": "注意符号"}],
            "solution_steps": ["第一步：写出行列式", "第二步：主对角线减副对角线"],
            "final_answer": "-2", "avoid_tips": ["牢记符号"],
            "variants": [{"stem": "变式题", "answer": "1", "hint": "提示"}],
        }
        self.st.save_report(self.q1, report, depth="cloud", engine="cloud:test")
        got = self.st.get_report(self.q1)
        self.assertEqual(got["final_answer"], "-2")
        self.assertEqual(got["_causes"], ["公式记错"])
        self.assertEqual(got["_knowledge"], ["二阶行列式"])
        self.assertEqual(got["_depth"], "cloud")
        # 报告正文也要能搜到
        self.assertIn(self.q1, [q["uid"] for q in self.st.search(keyword="行列式")])

    def test_07_stats(self):
        s = self.st.stats()
        self.assertEqual(s["questions"], 2)
        self.assertEqual(s["wrong"], 1)
        self.assertEqual(s["blank"], 1)
        self.assertEqual(s["by_subject"].get("数学"), 2)
        self.assertEqual(s["by_type"].get("选择题"), 1)
        self.assertIn("导数", s["weak_knowledge"])
        self.assertEqual(s["cause_stats"].get("公式记错"), 1)

    def test_08_tags_and_update(self):
        self.st.add_tag("question", self.st.question_id(self.q2), "期中考试")
        self.assertEqual([q["uid"] for q in self.st.search(tag="期中考试")], [self.q2])
        self.st.update_question(self.q2, {"status": "wrong", "stem": "计算不定积分 ∫x²dx"})
        q = self.st.get_question(self.q2)
        self.assertEqual(q["status"], "wrong")
        self.assertIn("不定积分", q["stem"])
        self.assertIn(self.q2, [x["uid"] for x in self.st.search(keyword="不定积分")])
        # 改回去，避免影响其它用例
        self.st.update_question(self.q2, {"status": "blank", "stem": Q2["stem"]})

    def test_09_backup_and_export(self):
        zip_path = self.st.backup_zip(os.path.join(self.tmp, "b.zip"), include_images=False)
        self.assertTrue(os.path.getsize(zip_path) > 0)
        json_path = self.st.export_json(os.path.join(self.tmp, "e.json"))
        self.assertTrue(os.path.getsize(json_path) > 100)

    def test_10_plain_mode(self):
        db = os.path.join(self.tmp, "plain.db")
        st = Store(db, encrypt=False)
        uid = st.add_image("y.png")
        q = st.add_question({"stem": "明文模式测试题", "status": "correct"}, st.image_id(uid))
        self.assertEqual(st.get_question(q)["stem"], "明文模式测试题")
        self.assertEqual([x["uid"] for x in st.search(keyword="明文模式")], [q])
        st.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
