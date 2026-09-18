# -*- coding: utf-8 -*-
"""导出模块测试：TXT / Markdown / HTML / Word(.docx) / PDF 都要能真的生成有效文件。

重点验证：
- .docx 是合法 zip，含 [Content_Types].xml、word/document.xml，且题干/报告内容确实写进去了；
- .pdf 以 %PDF 开头、以 %%EOF 结尾，页数正确，中文页是图片页（DCTDecode）；
- HTML 内嵌图片（base64）可离线打开；
- 错题本 / 薄弱清单 / 汇总三个预置文档都能导出。
"""

from __future__ import annotations

import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import gdiplus, imageops  # noqa: E402
from exerarchive.exporter import Exporter, export_docx, export_pdf, to_html, to_markdown, to_text  # noqa: E402
from exerarchive.store import Store  # noqa: E402
from tests._util import cleanup, scratch  # noqa: E402

REPORT = {
    "restate": "题目要求二阶行列式的值，学生把符号写反了，得到 7，正确是 -3。",
    "causes": [{"type": "公式记错", "detail": "行列式展开时第二项符号写错"}],
    "knowledge_points": [{"name": "二阶行列式", "textbook": "线性代数·行列式",
                          "note": "主对角线乘积减副对角线乘积"}],
    "solution_steps": ["写出行列式", "按对角线法则展开", "代入数值计算"],
    "final_answer": "-3",
    "avoid_tips": ["先写公式再代入", "算完回代检查符号"],
    "variants": [{"stem": "计算 |2 1; 3 4|", "answer": "5", "hint": "对角线法则"}],
    "depth": "cloud", "engine": "cloud:test-model",
}


class ExporterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch("export")
        cls.store = Store(os.path.join(cls.tmp, "t.db"), encrypt=True)
        # 造一张小图当作题目图片（导出时可以内嵌）
        if gdiplus.available():
            w, h, px = gdiplus.draw_text_bitmap(600, 400, lines=["行列式计算题", "D = |1 2 3|"],
                                                font_size=34)
            cls.img_path = os.path.join(cls.tmp, "q.png")
            imageops.Image(w, h, px).save(cls.img_path)
        else:
            cls.img_path = ""
        img_uid = cls.store.add_image(cls.img_path or "img.png")
        cls.uid = cls.store.add_question({
            "index": 1, "type": "解答题", "status": "wrong",
            "stem": "计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
            "student_answer": "7", "student_steps": "D = 1×5×10 - 2×4×10 = 50 - 80 = 7",
            "correct_answer": "-3", "knowledge_points": ["行列式", "线性代数"],
            "confidence": 0.93, "subject": "数学", "subject_detail": "线性代数",
            "grade": "大学", "qtype": "解答题",
        }, cls.store.image_id(img_uid), "q.png")
        cls.store.save_report(cls.uid, REPORT, depth="cloud", engine="cloud:test-model")
        cls.exporter = Exporter(None, cls.store)
        cls.items = cls.exporter.collect([cls.uid], with_report=True, include_images=bool(cls.img_path))

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cleanup(cls.tmp)

    def test_01_collect(self):
        self.assertEqual(len(self.items), 1)
        self.assertEqual(self.items[0]["question"]["uid"], self.uid)
        self.assertIn("restate", self.items[0]["report"])
        if self.img_path:
            self.assertTrue(self.items[0].get("image_bytes"))
            self.assertTrue(self.items[0]["image_data_uri"].startswith("data:image/jpeg;base64,"))

    def test_02_text_and_markdown(self):
        text = to_text(self.items, "测试导出")
        for needle in ("错题还原", "公式记错", "二阶行列式", "标准解题步骤", "-3", "避错技巧", "变式1"):
            self.assertIn(needle, text)
        md = to_markdown(self.items, "测试导出")
        self.assertIn("# 测试导出", md)
        self.assertIn("### 错题还原", md)

    def test_03_html(self):
        html = to_html(self.items, "测试导出", embed_images=True)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("行列式", html)
        if self.img_path:
            self.assertIn("data:image/jpeg;base64,", html)

    def test_04_docx(self):
        path = os.path.join(self.tmp, "out.docx")
        export_docx(self.items, path, "测试错题本", include_images=bool(self.img_path))
        self.assertTrue(os.path.getsize(path) > 2000)
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("word/document.xml", names)
            document = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("行列式", document)
            self.assertIn("公式记错", document)
            self.assertIn("测试错题本", document)
            if self.img_path:
                self.assertIn("word/media/image1.jpeg", names)
                self.assertIn("rIdImg1", document)

    def test_05_pdf_offline_renderer(self):
        """内置渲染器（不依赖浏览器）也要能出合法 PDF，且文字页确实是白底黑字。"""
        import re
        import zlib as _zlib

        from exerarchive.exporter import MiniPDF

        pdf = MiniPDF()
        pdf.add_text_page(["错题本测试", "第一题：计算行列式", "答案：-3"], font_size=28)
        path = os.path.join(self.tmp, "mini.pdf")
        pdf.write(path)
        data = open(path, "rb").read()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertIn(b"%%EOF", data[-16:])
        self.assertEqual(data.count(b"/Type /Page "), 1)
        self.assertIn(b"/Filter /FlateDecode", data)
        # 解压图片流，确认是白底（纸白）而不是黑底
        stream = re.search(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S).group(1)
        raw = _zlib.decompress(stream)
        self.assertEqual(len(raw), MiniPDF.PAGE_W * 2 * MiniPDF.PAGE_H * 2 * 3)
        mean = sum(raw[0::3]) / (len(raw) // 3)
        self.assertGreater(mean, 200.0, "文字页应当是白底")
        self.assertLess(mean, 255.0, "文字页应当有字（不是全白）")

    def test_06_pdf_export(self):
        path = os.path.join(self.tmp, "out.pdf")
        try:
            export_pdf(self.items, path, "测试错题本", include_images=bool(self.img_path),
                       prefer_browser=False)
        except RuntimeError as exc:
            self.skipTest(f"当前环境不支持内置 PDF 渲染：{exc}")
        data = open(path, "rb").read()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(b"%%EOF", data[-32:])
        self.assertGreater(len(data), 5000)
        if self.img_path:
            # 题目原图应作为 JPEG 页嵌入（DCTDecode），文字页是 FlateDecode
            self.assertIn(b"/Filter /DCTDecode", data)
            self.assertIn(b"/Filter /FlateDecode", data)
            self.assertEqual(data.count(b"/Type /Page "), 2)

    def test_07_exporter_export_all_formats(self):
        for fmt, ext in (("txt", "txt"), ("md", "md"), ("html", "html"), ("docx", "docx")):
            path = os.path.join(self.tmp, f"all.{ext}")
            out = self.exporter.export(path, uids=[self.uid], fmt=fmt, title="全格式测试")
            self.assertTrue(os.path.exists(out), f"{fmt} 未生成")
            self.assertGreater(os.path.getsize(out), 200, f"{fmt} 内容过小")

    def test_08_preset_documents(self):
        for fn, name in ((self.exporter.export_wrong_book, "wrong_book.md"),
                         (self.exporter.export_weak_list, "weak_list.md"),
                         (self.exporter.export_summary, "summary.md")):
            path = os.path.join(self.tmp, name)
            fn(path)
            self.assertGreater(os.path.getsize(path), 50, f"{name} 内容过小")
            content = open(path, encoding="utf-8").read()
            self.assertIn("行列式" if "weak" in name or "wrong" in name else "错题", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
