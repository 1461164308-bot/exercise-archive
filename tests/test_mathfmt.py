# -*- coding: utf-8 -*-
"""公式排版测试：文本切分、纯文本回退、以及真实渲染（GDI 光栅化）。

这些用例钉住的是「错题解析里的数学要像教科书那样显示」这条需求：
- 模型写的 ``$e^{2\\pi i k/6}$`` 必须被识别成公式片段（而不是原样显示成文本）；
- 模型偶尔漏写 ``$`` 的裸公式（``x^(n-1)+1``）也要认出来；
- 普通文字里的数字/日期（``2024-2025``、``3 个``）不能被误判成公式；
- 渲染出来的图必须**真的有笔画**（不是空白图），且分式/根号/矩阵的结构尺寸合理。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import mathdraw, mathfmt  # noqa: E402


class SegmentTest(unittest.TestCase):
    def test_01_explicit_inline_math(self):
        segs = mathfmt.to_segments("由公式 $e^{2\\pi i k/6}$ 可得")
        kinds = [k for k, _ in segs]
        self.assertEqual(kinds, ["text", "math", "text"])
        # 除法会转成竖排分数（见 test_19d），所以这里比对转换后的结果
        self.assertEqual(segs[1][1], "e^{\\frac{2\\pi i k}{6}}")

    def test_02_display_and_paren_forms(self):
        for src in ("$$\\frac{1}{2}$$", "\\[\\frac{1}{2}\\]", "\\(\\frac{1}{2}\\)"):
            segs = mathfmt.to_segments(f"结果 {src} 结束")
            self.assertEqual([k for k, _ in segs], ["text", "math", "text"], src)
            self.assertIn("\\frac", segs[1][1])

    def test_03_bare_math_is_detected(self):
        cases = ["根为 x^(n-1)+x^(n-2)+...+1 的根",
                 "代入 \\frac{a}{b} 计算",
                 "面积 S=πr^2 与周长 2πr",
                 "由 x^2 得"]
        for src in cases:
            segs = mathfmt.to_segments(src)
            self.assertIn("math", [k for k, _ in segs], f"应当识别出公式：{src}")

    def test_03b_prose_without_signal_stays_text(self):
        """没有任何数学信号的普通句子不能被当成公式。

        （注意：``=`` 现在算数学信号，所以 ``S=πr²`` 会被排版成公式 —— 那是有意的；
        真正要挡住的是"共 3 个题""2024-2025 学年"这类纯叙述。）
        """
        for src in ("这是一段普通说明文字，没有任何式子",
                    "共 3 个题，其中 2 个做错",
                    "本学期共 12 周，第 3 周开始复习"):
            segs = mathfmt.to_segments(src)
            self.assertEqual([k for k, _ in segs], ["text"], f"不应识别成公式：{src}")

    def test_04_prose_not_mistaken_for_math(self):
        for src in ("这是 2024-2025 学年第一学期",
                    "共 3 个题，其中 2 个做错",
                    "见第 12 页第 3 行",
                    "比例约为 1 比 2"):
            segs = mathfmt.to_segments(src)
            self.assertEqual([k for k, _ in segs], ["text"], f"不应识别成公式：{src}")

    def test_05_empty_and_plain(self):
        self.assertEqual(mathfmt.to_segments(""), [])
        self.assertEqual(mathfmt.to_segments("纯文字"), [("text", "纯文字")])


class PlainTextTest(unittest.TestCase):
    def test_10_frac_and_sqrt(self):
        self.assertEqual(mathfmt.math_to_plain("\\frac{1}{2}"), "1/2")
        self.assertEqual(mathfmt.math_to_plain("\\frac{a+b}{c}"), "(a+b)/c")
        self.assertEqual(mathfmt.math_to_plain("\\sqrt{b^2-4ac}"), "√(b²-4ac)")
        self.assertEqual(mathfmt.math_to_plain("\\sqrt[3]{x}"), "3√(x)")

    def test_11_scripts_and_symbols(self):
        self.assertEqual(mathfmt.math_to_plain("x^2"), "x²")
        self.assertEqual(mathfmt.math_to_plain("a_1"), "a₁")
        self.assertEqual(mathfmt.math_to_plain("x^{10}"), "x¹⁰")
        self.assertEqual(mathfmt.math_to_plain("\\alpha\\le\\beta"), "α≤β")
        self.assertEqual(mathfmt.math_to_plain("\\int_0^1 x\\,dx"), "∫₀¹ x dx")

    def test_12_matrix(self):
        self.assertEqual(mathfmt.math_to_plain("\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}"),
                         "(1 2; 3 4)")
        self.assertEqual(mathfmt.math_to_plain("\\begin{vmatrix} 1 & 2 \\\\ 3 & 4 \\end{vmatrix}"),
                         "|1 2; 3 4|")

    def test_13_format_for_export(self):
        out = mathfmt.format_for_export("由 $\\frac{a}{b}$ 与 x^2 得")
        self.assertNotIn("\\", out)
        self.assertIn("a/b", out)
        self.assertIn("x²", out)

    def test_14_normalize(self):
        self.assertEqual(mathfmt.normalize_math_text("x^12"), "x^{12}")
        self.assertEqual(mathfmt.normalize_math_text("a_ij"), "a_{ij}")
        self.assertEqual(mathfmt.normalize_math_text("**粗体**"), "粗体")
        self.assertEqual(mathfmt.normalize_math_text("（1，2）"), "(1,2)")

    def test_15_caret_paren_becomes_real_superscript(self):
        """纯文本写法 e^(2πik/6) 必须整组变成上标，而不是只把左括号抬上去。

        用户截图反馈过：识别结果里的 `e^(2πik/6)`、`x^(n-1)` 渲染成了
        「e + 悬空的上标 ( + 基线上的 2πik/6)」。
        """
        cases = {
            "e^(2πik/6)": "e^{\\frac{2πik}{6}}",
            "x^(n-1)+x^(n-2)+...+1": "x^{n-1}+x^{n-2}+...+1",
            "a_(i,j)": "a_{i,j}",
            "2^{(3)}": "2^{3}",                 # 多余的外层括号要去掉
            "x^{(n-1)}": "x^{n-1}",
        }
        for src, want in cases.items():
            segs = mathfmt.to_segments(src)
            math = [v for k, v in segs if k == "math"]
            self.assertTrue(math, f"没识别成公式：{src}")
            self.assertEqual(math[0], want, f"{src} → {math[0]}")

    def test_16_left_right_and_font_commands_do_not_leak(self):
        """\\left \\right \\text 这类命令不能变成字面文字（曾转出 "cosleft(...right)"）。"""
        self.assertEqual(mathfmt.math_to_plain(r"\cos\left(\frac{k\pi}{3}\right)"), "cos(kπ/3)")
        self.assertEqual(mathfmt.math_to_plain(r"\text{k}\pi i/n"), "kπi/n")
        self.assertEqual(mathfmt.math_to_plain(r"\operatorname{lim}_{x\to 0}"), "lim_(x→0)")
        self.assertEqual(mathfmt.math_to_plain(r"\left|x\right|"), "|x|")
        out = mathfmt.format_for_export(r"$z_k=\cos\left(\frac{k\pi}{3}\right)$")
        self.assertNotIn("left", out)
        self.assertNotIn("right", out)
        self.assertNotIn("text", out)

    def test_17_paren_superscript_renders_like_braces(self):
        """渲染层面：e^(2πik/6) 与 e^{2πik/6} 的墨迹应当几乎一致。"""
        a, _ = mathdraw.render_image("e^(2πik/6)", size_px=20)
        b, _ = mathdraw.render_image("e^{2πik/6}", size_px=20)
        self.assertLessEqual(abs(a.w - b.w), 2, f"{a.w} vs {b.w}")
        self.assertLessEqual(abs(a.h - b.h), 2, f"{a.h} vs {b.h}")
        # 不能出现"悬空的左括号"：e 之后紧跟的应该是上标内容而不是单独一个 (
        only_paren, _ = mathdraw.render_image("e^(", size_px=20)
        self.assertGreater(a.w, only_paren.w + 10, "上标内容没有跟上去")


@unittest.skipUnless(mathdraw.available(), "需要 Windows GDI 才能渲染公式")
class RenderTest(unittest.TestCase):
    FORMULAS = [
        "e^{2\\pi i k/6}",
        "x^{n-1}+x^{n-2}+\\cdots+1",
        "\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}",
        "\\int_0^1 x^2\\,dx=\\frac{1}{3}",
        "\\sum_{i=1}^{n} a_i",
        "\\lim_{x\\to 0}\\frac{\\sin x}{x}=1",
        "\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}",
        "D=\\begin{vmatrix} 1 & 2 \\\\ 3 & 4 \\end{vmatrix}=-2",
        "\\begin{cases} x+y=3 \\\\ x-y=1 \\end{cases}",
        "\\sqrt[3]{x+1}",
        "\\alpha\\le\\beta\\Rightarrow\\gamma",
    ]

    def _ink_ratio(self, img) -> float:
        dark = sum(1 for i in range(0, len(img.data), 3) if img.data[i] < 200)
        return dark / max(1, img.w * img.h)

    def test_20_all_formulas_render_with_ink(self):
        for latex in self.FORMULAS:
            img, baseline = mathdraw.render_image(latex, size_px=24)
            self.assertGreater(img.w, 4, f"渲染宽度异常：{latex}")
            self.assertGreater(img.h, 4, f"渲染高度异常：{latex}")
            self.assertGreater(self._ink_ratio(img), 0.002,
                               f"渲染结果几乎是空白（可能字形缺失）：{latex}")
            self.assertTrue(0 < baseline < img.h, f"基线位置异常：{latex} → {baseline}")

    def test_21_structures_are_taller_than_plain(self):
        plain, _ = mathdraw.render_image("x", size_px=24)
        frac, _ = mathdraw.render_image("\\frac{a}{b}", size_px=24)
        mat, _ = mathdraw.render_image("\\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}", size_px=24)
        self.assertGreater(frac.h, plain.h, "分式应当比单字符高（上下两层）")
        self.assertGreater(mat.h, plain.h, "矩阵应当比单字符高")
        self.assertGreater(mat.w, plain.w, "矩阵应当比单字符宽")

    def test_22_superscript_raises_not_flattens(self):
        """上标必须真的抬到基线以上，而不是退化成字面的 x^2。

        （宽度不一定更大：上标字号只有 70%，x^2 可能比字面的 "x2" 还窄，
        所以这里看**高度**与**墨迹分布**。）
        """
        img, _ = mathdraw.render_image("x^2", size_px=24)
        plain_x, _ = mathdraw.render_image("x", size_px=24)
        literal, _ = mathdraw.render_image("x2", size_px=24)
        self.assertGreater(img.h, plain_x.h, "带上标后应当比单个字符高")
        # 上标会抬高整体上边界；右上区域应当有墨迹
        top_right_ink = any(img.data[(y * img.w + x) * 3] < 200
                            for y in range(0, img.h // 2)
                            for x in range(img.w // 2, img.w))
        self.assertTrue(top_right_ink, "右上（上标）区域没有墨迹")
        # 与字面的 "x2" 相比，墨迹分布应当不同（说明确实做了排版）
        self.assertNotEqual(bytes(img.data), bytes(literal.data),
                            "x^2 的渲染结果与字面 x2 相同，说明上标没生效")

    def test_23_greek_and_symbols_have_glyphs(self):
        """希腊字母/数学符号必须真的画出来（字体缺字形时会变空白或方框）。"""
        for latex in ("\\alpha", "\\pi", "\\theta", "\\int", "\\sum", "\\le", "\\infty", "\\sqrt{x}"):
            img, _ = mathdraw.render_image(latex, size_px=24)
            self.assertGreater(self._ink_ratio(img), 0.002, f"{latex} 渲染成了空白")

    def test_18_left_right_render_without_leaking(self):
        """渲染层面也要能吃掉 \\left \\right（它们只是"自动伸缩括号"的标记）。"""
        img, _ = mathdraw.render_image(r"\cos\left(\frac{k\pi}{3}\right)", size_px=22)
        self.assertGreater(img.w, 30)
        # 画出来不该出现字面的 "left"/"right" 字样：与显式括号版本对比，宽度应当接近
        plain, _ = mathdraw.render_image(r"\cos(\frac{k\pi}{3})", size_px=22)
        self.assertLessEqual(abs(img.w - plain.w), 4, f"{img.w} vs {plain.w}")
        self.assertGreater(self._ink_ratio(img), 0.01)

    def test_19_text_command_keeps_content(self):
        """\\text{...} 里的内容要保留（不能变成字面的 "text"，空格也要保留）。"""
        img, _ = mathdraw.render_image(r"z_k=e^{2\text{k}\pi i/n}", size_px=22)
        self.assertGreater(self._ink_ratio(img), 0.01)
        plain, _ = mathdraw.render_image(r"z_k=e^{2k\pi i/n}", size_px=22)
        self.assertLessEqual(abs(img.w - plain.w), 6, "\\text{k} 与 k 的渲染宽度应当接近")
        # 文字模式里的空格必须保留
        with_space, _ = mathdraw.render_image(r"\text{a b}", size_px=22)
        without, _ = mathdraw.render_image(r"\text{ab}", size_px=22)
        self.assertGreater(with_space.w, without.w, r"\text{a b} 的空格丢了")

    def test_19b_math_mode_ignores_source_spaces(self):
        """数学模式的空格只是分隔符：e^{2\\pi i k/n} 与 e^{2\\pi ik/n} 必须一模一样。

        用户反馈"角标上的形式依然有些奇怪" —— 就是上标里多了一个空格。
        """
        a, _ = mathdraw.render_image(r"z_k = e^{2\pi i k/n}", size_px=22)
        b, _ = mathdraw.render_image(r"z_k=e^{2\pi ik/n}", size_px=22)
        self.assertLessEqual(abs(a.w - b.w), 2, f"{a.w} vs {b.w}")
        self.assertEqual(bytes(a.data), bytes(b.data), "两种写法的渲染结果应当逐像素相同")

    def test_19c_operator_spacing(self):
        """运算符两侧要有数学间距（x=1 不能挤成一团），一元符号不加间距。"""
        tight, _ = mathdraw.render_image(r"x{=}1", size_px=22)
        spaced, _ = mathdraw.render_image(r"x=1", size_px=22)
        self.assertGreater(spaced.w, tight.w, "= 两侧没有留出间距")
        binary, _ = mathdraw.render_image(r"a+b", size_px=22)
        binary_tight, _ = mathdraw.render_image(r"a{+}b", size_px=22)
        self.assertGreater(binary.w, binary_tight.w)
        # 一元负号（表达式开头 / 指数开头）不加间距
        unary, _ = mathdraw.render_image(r"-b", size_px=22)
        binary_minus, _ = mathdraw.render_image(r"a-b", size_px=22)
        self.assertLess(unary.w, binary_minus.w, "开头的 - 是一元符号，不该带间距")
        exp, _ = mathdraw.render_image(r"x^{-1}", size_px=22)
        self.assertLess(exp.w, 40, "指数里的一元负号不应带间距")

    def test_19d_slash_becomes_stacked_fraction(self):
        """除法要竖排成分数（用户要求）：e^{2πik/n} 的指数里必须是上下分数，不是斜杠。"""
        cases = {
            r"z_k=e^{2\pi i k/n}": r"z_k=e^{\frac{2\pi i k}{n}}",
            r"z_k=e^{(2\pi i k)/n}": r"z_k=e^{\frac{2\pi i k}{n}}",   # 分子多余括号去掉
            r"v=s/t": r"v=\frac{s}{t}",
            r"f(x)=x/2+1/3": r"f(x)=\frac{x}{2}+\frac{1}{3}",
            r"y=(a+b)/(c-d)": r"y=\frac{a+b}{c-d}",
        }
        for src, want in cases.items():
            segs = mathfmt.to_segments(src)
            math = [v for k, v in segs if k == "math"]
            self.assertTrue(math, f"没识别成公式：{src}")
            self.assertEqual(math[0], want, f"{src} → {math[0]}")

    def test_19e_text_mode_slash_untouched(self):
        """\\text{...} 里的斜杠不能被动（如 \\text{km/h}、单位、网址）。"""
        self.assertEqual(mathfmt.normalize_math_text(r"\text{km/h}"), r"\text{km/h}")
        self.assertNotIn("frac", mathfmt.normalize_math_text(r"\text{km/h} 与 \text{m/s}"))

    def test_19f_stacked_fraction_is_taller(self):
        """竖排分数在渲染上必须真的比斜杠高（证明是上下两层）。"""
        slash, _ = mathdraw.render_image(r"k/n", size_px=22)
        stacked, _ = mathdraw.render_image(r"\frac{k}{n}", size_px=22)
        self.assertGreater(stacked.h, slash.h * 1.5, f"{stacked.h} vs {slash.h}")
        # 指数里的分数：整体高度要明显高于没有分数的版本
        with_frac, _ = mathdraw.render_image(r"e^{\frac{2\pi ik}{n}}", size_px=22)
        without, _ = mathdraw.render_image(r"e^{2\pi ik}", size_px=22)
        self.assertGreater(with_frac.h, without.h, "指数里的分数没排出来")

    def test_20_plain_text_fallback_never_blank(self):
        """即使公式写得很怪，也不能渲染成空白图（宁可显示近似文本）。"""
        for latex in ("x", "1+1", "abc", "\\unknowncmd", "{", "}"):
            img, _ = mathdraw.render_image(latex, size_px=24)
            self.assertGreaterEqual(img.w, 1)

    def test_25_supersample_is_sharper_and_same_size(self):
        """超采样：**墨迹尺寸**基本不变，但抗锯齿层次明显更多（治"小字号发虚"）。

        用户反馈过"公式太小太糊"：GDI 在小字号下只有一档灰度抗锯齿。
        默认按 3 倍渲染再缩回，边缘灰度层次会明显增加。
        （比较墨迹范围而不是整图尺寸 —— 整图含透明留白，会随取整差几个像素。）
        """
        latex = r"z_k=e^{2\text{k}\pi i/n},\ k=0,1,\ldots,n"

        def levels(img) -> int:
            return len({img.data[i] for i in range(0, len(img.data), 3)})

        def ink_box(img):
            bgv = img.data[0]
            xs, ys = [], []
            for y in range(img.h):
                for x in range(img.w):
                    if img.data[(y * img.w + x) * 3] != bgv:
                        xs.append(x)
                        ys.append(y)
            if not xs:
                return (0, 0, 0, 0)
            return (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

        small, base1 = mathdraw.render_image(latex, size_px=18, supersample=1)
        sharp, base3 = mathdraw.render_image(latex, size_px=18, supersample=3)
        _x1, _y1, w1, h1 = ink_box(small)
        _x3, _y3, w3, h3 = ink_box(sharp)
        self.assertLessEqual(abs(w3 - w1), 2, f"墨迹宽度变了：{w3} vs {w1}")
        self.assertLessEqual(abs(h3 - h1), 2, f"墨迹高度变了：{h3} vs {h1}")
        # 抗锯齿层次更多 = 边缘更平滑
        self.assertGreater(levels(sharp), levels(small),
                           "3 倍超采样后灰度层次没有增加，说明超采样没生效")
        # 基线仍应落在图内（缩回后要按比例换算）
        self.assertTrue(0 < base3 < sharp.h, f"基线越界：{base3}/{sharp.h}")

    def test_26_bigger_size_means_bigger_image(self):
        """倍率调大后图必须真的变大（设置里的「公式大小倍率」靠它生效）。"""
        small, _ = mathdraw.render_image(r"x^2+1", size_px=16)
        big, _ = mathdraw.render_image(r"x^2+1", size_px=24)
        self.assertGreater(big.w, small.w)
        self.assertGreater(big.h, small.h)
        self.assertGreater(big.w / float(small.w), 1.25, "字号放大 1.5 倍，宽度却没怎么变")

    def test_27_render_is_fast_enough(self):
        """报告里常有几十条公式，单条渲染必须够快（超采样 3 倍也应在 60ms 内）。"""
        import time

        t0 = time.time()
        for _ in range(5):
            mathdraw.render_image(r"\frac{-b\pm\sqrt{b^2-4ac}}{2a}", size_px=20)
        avg = (time.time() - t0) / 5
        self.assertLess(avg, 0.06, f"单条公式平均 {avg * 1000:.0f}ms，太慢了")


class RichTextMathTest(unittest.TestCase):
    """报告页要把公式显示成教科书式图片（而不是纯文本 e^(2πik/6)）。"""

    def setUp(self):
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest(f"无法初始化 Tk：{exc}")
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            self.skipTest(f"无法初始化 Tk：{exc}")

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_30_inline_math_becomes_images(self):
        from exerarchive.ui.widgets import RichText

        rt = RichText(self.root, height=10)
        rt.pack()
        blocks = [
            ("h1", "Q1 错题分析"),
            ("h2", "错题还原"),
            ("para", "解方程 $x^2-4x+3=0$，学生得到 $x=1$，正确是 $x=1$ 或 $x=3$。"),
            ("h2", "标准解题步骤"),
            ("bullet", "① 因式分解：$x^2-4x+3=(x-1)(x-3)$"),
            ("bullet", "② 求根公式：$x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$"),
            ("kv", "最终答案：$x_1=1,\\ x_2=3$"),
            ("para", "判别式 $\\Delta=b^2-4ac=4>0$"),
        ]
        rt.write_blocks(blocks)
        self.root.update()
        # 每个公式片段都应当变成一张内嵌图
        self.assertGreaterEqual(len(rt._photos), 8, "公式没有被渲染成图片")
        for photo in rt._photos:
            self.assertGreater(photo.width(), 1)
            self.assertGreater(photo.height(), 1)
        # 取回的文本里不该残留 LaTeX 源码（导出/复制时用纯文本写法）
        text = rt.get_text()
        self.assertNotIn("$", text)
        self.assertNotIn("\\frac", text)
        self.assertIn("x²", text)

    def test_31_bare_math_also_rendered(self):
        from exerarchive.ui.widgets import RichText

        rt = RichText(self.root, height=6)
        rt.pack()
        rt.write_blocks([("para", "由 e^(2πik/6) 可得，k=0,1,2,3,4,5")])
        self.root.update()
        self.assertGreaterEqual(len(rt._photos), 1, "裸公式也应被排版")

    def test_32_plain_text_untouched(self):
        from exerarchive.ui.widgets import RichText

        rt = RichText(self.root, height=6)
        rt.pack()
        rt.write_blocks([("para", "这是一段没有任何公式的说明文字。")])
        self.root.update()
        self.assertEqual(len(rt._photos), 0)
        self.assertIn("没有任何公式", rt.get_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
