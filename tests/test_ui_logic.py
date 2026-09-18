# -*- coding: utf-8 -*-
"""界面层的纯逻辑测试（不开窗口）：识别取图决策、归档目录/命名模板、导出块组装。

这些逻辑踩过坑，所以单独测：
- **手工预处理必须被识别流程采用**：早期版本 ``on_editor_applied`` 只写了路径却没置
  ``manual_edit`` 标记，导致用户手工裁剪/纠偏的结果在识别时被自动预处理覆盖（白调一遍）。
- 归档目录模板的合法化（学科名里出现 ``/`` 或 ``:`` 时不能炸）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive.archive import Archiver, safe_name  # noqa: E402
from exerarchive.exporter import question_blocks  # noqa: E402
from tests._util import cleanup, scratch  # noqa: E402


class PickSourceTest(unittest.TestCase):
    """识别取图决策：手工处理过的图优先，且跳过自动预处理。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch("ui_logic")
        cls.original = os.path.join(cls.tmp, "原始.jpg")
        cls.edited = os.path.join(cls.tmp, "手工处理.jpg")
        for p in (cls.original, cls.edited):
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0demo")

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def _pick(self, item):
        from exerarchive.ui.app import pick_recognition_source

        return pick_recognition_source(item)

    def test_01_plain_import_uses_original(self):
        src, skip = self._pick({"path": self.original})
        self.assertEqual(src, self.original)
        self.assertFalse(skip, "没手工调过就必须走自动预处理")

    def test_02_manual_edit_wins(self):
        item = {"path": self.original, "work_path": self.edited, "manual_edit": True}
        src, skip = self._pick(item)
        self.assertEqual(src, self.edited, "手工处理过的图必须优先")
        self.assertTrue(skip, "手工处理过的图不能再被自动预处理覆盖")

    def test_03_work_path_without_flag_is_ignored(self):
        """自动预处理产生的工作图（没有 manual_edit 标记）不该跳过预处理。"""
        item = {"path": self.original, "work_path": self.edited}
        src, skip = self._pick(item)
        self.assertEqual(src, self.original)
        self.assertFalse(skip)

    def test_04_missing_file_falls_back(self):
        item = {"path": self.original, "work_path": os.path.join(self.tmp, "不存在.jpg"),
                "manual_edit": True}
        src, skip = self._pick(item)
        self.assertEqual(src, self.original, "手工图被删掉时要退回原图，而不是报错")
        self.assertFalse(skip)

    def test_05_empty_item(self):
        src, skip = self._pick({})
        self.assertEqual(src, "")
        self.assertFalse(skip)


class PhotoImageTest(unittest.TestCase):
    """预览画布用的 PhotoImage 必须真的能建出来。

    踩过的坑：早期用 ``PhotoImage(data=base64(ppm))``，Tk 报
    ``couldn't recognize image data``（Tk 的 ``-data`` 只对 GIF/PNG 支持 base64，
    PPM 只认原始字节），表现是预览区永远空白、点「载入编辑器」直接弹错误框。
    这个用例就是钉住这个行为，防止再退化。
    """

    def setUp(self):
        try:
            import tkinter as tk

            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:      # 无图形环境（服务器/CI）时跳过
            self.skipTest(f"当前环境无法初始化 Tk：{exc}")

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _img(self, w=300, h=200):
        from exerarchive import imageops

        data = bytearray(w * h * 3)
        for y in range(h):            # 画点内容，避免纯白图掩盖问题
            for x in range(0, w, 7):
                o = (y * w + x) * 3
                data[o] = data[o + 1] = data[o + 2] = 30
        return imageops.Image(w, h, data)

    def test_40_make_photo_from_image(self):
        from exerarchive.ui.widgets import make_photo

        img = self._img()
        photo = make_photo(img)
        self.assertEqual((photo.width(), photo.height()), (img.w, img.h))
        photo2 = make_photo(self._img(120, 90))
        self.assertEqual((photo2.width(), photo2.height()), (120, 90))

    def test_41_canvas_set_image(self):
        from exerarchive.ui.widgets import ImageCanvas

        canvas = ImageCanvas(self.root)
        canvas.pack()
        canvas.set_image(self._img(400, 300), max_side=200)
        self.assertIsNotNone(canvas._photo, "画布应当持有 PhotoImage")
        self.assertLessEqual(max(canvas._view.w, canvas._view.h), 200)
        self.assertGreaterEqual(len(canvas.canvas.find_all()), 1, "画布上应当有图元")
        canvas.clear()
        self.assertIsNone(canvas._photo)


class PathsTest(unittest.TestCase):
    """路径解析：代码目录与数据目录必须分开。

    踩过的坑：用户用 ``EXER_HOME`` 把数据挪到别的盘后，程序去 ``EXER_HOME/tools``
    找自检脚本，直接 ``ModuleNotFoundError: No module named 'selfcheck'``。
    随代码发布的东西（tools/ docs/ samples/）永远看 ``code_root()``，
    用户数据（data/）才看 ``app_root()``。
    """

    def test_30_code_root_vs_app_root(self):
        from exerarchive import paths

        tmp = scratch("paths_exer_home")
        code = paths.code_root()
        self.assertTrue((code / "exerarchive" / "paths.py").exists())
        self.assertEqual(paths.tools_dir(), code / "tools")
        self.assertTrue(paths.tools_dir().is_dir())
        self.assertEqual(paths.docs_dir(), code / "docs")

        # 设了 EXER_HOME 之后：数据目录跟着走，代码目录不动
        old = os.environ.get("EXER_HOME")
        try:
            os.environ["EXER_HOME"] = tmp
            self.assertEqual(paths.app_root(), paths.Path(tmp).resolve())
            self.assertEqual(paths.tools_dir(), code / "tools", "tools 必须仍在代码目录下")
            self.assertTrue(str(paths.data_dir()).startswith(str(paths.Path(tmp).resolve())))
            self.assertTrue(paths.data_dir().is_dir(), "数据目录应被自动创建")
        finally:
            if old is None:
                os.environ.pop("EXER_HOME", None)
            else:
                os.environ["EXER_HOME"] = old
            cleanup(tmp)

    def test_31_ollama_search_includes_code_dir(self):
        """模型目录探测要同时看代码目录与数据目录（绿色部署/数据挪盘都覆盖）。"""
        from exerarchive import config, paths

        roots = config._search_roots()
        self.assertIn(paths.code_root(), roots)
        self.assertIn(paths.app_root(), roots)
        found = config.default_ollama_paths()
        self.assertIn("exe", found)


class PickerAndArchiveTest(unittest.TestCase):
    def test_10_safe_name(self):
        self.assertEqual(safe_name('数学/英语:物理*'), "数学_英语_物理_")
        self.assertEqual(safe_name("   "), "未命名")
        self.assertEqual(safe_name("线性代数"), "线性代数")
        self.assertTrue(len(safe_name("x" * 200)) <= 60)

    def test_11_folder_pattern(self):
        from exerarchive import config

        tmp = scratch("archive_pattern")
        try:
            cfg = config.Config({})
            cfg.set("archive.root", tmp)
            cfg.set("archive.pattern", "{subject}/{grade}/{qtype}/{status}")
            cfg.set("archive.naming", "{uid}_{status}")
            arch = Archiver(cfg, None)
            q = {"uid": "Q20260918-0001", "subject": "数学/高等数学", "grade": "大学",
                 "qtype": "解答题", "status": "wrong", "created_at": "2026-09-18 10:00:00"}
            folder = arch.folder_for(q)
            self.assertTrue(folder.startswith(tmp))
            self.assertIn("大学", folder)
            self.assertIn("解答题", folder)
            self.assertIn("错题", folder)
            self.assertNotIn(": ", folder)
            stem = arch.question_file_stem(q)
            self.assertEqual(stem, "Q20260918-0001_错题")

            # 自定义模板：用日期与来源
            cfg.set("archive.pattern", "{subject}/{year}-{month}/{status}")
            cfg.set("archive.naming", "{status}-{uid}")
            folder2 = arch.folder_for(q)
            self.assertIn("2026-09", folder2)
            self.assertEqual(arch.question_file_stem(q), "错题-Q20260918-0001")

            # 非法模板不应崩溃
            cfg.set("archive.pattern", "{不存在的变量}")
            self.assertTrue(arch.folder_for(q))
        finally:
            cleanup(tmp)


class ExportBlocksTest(unittest.TestCase):
    def test_20_blocks_cover_all_sections(self):
        q = {"uid": "Q1", "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
             "qtype": "解答题", "status": "wrong", "difficulty": "中等",
             "knowledge_points": ["行列式"], "tags": ["期中"],
             "stem": "计算行列式 D", "options": ["A. 1", "B. 2"],
             "student_answer": "7", "student_steps": "展开得 7", "correct_answer": "-3"}
        report = {"restate": "还原", "causes": [{"type": "公式记错", "detail": "符号反"}],
                  "knowledge_points": [{"name": "行列式", "textbook": "线代", "note": "注意符号"}],
                  "solution_steps": ["第一步", "第二步"], "final_answer": "-3",
                  "avoid_tips": ["先写公式"],
                  "variants": [{"stem": "变式", "answer": "1", "hint": "提示"}],
                  "depth": "cloud", "engine": "cloud:demo"}
        kinds = [k for k, _ in question_blocks(q, report)]
        text = "\n".join(t for _, t in question_blocks(q, report))
        for seg in ("题干", "选项", "我的作答", "参考答案", "错题还原", "错误原因分析",
                    "对应知识点", "标准解题步骤", "避错技巧", "举一反三"):
            self.assertIn(seg, text, f"导出块缺少段落：{seg}")
        self.assertIn("heading", kinds)
        self.assertIn("bullet", kinds)
        # 没有报告时也要能出块（正确题/未做题）
        blocks_no_report = question_blocks(q, None)
        self.assertTrue(blocks_no_report)
        self.assertNotIn("错题还原", "\n".join(t for _, t in blocks_no_report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
