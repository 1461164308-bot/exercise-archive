# -*- coding: utf-8 -*-
"""样张绘制基元的回归测试：**画图形不能把底下的内容擦掉**。

为什么单独测这个：GDI 的 ``Ellipse``/``Rectangle`` 一定会用当前画刷填充，而 DC 的默认
画刷是白色的。``tools/make_samples.py`` 早期版本在 ``fill=None`` 时选了「纸色实心画刷」，
于是 ``hand_circle`` 画"老师红圈"时，**圈里的题号被整块擦成纸色**：

- 样张 ``s1_math_choice.jpg`` 第 2 题（红圈那题）题号「2.（5 分）」看不见；
- 新做的大学数学习题样张 ``u1``/``u4`` 等页里，凡是打叉的题号都被抹掉。

这类问题在"图能打开、体积正常、尺寸正确"的校验里**完全查不出来**，只有像素级断言能抓住，
所以这里直接数像素：画圈前后，圈内原本的深色文字必须基本还在（红笔压上去造成的那点
重叠是正常的，允许少量损失），同时红色笔迹必须真的画上了。
"""
from __future__ import annotations

import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import make_samples as S  # noqa: E402


def _counts(dib) -> tuple:
    """整张图里的 (深色文字像素, 红色笔迹像素)。"""
    buf = dib.buffer()
    dark = red = 0
    for i in range(0, len(buf), 4):
        b, g, r = buf[i], buf[i + 1], buf[i + 2]
        if r < 120 and g < 120 and b < 120:
            dark += 1
        elif r > 120 and r > g + 40 and r > b + 40:
            red += 1
    return dark, red


class HollowShapeTest(unittest.TestCase):
    """基元层面：ellipse/circle/frame_rect 默认必须是空心的。"""

    def setUp(self):
        self.dib = S.make_paper(20260401, 320, 120)
        self.c = S.Canvas(self.dib)

    def tearDown(self):
        self.dib.close()

    def test_01_circle_does_not_erase_text_under_it(self):
        self.c.text(20, 80, "2.（5 分）", size=30, color=S.INK, families=S.F_PRINT)
        S.gdi32.GdiFlush()
        dark0, red0 = _counts(self.dib)
        self.assertGreater(dark0, 100, "前置条件：文字得先画出来")

        S.hand_circle(self.c, 60, 69, 45, 19, rnd=random.Random(7))
        S.gdi32.GdiFlush()
        dark1, red1 = _counts(self.dib)

        self.assertGreater(red1, red0 + 200, "红圈没画上（红色像素没增加）")
        lost = dark0 - dark1
        self.assertLess(lost, dark0 * 0.25,
                        "红圈把底下的题号擦掉了 %d/%d 个像素 —— "
                        "多半是 ellipse 又用了实心画刷（fill=None 必须是空心）" % (lost, dark0))

    def test_02_frame_rect_does_not_erase_text_inside(self):
        """方框同理：`frame_rect` 画框不能把框里的字涂掉。"""
        self.c.text(20, 80, "得分栏内容", size=26, color=S.INK, families=S.F_PRINT)
        S.gdi32.GdiFlush()
        dark0, _ = _counts(self.dib)
        self.c.frame_rect(10, 40, 300, 100, color=S.INK_LIGHT, width=1)
        S.gdi32.GdiFlush()
        dark1, _ = _counts(self.dib)
        self.assertLess(dark0 - dark1, dark0 * 0.05,
                        "画方框把框里的内容涂掉了（fill=None 必须是空心）")

    def test_03_explicit_fill_still_fills(self):
        """显式给了填充色时，仍然要真的填满（别把空心改过头）。"""
        _, _ = _counts(self.dib)
        self.c.frame_rect(20, 20, 120, 90, color=S.INK, width=1, fill=(200, 30, 30))
        S.gdi32.GdiFlush()
        buf = self.dib.buffer()
        # 取框中心一个像素，应当是那个红色
        idx = ((20 + 90) // 2 * self.dib.w + (20 + 120) // 2) * 4
        b, g, r = buf[idx], buf[idx + 1], buf[idx + 2]
        self.assertGreater(r, 150, "显式 fill 没有生效（中心点 bgr=%s）" % ((b, g, r),))
        self.assertLess(b, 80)


class RenderedSampleTest(unittest.TestCase):
    """整体层面：真跑一遍 s1 的绘制，确认题号没被红圈吃掉。

    实测（同一段判定，只换画刷）：
      * 空心画刷（修复后）→ 题号「2.」区域有 64 个深色像素；
      * 纸色实心画刷（修复前）→ **0 个**，整个题号被擦成纸色。
    所以下面的阈值 25 是能真正区分两种行为的，不是走过场。
    """

    # s1 第 2 题的基线：page_header 的高度 + 第 1 题的间距（176）
    S1_Q2_BASELINE = 108 + 46 + 30 + 26 + 6 + 52 + 176

    def _render_s1(self):
        spec = next(p for p in S.PAGES if p["file"] == "s1_math_choice")
        dib = S.make_paper(spec["seed"])
        canvas = S.Canvas(dib)
        spec["builder"](canvas, random.Random(spec["seed"]))
        S.gdi32.GdiFlush()
        return dib

    def _dark_in(self, dib, x0, x1, y0, y1) -> int:
        buf = dib.buffer()
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                i = (y * dib.w + x) * 4
                if buf[i] < 120 and buf[i + 1] < 120 and buf[i + 2] < 120:
                    n += 1
        return n

    def test_04_s1_question_number_survives_red_circle(self):
        dib = self._render_s1()
        try:
            y = self.S1_Q2_BASELINE
            # 只量「2.」这两个字形所在的窄条 —— 红圈正是圈在这里
            dark = self._dark_in(dib, S.MARGIN, S.MARGIN + 40, y - 32, y + 10)
            self.assertGreater(
                dark, 25,
                "s1 第 2 题的题号「2.」几乎没墨迹了（%d 个像素）——"
                "红圈把它擦掉了，检查 Canvas.ellipse 的画刷" % dark)
        finally:
            dib.close()

    def test_05_stem_text_survives_a_wide_circle(self):
        """红圈画得比题号宽时，圈里的题干文字也不能被擦掉（大学数学样张就是这么圈的）。"""
        dib = S.make_paper(7, 400, 140)
        try:
            canvas = S.Canvas(dib)
            canvas.text(30, 90, "4. 下列无穷小与 x 等价的是", size=28, color=S.INK,
                        families=S.F_PRINT)
            S.gdi32.GdiFlush()
            before = self._dark_in(dib, 30, 380, 60, 100)
            self.assertGreater(before, 100)
            S.hand_circle(canvas, 30 + 200, 79, 190, 22, rnd=random.Random(11))
            S.gdi32.GdiFlush()
            after = self._dark_in(dib, 30, 380, 60, 100)
            self.assertLess(before - after, before * 0.25,
                            "宽红圈擦掉了题干文字：%d → %d" % (before, after))
        finally:
            dib.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
