# -*- coding: utf-8 -*-
"""图像模块测试：编解码、几何变换、增强、质量分析、纠偏、去水印。"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exerarchive import gdiplus, imageops  # noqa: E402
from tests._util import cleanup, scratch  # noqa: E402


def make_image(w=400, h=300, bg=(255, 255, 255)):
    data = bytearray(bytes(bg) * (w * h))
    return imageops.Image(w, h, data)


def draw_marks(img: imageops.Image, boxes, color=(0, 0, 0)):
    """在图上画几个实心块（用于几何变换的可验证性）。"""
    for (x0, y0, x1, y1) in boxes:
        for y in range(y0, y1):
            for x in range(x0, x1):
                o = (y * img.w + x) * 3
                img.data[o], img.data[o + 1], img.data[o + 2] = color
    return img


class ImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch("image")

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    # -- 基本 ---------------------------------------------------------------- #
    def test_01_construct_and_pixel(self):
        img = make_image(10, 5, (10, 20, 30))
        self.assertEqual(img.size, (10, 5))
        self.assertEqual(img.pixel(0, 0), (10, 20, 30))
        self.assertEqual(len(img.data), 10 * 5 * 3)
        with self.assertRaises(ValueError):
            imageops.Image(0, 5, b"")

    def test_02_ppm_header(self):
        img = make_image(3, 2)
        ppm = img.to_ppm()
        self.assertTrue(ppm.startswith(b"P6\n3 2\n255\n"))
        self.assertEqual(len(ppm), len(b"P6\n3 2\n255\n") + 18)

    def test_03_pure_python_codecs(self):
        """纯 Python 的 PNG/BMP 编解码必须与原始像素完全一致（非 Windows 也靠它）。"""
        img = draw_marks(make_image(64, 48), [(5, 5, 20, 20)], (200, 30, 40))
        png = imageops.encode_png(img.w, img.h, bytes(img.data))
        with open(os.path.join(self.tmp, "a.png"), "wb") as f:
            f.write(png)
        w, h, data = imageops._decode_png(png)
        self.assertEqual((w, h), (64, 48))
        self.assertEqual(bytes(data), bytes(img.data))

        bmp = imageops.encode_bmp(img.w, img.h, bytes(img.data))
        w2, h2, data2 = imageops._decode_bmp(bmp)
        self.assertEqual((w2, h2), (64, 48))
        self.assertEqual(bytes(data2), bytes(img.data))

    def test_04_save_load_roundtrip(self):
        """PNG 无损往返；JPEG 往返允许有损但有界。"""
        img = draw_marks(make_image(200, 150), [(20, 20, 80, 80)], (0, 0, 0))
        p = os.path.join(self.tmp, "rt.png")
        img.save(p)
        back = imageops.load(p)
        self.assertEqual(back.size, img.size)
        self.assertEqual(bytes(back.data), bytes(img.data))

        jp = os.path.join(self.tmp, "rt.jpg")
        img.save(jp, quality=95)
        back2 = imageops.load(jp)
        self.assertEqual(back2.size, img.size)
        diff = sum(abs(a - b) for a, b in zip(back2.data[::31], img.data[::31]))
        self.assertLess(diff / max(1, len(back2.data[::31])), 12.0, "JPEG 往返误差过大")

    # -- 几何 ---------------------------------------------------------------- #
    def test_05_crop_and_resize(self):
        img = draw_marks(make_image(400, 300), [(0, 0, 50, 50)], (255, 0, 0))
        c = img.crop((0, 0, 100, 100))
        self.assertEqual(c.size, (100, 100))
        self.assertEqual(c.pixel(10, 10), (255, 0, 0))
        self.assertEqual(c.pixel(90, 90), (255, 255, 255))
        r = img.resize(200, 150)
        self.assertEqual(r.size, (200, 150))
        self.assertEqual(r.pixel(5, 5), (255, 0, 0), "缩放后左上角红块应保留（不能变成居中裁剪）")
        self.assertEqual(r.pixel(195, 145), (255, 255, 255))

    def test_06_scale_to_fit(self):
        img = make_image(4000, 2000)
        s = img.scale_to_fit(1000)
        self.assertEqual(s.size, (1000, 500))
        self.assertEqual(img.scale_to_fit(9999).size, (4000, 2000), "不放大")

    def test_07_rotate_geometry(self):
        """旋转 90° 与任意角度的几何正确性。"""
        img = draw_marks(make_image(100, 50), [(0, 0, 10, 10)], (0, 255, 0))
        r90 = img.rotate90(1)
        self.assertEqual(r90.size, (50, 100))
        self.assertEqual(r90.pixel(49, 0), (0, 255, 0), "顺时针 90° 后左上角应到右上角")

        line = draw_marks(make_image(400, 200), [(50, 100, 350, 102)], (0, 0, 0))
        rot = line.rotate(10.0)
        self.assertGreater(rot.h, 200)

        def darkest_y(im, x):
            best, by = 999, 0
            for y in range(im.h):
                v = im.pixel(x, y)[0]
                if v < best:
                    best, by = v, y
            return by

        slope = (darkest_y(rot, 300) - darkest_y(rot, 100)) / 200.0
        self.assertAlmostEqual(slope, math.tan(math.radians(10)), places=1)

    # -- 增强与质量 ---------------------------------------------------------- #
    def test_08_adjust_brightness(self):
        img = make_image(50, 50, (100, 100, 100))
        brighter = img.adjust(brightness=20)
        self.assertGreater(brighter.pixel(0, 0)[0], 100)
        darker = img.adjust(brightness=-20)
        self.assertLess(darker.pixel(0, 0)[0], 100)
        self.assertGreater(img.adjust(gamma=2.0).pixel(0, 0)[0], 100, "gamma>1 应变亮")

    def test_09_autocontrast_and_brighten(self):
        img = make_image(100, 100, (170, 175, 180))  # 灰蒙蒙的纸面
        draw_marks(img, [(10, 10, 20, 20)], (60, 60, 60))
        out = img.autocontrast()
        self.assertGreater(out.mean_brightness(), img.mean_brightness() - 1)
        bright, gamma = img.auto_brighten()
        self.assertGreater(bright.mean_brightness(), img.mean_brightness())
        self.assertNotEqual(gamma, 1.0)

    def test_10_quality_metrics(self):
        sharp = draw_marks(make_image(600, 400), [(50, 50, 550, 52)], (0, 0, 0))
        blurry = sharp.blur(3)
        self.assertGreater(sharp.sharpness(), blurry.sharpness())
        self.assertLess(blurry.quality_score()["score"], sharp.quality_score()["score"])
        dark = make_image(200, 200, (40, 40, 40))
        self.assertLess(dark.mean_brightness(), 60)
        # 深色/黑色照片应当被判定为"需要云端"或质量偏低
        self.assertLess(dark.quality_score()["score"], 0.6)

    def test_11_deskew_detection(self):
        """合成文本行 → 倾斜后必须能检出（含负角度与深色背景干扰）。"""
        w, h = 900, 700
        img = make_image(w, h)
        for row in range(6):  # 画 6 条"文本行"
            y = 60 + row * 100
            draw_marks(img, [(60, y, 840, y + 12)], (30, 30, 30))
        self.assertLess(abs(img.detect_skew()), 0.6, "正放的文本应判定为不倾斜")
        for angle in (3.0, -4.0, 7.0):
            tilted = img.rotate(angle)
            got = tilted.detect_skew()
            self.assertAlmostEqual(got, angle, delta=0.8, msg=f"{angle}° 倾斜检测偏差过大（得到 {got}）")

    def test_12_deskew_with_dark_border(self):
        """桌面照片：四周是深色背景，依然要能测出纸面倾斜（真实踩过的坑）。"""
        w, h = 900, 700
        img = make_image(w, h, (45, 48, 52))          # 深色桌面
        inner = imageops.Image(w - 240, h - 200, bytearray(bytes((250, 250, 248)) * ((w - 240) * (h - 200))))
        for row in range(4):
            y = 40 + row * 100
            draw_marks(inner, [(40, y, inner.w - 40, y + 12)], (30, 30, 30))
        # 把纸面贴到桌面上
        for y in range(inner.h):
            src = y * inner.w * 3
            dst = ((y + 100) * w + 120) * 3
            img.data[dst : dst + inner.w * 3] = inner.data[src : src + inner.w * 3]
        self.assertLess(abs(img.detect_skew()), 0.8)
        tilted = img.rotate(5.0)
        got = tilted.detect_skew()
        self.assertAlmostEqual(got, 5.0, delta=1.0)

    def test_13_remove_watermark_modes(self):
        # 1) 压住文字的黑块：white / inpaint 应当用周边纸色把它抹掉
        img = draw_marks(make_image(300, 200), [(50, 50, 150, 100)], (0, 0, 0))
        for mode in ("white", "inpaint"):
            out = imageops.remove_watermark(img, (50, 50, 150, 100), mode)
            self.assertEqual(out.size, img.size)
            self.assertGreater(out.crop((60, 60, 140, 90)).mean_brightness(), 150,
                               f"{mode} 模式应当把黑块抹成纸色")
        # 2) 半透明浅灰水印：median 平滑应当把它的对比度压低
        wm = make_image(300, 200, (255, 255, 255))
        draw_marks(wm, [(60, 60, 140, 90)], (205, 205, 205))
        before = wm.crop((70, 70, 130, 80)).mean_brightness()
        after = imageops.remove_watermark(wm, (60, 60, 140, 90), "median").crop(
            (70, 70, 130, 80)).mean_brightness()
        self.assertGreaterEqual(after, before - 0.5)
        # 3) 非法/过小的框不应崩，返回原图副本
        tiny = imageops.remove_watermark(img, (10, 10, 11, 11), "inpaint")
        self.assertEqual(tiny.size, img.size)

    def test_14_auto_preprocess_smoke(self):
        """自动预处理在“又暗又歪”的图上应当至少做出一项改动，且尺寸受限。"""
        w, h = 1200, 900
        img = make_image(w, h, (150, 150, 150))
        for row in range(7):
            y = 60 + row * 110
            draw_marks(img, [(80, y, w - 80, y + 14)], (60, 60, 60))
        tilted = img.rotate(4.5)
        out, steps = imageops.auto_preprocess(tilted, {"max_side": 800})
        self.assertTrue(steps, "应当至少执行一步处理")
        self.assertLessEqual(max(out.w, out.h), 800)
        self.assertGreater(out.mean_brightness(), tilted.mean_brightness() - 5)

    def test_15_backend_report(self):
        self.assertTrue(imageops.backend_name())
        if gdiplus.available():
            self.assertTrue(imageops.probe(os.path.join(self.tmp, "rt.png")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
