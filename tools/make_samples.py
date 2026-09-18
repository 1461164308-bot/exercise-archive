# -*- coding: utf-8 -*-
"""生成「中文习题作业」测试图片样本（纯标准库 + Windows GDI/GDI+，无第三方依赖）。

用法（在 C:\\deepseek\\exercise-archive 目录下直接跑）：
    python tools\\make_samples.py

产物（默认写进 exercise-archive\\samples\\）：
    s1_math_choice.jpg         高中数学 选择题 3 道（第 1 题红笔对勾，第 2 题红笔打叉，第 3 题空白）
    s2_math_fill.jpg           大学微积分 填空题 3 道（含积分/导数符号，第 3 题空白）
    s3_math_solution.jpg       大学线性代数 解答题 1 道（行列式，学生步骤符号写错，老师批「符号错」+ 红叉）
    s4_english.jpg             高中英语 选择/填空 3 道（英文题干 + 中文批注，第 1 题选错打叉）
    s5_physics.jpg             高中物理 计算题 1 道（斜面/牛顿第二定律，答错 + 红叉 + 「单位漏了」）
    s6_math_tilted_blur.jpg    与 s1 内容相同，但整体旋转 7°、轻微模糊、偏暗、左上角半透明水印
    ground_truth.json          每题的标准答案状态（wrong / correct / blank）

技术要点：
  * 全部绘制走 ctypes + GDI：CreateCompatibleDC / CreateDIBSection（32 位顶朝下 DIB）/
    CreateFontW / TextOutW / MoveToEx / LineTo / Rectangle / Ellipse / Polygon ...
    —— 句柄一律先声明 argtypes，否则 64 位下会被截断成 32 位整数（实测踩过的坑）。
  * 编码走 GDI+（gdiplus.dll）：GdiplusStartup → GdipCreateBitmapFromScan0 →
    GdipSaveImageToFile，JPEG 编码器 CLSID 由 GdipGetImageEncoders 查询得到；
    若 GDI+ 不可用则兜底写 24bpp 自底向上 BMP（BITMAPFILEHEADER + BITMAPINFOHEADER）。
  * 旋转 / 缩放合成也交给 GDI+（世界变换 + 高质量插值），逐像素的压暗与去色块水印混入
    用 bytes.translate / bytearray 扩展切片做，避免纯 Python 逐像素循环。
  * 对勾、叉、手写答案、批注全部是「带抖动的手绘笔画 + 逐字抖动倾斜的手写体」，红笔。
  * 所有随机数用固定种子，样本可复现。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import random
import struct
import sys
from ctypes import wintypes

# ================================================================ 基本参数
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "samples")

PAGE_W, PAGE_H = 1240, 1754          # A4 @150dpi
MARGIN = 78                          # 版心左右边距
TEXT_W = PAGE_W - MARGIN * 2         # 版心宽度
JPEG_QUALITY = 88
MIN_BYTES = 5 * 1024                 # 校验阈值：小于 5KB 视为生成失败

# 颜色统一用 (r, g, b)
PAPER = (247, 246, 241)              # 纸底色（略偏暖的灰白）
INK = (32, 34, 38)                   # 印刷体正文墨色
INK_LIGHT = (96, 100, 108)           # 印刷体浅灰（辅助说明、页脚）
RULE = (120, 124, 132)               # 印刷横线
RED = (203, 36, 36)                  # 红笔（学生作答 + 老师批改）
RED_DEEP = (168, 22, 22)             # 红笔重色（打叉/对勾）
WATERMARK_INK = (72, 76, 96)         # 水印墨色

# 字体族候选栈：逐字符挑第一个「真有这个字形」的字体，保证不出现豆腐块
F_PRINT = ["SimHei", "DengXian", "Microsoft YaHei", "SimSun"]              # 印刷体（黑体）中文
F_MATH = ["Times New Roman", "Cambria", "SimHei", "DengXian"]              # 印刷体西文/数学
F_HAND_CJK = ["KaiTi", "DengXian", "SimHei"]                               # 手写中文（楷体 + 抖动）
# 手写西文：Segoe Print 排在最前 —— Ink Free 的数字 9 收笔像 q，会被 OCR 认错
F_HAND_LAT = ["Segoe Print", "Ink Free", "Segoe Script", "KaiTi", "SimHei"]

# ================================================================ GDI 常量
BI_RGB = 0
DIB_RGB_COLORS = 0
TRANSPARENT = 1
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
ANTIALIASED_QUALITY = 4
VARIABLE_PITCH = 2
FF_DONTCARE = 0
PS_SOLID = 0
GGI_MARK_NONEXISTING_GLYPHS = 0x0001
DT_CENTER = 0x00000001
DT_RIGHT = 0x00000002
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
DT_NOCLIP = 0x00000100
SRCCOPY = 0x00CC0020

# ================================================================ ctypes 绑定
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class TEXTMETRIC(ctypes.Structure):
    _fields_ = [("tmHeight", wintypes.LONG), ("tmAscent", wintypes.LONG),
                ("tmDescent", wintypes.LONG), ("tmInternalLeading", wintypes.LONG),
                ("tmExternalLeading", wintypes.LONG), ("tmAveCharWidth", wintypes.LONG),
                ("tmMaxCharWidth", wintypes.LONG), ("tmWeight", wintypes.LONG),
                ("tmOverhang", wintypes.LONG), ("tmDigitizedAspectX", wintypes.LONG),
                ("tmDigitizedAspectY", wintypes.LONG), ("tmFirstChar", wintypes.WCHAR),
                ("tmLastChar", wintypes.WCHAR), ("tmDefaultChar", wintypes.WCHAR),
                ("tmBreakChar", wintypes.WCHAR), ("tmItalic", ctypes.c_byte),
                ("tmUnderlined", ctypes.c_byte), ("tmStruckOut", ctypes.c_byte),
                ("tmPitchAndFamily", ctypes.c_byte), ("tmCharSet", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                   wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [ctypes.c_wchar_p]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextAlign.argtypes = [wintypes.HDC, wintypes.UINT]
gdi32.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p,
                           ctypes.c_int]
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, ctypes.c_wchar_p, ctypes.c_int,
                                        ctypes.POINTER(SIZE)]
gdi32.GetTextMetricsW.argtypes = [wintypes.HDC, ctypes.POINTER(TEXTMETRIC)]
gdi32.GetGlyphIndicesW.argtypes = [wintypes.HDC, ctypes.c_wchar_p, ctypes.c_int,
                                   ctypes.POINTER(wintypes.WORD), wintypes.UINT]
gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.Rectangle.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int]
gdi32.Polygon.argtypes = [wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_int]
gdi32.SetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
user32.DrawTextW.argtypes = [wintypes.HDC, ctypes.c_wchar_p, ctypes.c_int,
                            ctypes.POINTER(RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
user32.FillRect.restype = ctypes.c_int
gdi32.GdiFlush.restype = wintypes.BOOL


def _colorref(c) -> int:
    """(r,g,b) → GDI 的 COLORREF（0x00BBGGRR）。"""
    return int(c[0]) | (int(c[1]) << 8) | (int(c[2]) << 16)


# ================================================================ DIB（32 位顶朝下）
class Dib:
    """32 位顶朝下 DIB，内存布局 BGRA。GDI 可以直接往这块内存上画。"""

    def __init__(self, w: int, h: int):
        self.w, self.h = max(1, int(w)), max(1, int(h))
        self.stride = self.w * 4
        self.size = self.stride * self.h
        self.bmi = BITMAPINFO()
        hdr = self.bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = self.w
        hdr.biHeight = -self.h          # 负数 = 顶朝下
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = BI_RGB
        hdr.biSizeImage = self.size

        self._bits = ctypes.c_void_p()
        screen_dc = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen_dc)
        self.hbmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(self.bmi), DIB_RGB_COLORS,
                                           ctypes.byref(self._bits), None, 0)
        user32.ReleaseDC(None, screen_dc)
        if not self.hbmp:
            raise OSError("CreateDIBSection 失败")
        self._old = gdi32.SelectObject(self.dc, self.hbmp)

    # -- 访问 --------------------------------------------------------
    def buffer(self):
        return ctypes.cast(self._bits, ctypes.POINTER(ctypes.c_ubyte * self.size)).contents

    def bits_ptr(self):
        return ctypes.cast(self._bits, ctypes.c_void_p)

    def hdc(self):
        """包成 wintypes.HDC 再传：Python int 会被 argtypes 当成 32 位截断，句柄就废了。"""
        return wintypes.HDC(self.dc)

    def fill(self, color, a: int = 255) -> None:
        r, g, b = color
        row = bytes((b, g, r, a)) * self.w
        ctypes.memmove(self.buffer(), row * self.h, self.size)

    def copy_from(self, other: "Dib") -> None:
        if (self.w, self.h) != (other.w, other.h):
            raise ValueError("尺寸不一致，无法整块拷贝")
        ctypes.memmove(self.buffer(), other.buffer(), self.size)

    def close(self) -> None:
        if getattr(self, "hbmp", None):
            gdi32.SelectObject(self.dc, self._old)
            gdi32.DeleteObject(self.hbmp)
            gdi32.DeleteDC(self.dc)
            self.hbmp = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def make_paper(seed: int, w: int = PAGE_W, h: int = PAGE_H, base=None) -> Dib:
    """生成「灰白纸张」底：底色 + 细噪点 + 少量纸屑/污点 + 边缘轻微压暗。

    逐像素写 200 万像素在纯 Python 里太慢，这里用「噪声模板行 + 随机偏移切片」的办法：
    模板只有两行宽，每行切一段贴过去，行与行之间不重样，视觉上就是均匀细噪点。
    base 用来换底色（s6 的「桌面」背景就是同一套纸纹、只换个基色）。
    """
    rnd = random.Random(seed)
    base = tuple(base or PAPER)
    dib = Dib(w, h)
    stride = dib.stride

    # --- 噪声模板（2 行宽）：只做亮度抖动、三通道同增同减，
    #     否则每个通道各自随机会在平坦区域形成彩色横纹（JPEG 上一眼就假） ---
    tpl = bytearray(stride * 2)
    for i in range(stride * 2 // 4):
        j = i * 4
        d = rnd.randint(-3, 3)
        tpl[j + 0] = _clamp(base[2] + d)
        tpl[j + 1] = _clamp(base[1] + d)
        tpl[j + 2] = _clamp(base[0] + d)
        tpl[j + 3] = 255
    tpl = bytes(tpl)
    buf = dib.buffer()
    for y in range(h):
        off = rnd.randrange(0, stride)
        ctypes.memmove(ctypes.byref(buf, y * stride), tpl[off:off + stride], stride)

    # --- 纸屑 / 墨点 ---
    for _ in range(420):
        x = rnd.randrange(6, w - 6)
        y = rnd.randrange(6, h - 6)
        dark = rnd.random() < 0.35
        delta = rnd.randint(10, 42) if dark else rnd.randint(4, 12)
        col = (_clamp(base[0] - delta), _clamp(base[1] - delta), _clamp(base[2] - delta))
        rad = 1 if rnd.random() < 0.8 else 2
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                px, py = x + dx, y + dy
                if 0 <= px < w and 0 <= py < h:
                    o = py * stride + px * 4
                    a = 120 if dark else 70
                    buf[o + 0] = _clamp((buf[o + 0] * (255 - a) + col[2] * a) // 255)
                    buf[o + 1] = _clamp((buf[o + 1] * (255 - a) + col[1] * a) // 255)
                    buf[o + 2] = _clamp((buf[o + 2] * (255 - a) + col[0] * a) // 255)

    # --- 扫描/拍照常见的边缘渐暗 ---
    edge = 26
    for y in range(h):
        dy = min(y, h - 1 - y)
        fy = 1.0 if dy >= edge else 0.965 + 0.035 * (dy / edge)
        base = y * stride
        for x in list(range(0, edge)) + list(range(w - edge, w)):
            dx = min(x, w - 1 - x)
            f = fy * (0.965 + 0.035 * (dx / edge))
            o = base + x * 4
            buf[o + 0] = _clamp(int(buf[o + 0] * f))
            buf[o + 1] = _clamp(int(buf[o + 1] * f))
            buf[o + 2] = _clamp(int(buf[o + 2] * f))
    return dib


def _clamp(v: int, lo: int = 0, hi: int = 255) -> int:
    return lo if v < lo else (hi if v > hi else v)


# ================================================================ GDI 画布
class Canvas:
    """把 GDI 的绘图调用包装成「基线坐标」的排版接口。

    统一以**基线**定位（而不是 TextOut 的左上角），这样中文字体与西文字体的
    字号不同也不会串行；上下标、不同字号混排都能对齐。
    """

    def __init__(self, dib: Dib):
        self.dib = dib
        self.hdc = dib.hdc()
        self._fonts: dict = {}
        self._pens: dict = {}
        self._asc: dict = {}
        self._glyph_cache: dict = {}
        self._pick_cache: dict = {}
        self._measure_cache: dict = {}
        gdi32.SetBkMode(self.hdc, TRANSPARENT)
        gdi32.SetTextAlign(self.hdc, 0)      # TA_LEFT | TA_TOP

    # -- 字体 --------------------------------------------------------
    def font(self, size, face, bold=False, italic=False, esc=0):
        key = (int(round(size)), face, bool(bold), bool(italic), int(esc))
        hf = self._fonts.get(key)
        if hf is None:
            hf = gdi32.CreateFontW(-int(round(size)), 0, int(esc), int(esc),
                                   700 if bold else 400, 1 if italic else 0, 0, 0,
                                   DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                                   ANTIALIASED_QUALITY, VARIABLE_PITCH | FF_DONTCARE, face)
            if not hf:
                raise OSError("CreateFontW 失败: %s" % face)
            self._fonts[key] = hf
        return hf

    def _ascent(self, size, face, bold=False, italic=False, esc=0) -> int:
        key = (int(round(size)), face, bool(bold), bool(italic), int(esc))
        v = self._asc.get(key)
        if v is None:
            hf = self.font(size, face, bold, italic, esc)
            old = gdi32.SelectObject(self.hdc, hf)
            tm = TEXTMETRIC()
            gdi32.GetTextMetricsW(self.hdc, ctypes.byref(tm))
            gdi32.SelectObject(self.hdc, old)
            v = int(tm.tmAscent)
            self._asc[key] = v
        return v

    def _has_glyph(self, size, face, ch: str, bold=False) -> bool:
        key = (int(round(size)), face, ch, bool(bold))
        v = self._glyph_cache.get(key)
        if v is None:
            hf = self.font(size, face, bold)
            old = gdi32.SelectObject(self.hdc, hf)
            gl = (wintypes.WORD * 1)()
            n = gdi32.GetGlyphIndicesW(self.hdc, ch, 1, gl, GGI_MARK_NONEXISTING_GLYPHS)
            gdi32.SelectObject(self.hdc, old)
            v = bool(n) and gl[0] != 0xFFFF
            self._glyph_cache[key] = v
        return v

    def pick_face(self, s: str, size, families, bold=False) -> str:
        key = (s, int(round(size)), tuple(families), bool(bold))
        got = self._pick_cache.get(key)
        if got is not None:
            return got
        chosen = families[-1]
        for fam in families:
            if all(self._has_glyph(size, fam, ch, bold) for ch in set(s)):
                chosen = fam
                break
        self._pick_cache[key] = chosen
        return chosen

    def measure(self, s: str, size, families=None, bold=False, italic=False) -> int:
        """量一段文本的像素宽度（不含 ^/_ 上下标解析）。"""
        if not s:
            return 0
        face = self.pick_face(s, size, families or F_PRINT, bold)
        key = (s, int(round(size)), face, bool(bold), bool(italic))
        v = self._measure_cache.get(key)
        if v is None:
            hf = self.font(size, face, bold, italic)
            old = gdi32.SelectObject(self.hdc, hf)
            sz = SIZE()
            gdi32.GetTextExtentPoint32W(self.hdc, s, len(s), ctypes.byref(sz))
            gdi32.SelectObject(self.hdc, old)
            v = int(sz.cx)
            self._measure_cache[key] = v
        return v

    # -- 绘制基元 ----------------------------------------------------
    def pen(self, color, width=1):
        key = (int(color[0]), int(color[1]), int(color[2]), int(width))
        hp = self._pens.get(key)
        if hp is None:
            hp = gdi32.CreatePen(PS_SOLID, max(1, int(width)), _colorref(color))
            self._pens[key] = hp
        return hp

    def _with_pen(self, color, width, fn):
        hp = self.pen(color, width)
        old = gdi32.SelectObject(self.hdc, hp)
        try:
            fn()
        finally:
            gdi32.SelectObject(self.hdc, old)

    def line(self, x1, y1, x2, y2, color=INK, width=1):
        def go():
            gdi32.MoveToEx(self.hdc, int(x1), int(y1), None)
            gdi32.LineTo(self.hdc, int(x2), int(y2))
        self._with_pen(color, width, go)

    def dash_line(self, x1, y1, x2, y2, color=INK, width=1, dash=14, gap=10):
        """虚线（GDI 的 PS_DOT 在粗线/缩放 DC 上不可靠，这里手动断点画）。"""
        dx, dy = x2 - x1, y2 - y1
        total = math.hypot(dx, dy)
        if total <= 0:
            return
        ux, uy = dx / total, dy / total
        pos = 0.0
        while pos < total:
            end = min(pos + dash, total)
            self.line(x1 + ux * pos, y1 + uy * pos, x1 + ux * end, y1 + uy * end, color, width)
            pos = end + gap

    def fill_rect(self, x1, y1, x2, y2, color):
        hb = gdi32.CreateSolidBrush(_colorref(color))
        old = gdi32.SelectObject(self.hdc, hb)
        try:
            r = RECT(int(x1), int(y1), int(x2), int(y2))
            user32.FillRect(self.hdc, ctypes.byref(r), hb)
        finally:
            gdi32.SelectObject(self.hdc, old)
            gdi32.DeleteObject(hb)

    def frame_rect(self, x1, y1, x2, y2, color=INK, width=1, fill=None):
        if fill is not None:
            self.fill_rect(x1, y1, x2, y2, fill)
        hp = self.pen(color, width)
        hb = gdi32.CreateSolidBrush(_colorref(fill if fill is not None else PAPER))
        oldp = gdi32.SelectObject(self.hdc, hp)
        oldb = gdi32.SelectObject(self.hdc, hb)
        try:
            gdi32.Rectangle(self.hdc, int(x1), int(y1), int(x2), int(y2))
        finally:
            gdi32.SelectObject(self.hdc, oldp)
            gdi32.SelectObject(self.hdc, oldb)
            gdi32.DeleteObject(hb)

    def circle(self, cx, cy, r, color=INK, width=1, fill=None):
        self.ellipse(cx, cy, r, r, color=color, width=width, fill=fill)

    def ellipse(self, cx, cy, rx, ry, color=INK, width=1, fill=None):
        hp = self.pen(color, width)
        hb = gdi32.CreateSolidBrush(_colorref(fill if fill is not None else PAPER))
        oldp = gdi32.SelectObject(self.hdc, hp)
        oldb = gdi32.SelectObject(self.hdc, hb)
        try:
            gdi32.Ellipse(self.hdc, int(cx - rx), int(cy - ry), int(cx + rx), int(cy + ry))
        finally:
            gdi32.SelectObject(self.hdc, oldp)
            gdi32.SelectObject(self.hdc, oldb)
            gdi32.DeleteObject(hb)

    def text_box(self, left, top, right, bottom, s, size, color=INK, families=None,
                 bold=False, align="center"):
        """DrawTextW：在矩形里排版（标题居中、页脚居中都用它）。"""
        flags = DT_NOCLIP | DT_SINGLELINE | DT_VCENTER
        if align == "center":
            flags |= DT_CENTER
        elif align == "right":
            flags |= DT_RIGHT
        face = self.pick_face(s, size, families or F_PRINT, bold)
        hf = self.font(size, face, bold)
        old = gdi32.SelectObject(self.hdc, hf)
        gdi32.SetTextColor(self.hdc, _colorref(color))
        r = RECT(int(left), int(top), int(right), int(bottom))
        try:
            user32.DrawTextW(self.hdc, s, len(s), ctypes.byref(r), flags)
        finally:
            gdi32.SelectObject(self.hdc, old)

    def polygon(self, pts, color=INK, width=1, fill=None):
        arr = (POINT * len(pts))()
        for i, (px, py) in enumerate(pts):
            arr[i].x, arr[i].y = int(px), int(py)
        hp = self.pen(color, width)
        hb = gdi32.CreateSolidBrush(_colorref(fill if fill is not None else PAPER))
        oldp = gdi32.SelectObject(self.hdc, hp)
        oldb = gdi32.SelectObject(self.hdc, hb)
        try:
            gdi32.Polygon(self.hdc, arr, len(pts))
        finally:
            gdi32.SelectObject(self.hdc, oldp)
            gdi32.SelectObject(self.hdc, oldb)
            gdi32.DeleteObject(hb)

    def stroke(self, pts, color=RED, width=3, jitter=0.0, rnd=None):
        """手绘折线：顶点带抖动、段中再插点，看起来是「一笔画下去」而不是矢量直线。"""
        rnd = rnd or random
        dense = []
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            seg = max(2, int(math.hypot(x2 - x1, y2 - y1) / 9) + 1)
            for t in range(seg):
                k = t / seg
                dense.append((x1 + (x2 - x1) * k, y1 + (y2 - y1) * k))
        dense.append(pts[-1])
        hp = self.pen(color, width)
        old = gdi32.SelectObject(self.hdc, hp)
        try:
            first = True
            for (px, py) in dense:
                jx = rnd.uniform(-jitter, jitter)
                jy = rnd.uniform(-jitter, jitter)
                if first:
                    gdi32.MoveToEx(self.hdc, int(px + jx), int(py + jy), None)
                    first = False
                else:
                    gdi32.LineTo(self.hdc, int(px + jx), int(py + jy))
        finally:
            gdi32.SelectObject(self.hdc, old)

    # -- 文本 --------------------------------------------------------
    def text(self, x, base, s, size=30, color=INK, families=None, bold=False, italic=False,
             hand=False, rnd=None, jitter=0.0, script=True) -> float:
        """在基线 base 处绘制文本，支持 ^ 上标 / _ 下标（如 x^2、（∫_0^1））。

        hand=True 时逐字抖动 + 逐字随机倾斜 + 字号微抖，用来伪造手写。
        返回文本右端的 x 坐标。
        """
        if not s:
            return x
        families = families or F_PRINT
        rnd = rnd or random
        runs = self._parse_scripts(s) if script else [(s, 0)]
        limit_anchor = None
        for run, level in runs:
            fsize = size * (0.60 if level else 1.0)
            dy = (-size * 0.40) if level > 0 else (size * 0.24 if level < 0 else 0.0)
            face = self.pick_face(run, fsize, families, bold)
            esc = 0
            if hand:
                jx, jy = rnd.uniform(-jitter, jitter), rnd.uniform(-jitter, jitter)
                esc = rnd.randrange(-45, 46, 15)          # ±4.5°，量化到 1.5° 减少句柄数
                fsize = fsize + rnd.choice((-1.5, -1.0, 0.0, 0.0, 1.0, 1.5))
                x += jx
                dy += jy
            asc = self._ascent(fsize, face, bold, italic, esc)
            y = base + dy - asc
            if limit_anchor is not None:                   # ∫/∑ 的上下限居中放在符号正上/正下方
                y_off = -size * 0.56 if level > 0 else size * 0.16
                y = base + y_off - asc
                x = limit_anchor - self.measure(run, fsize, families, bold, italic) / 2.0
            hf = self.font(fsize, face, bold, italic, esc)
            old = gdi32.SelectObject(self.hdc, hf)
            gdi32.SetTextColor(self.hdc, _colorref(color))
            try:
                gdi32.TextOutW(self.hdc, int(round(x)), int(round(y)), run, len(run))
            finally:
                gdi32.SelectObject(self.hdc, old)
            width = self.measure(run, fsize, families, bold, italic)
            if run in ("∫", "∑", "∏"):
                limit_anchor = x + width / 2.0
            else:
                limit_anchor = None
            x += width
        return x

    @staticmethod
    def _parse_scripts(s: str):
        """把 "x^2"、"∫_0^1"、"a_{n+1}" 拆成 [(片段, 层级)]，层级 0/±1。"""
        runs, buf, i = [], [], 0
        while i < len(s):
            ch = s[i]
            if ch in "^_":
                lvl = 1 if ch == "^" else -1
                i += 1
                if i < len(s) and s[i] == "{":
                    j = s.find("}", i)
                    j = len(s) if j < 0 else j
                    token = s[i + 1:j]
                    i = j + 1
                else:
                    token = s[i] if i < len(s) else ""
                    i += 1
                if token:
                    if buf:
                        runs.append(("".join(buf), 0))
                        buf = []
                    runs.append((token, lvl))
                continue
            buf.append(ch)
            i += 1
        if buf:
            runs.append(("".join(buf), 0))
        return runs


# ================================================================ 手写 / 批改笔迹
def hand_circle(c: Canvas, cx, cy, rx=30, ry=24, color=RED_DEEP, width=3, rnd=None):
    """红笔圈题号（老师标记「这题错了」）：GDI Ellipse 画两圈、略微错位，
    这样它不是一条完美的椭圆，看着像手一圈画出来的。"""
    rnd = rnd or random.Random(7)
    for _ in range(2):
        dx = rnd.uniform(-2.5, 2.5)
        dy = rnd.uniform(-2.0, 2.0)
        c.ellipse(cx + dx, cy + dy, rx + rnd.uniform(-2, 3), ry + rnd.uniform(-2, 2),
                  color=color, width=width)


def hand_check(c: Canvas, cx, cy, size=30, color=RED_DEEP, rnd=None):
    """红笔手写对勾 ✓（两段抖动折线，起笔短、收笔长）。"""
    rnd = rnd or random.Random(1)
    s = size / 30.0
    pts = [(cx - 0.55 * size, cy + 0.02 * size),
           (cx - 0.20 * size, cy + 0.42 * size),
           (cx + 0.60 * size, cy - 0.62 * size)]
    c.stroke([(x + rnd.uniform(-2, 2) * s, y + rnd.uniform(-2, 2) * s) for x, y in pts],
             color=color, width=max(3, int(4 * s)), jitter=1.5 * s, rnd=rnd)


def hand_cross(c: Canvas, cx, cy, size=32, color=RED_DEEP, rnd=None):
    """红笔手写叉 ✗（两条独立笔画，交点略偏，收笔带钩）。"""
    rnd = rnd or random.Random(2)
    s = size / 32.0
    a = [(cx - 0.50 * size, cy - 0.52 * size), (cx + 0.46 * size, cy + 0.54 * size)]
    b = [(cx + 0.50 * size, cy - 0.46 * size), (cx - 0.52 * size, cy + 0.48 * size)]
    for stroke in (a, b):
        pts = [(x + rnd.uniform(-2.5, 2.5) * s, y + rnd.uniform(-2.5, 2.5) * s)
               for x, y in stroke]
        c.stroke(pts, color=color, width=max(3, int(4.5 * s)), jitter=2.0 * s, rnd=rnd)


def hand_underline(c: Canvas, x1, x2, y, color=RED, rnd=None, wobble=3.0):
    """手写波浪下划线（老师划重点/标错处）。"""
    rnd = rnd or random.Random(3)
    n = max(4, int((x2 - x1) / 26) + 2)
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((x1 + (x2 - x1) * t, y + rnd.uniform(-wobble, wobble)))
    c.stroke(pts, color=color, width=3, jitter=1.2, rnd=rnd)


def hand_text(c: Canvas, x, base, s, size=30, color=RED, families=None, rnd=None,
              jitter=1.6):
    """手写文字：楷体/手写体 + 逐字抖动（学生作答、老师批注）。"""
    rnd = rnd or random.Random(4)
    return c.text(x, base, s, size=size, color=color,
                  families=families or F_HAND_CJK, hand=True, rnd=rnd, jitter=jitter)


def hand_latin(c: Canvas, x, base, s, size=30, color=RED, rnd=None, jitter=1.4):
    """手写西文/数字（用 Segoe Print / Ink Free 这类真手写体）。"""
    rnd = rnd or random.Random(5)
    return c.text(x, base, s, size=size, color=color, families=F_HAND_LAT,
                  hand=True, rnd=rnd, jitter=jitter)


def hand_text_fit(c: Canvas, x, base, s, size, max_x, color=RED, rnd=None,
                  families=None, jitter=1.6, min_size=21):
    """手写批注 + 自动缩字号（中英数混排的长批注保证不会画出版心）。"""
    rnd = rnd or random.Random(6)
    families = families or F_HAND_CJK
    size0 = size
    while size > min_size and x + c.measure(s, size, families) > max_x:
        size -= 1
    if size != size0:
        print("  · 适配版心：手写批注字号 %d → %d" % (size0, size))
    return c.text(x, base, s, size=size, color=color, families=families, hand=True,
                  rnd=rnd, jitter=jitter)


# ================================================================ GDI+ 封装
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class ImageCodecInfo(ctypes.Structure):
    _fields_ = [("Clsid", GUID), ("FormatID", GUID), ("CodecName", wintypes.LPWSTR),
                ("DllName", wintypes.LPWSTR), ("FormatDescription", wintypes.LPWSTR),
                ("FilenameExtension", wintypes.LPWSTR), ("MimeType", wintypes.LPWSTR),
                ("Flags", wintypes.DWORD), ("Version", wintypes.DWORD),
                ("SigCount", wintypes.DWORD), ("SigSize", wintypes.DWORD),
                ("SigPattern", ctypes.c_void_p), ("SigMask", ctypes.c_void_p)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL)]


class EncoderParameter(ctypes.Structure):
    _fields_ = [("Guid", GUID), ("NumberOfValues", ctypes.c_uint32),
                ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", EncoderParameter * 1)]


class PointF(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


PIXFMT_32BPP_RGB = 0x00022009      # 用 RGB（不用 ARGB）：GDI 不会维护 alpha，
                                   # 若按 ARGB 交给 GDI+ 合成会被当成整幅透明。
INTERP_HQ_BICUBIC = 7
INTERP_BILINEAR = 4
PIXEL_OFFSET_HQ = 2
SMOOTH_ANTIALIAS = 4
MATRIX_ORDER_APPEND = 1
QUALITY_GUID = GUID(0x1D5BE4B5, 0xFA4A, 0x452D,
                    (ctypes.c_ubyte * 8)(0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB))


class GdiPlus:
    """GDI+ 封装：只做三件事 —— JPEG 编码、位图缩放、旋转合成。"""

    def __init__(self):
        self.ok = False
        self.jpeg_clsid = None
        self.token = ctypes.c_void_p()
        try:
            self.dll = ctypes.WinDLL("gdiplus", use_last_error=True)
        except OSError:
            return
        d = self.dll
        d.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p]
        d.GdiplusShutdown.argtypes = [ctypes.c_void_p]
        d.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]
        d.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        d.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.POINTER(GUID), ctypes.c_void_p]
        d.GdipGetImageEncodersSize.argtypes = [ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_uint32)]
        d.GdipGetImageEncoders.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        d.GdipGetImageGraphicsContext.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_void_p)]
        d.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.GdipTranslateWorldTransform.argtypes = [ctypes.c_void_p, ctypes.c_float,
                                                  ctypes.c_float, ctypes.c_int]
        d.GdipRotateWorldTransform.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_int]
        d.GdipResetWorldTransform.argtypes = [ctypes.c_void_p]
        d.GdipDrawImageRectRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_float,
                                            ctypes.c_float, ctypes.c_float, ctypes.c_float,
                                            ctypes.c_float, ctypes.c_float, ctypes.c_float,
                                            ctypes.c_float, ctypes.c_int, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p]
        d.GdipDrawImagePointsRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(PointF), ctypes.c_int,
                                              ctypes.c_float, ctypes.c_float, ctypes.c_float,
                                              ctypes.c_float, ctypes.c_int, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_void_p]
        try:
            si = GdiplusStartupInput(1, None, False, False)
            if d.GdiplusStartup(ctypes.byref(self.token), ctypes.byref(si), None) != 0:
                return
            n, size = ctypes.c_uint32(0), ctypes.c_uint32(0)
            if d.GdipGetImageEncodersSize(ctypes.byref(n), ctypes.byref(size)) != 0:
                return
            buf = ctypes.create_string_buffer(size.value)
            if d.GdipGetImageEncoders(n, size, buf) != 0:
                return
            arr = ctypes.cast(buf, ctypes.POINTER(ImageCodecInfo))
            for i in range(n.value):                       # 找 image/jpeg 的编码器 CLSID
                mime = arr[i].MimeType
                if mime and "jpeg" in mime.lower():
                    self.jpeg_clsid = arr[i].Clsid
                    break
            self.ok = self.jpeg_clsid is not None
        except Exception:
            self.ok = False

    # -- 基础 --------------------------------------------------------
    def bitmap_from_dib(self, dib: Dib):
        gdi32.GdiFlush()                 # 先把 GDI 的绘制指令刷进 DIB 内存，再交给 GDI+
        ptr = ctypes.c_void_p()
        st = self.dll.GdipCreateBitmapFromScan0(dib.w, dib.h, dib.stride, PIXFMT_32BPP_RGB,
                                                dib.bits_ptr(), ctypes.byref(ptr))
        if st != 0 or not ptr:
            raise OSError("GdipCreateBitmapFromScan0 失败: %d" % st)
        return ptr

    def save_jpeg(self, dib: Dib, path: str, quality: int = JPEG_QUALITY) -> bool:
        if not self.ok:
            return False
        img = self.bitmap_from_dib(dib)
        try:
            params = EncoderParameters()
            params.Count = 1
            params.Parameter[0].Guid = QUALITY_GUID
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4                # EncoderParameterValueTypeLong
            q = ctypes.c_ulong(int(quality))
            params.Parameter[0].Value = ctypes.cast(ctypes.byref(q), ctypes.c_void_p)
            st = self.dll.GdipSaveImageToFile(img, os.path.abspath(path),
                                              ctypes.byref(self.jpeg_clsid),
                                              ctypes.byref(params))
            return st == 0
        finally:
            self.dll.GdipDisposeImage(img)

    # -- 合成 --------------------------------------------------------
    def resample(self, src: Dib, new_w: int, new_h: int, interp=INTERP_HQ_BICUBIC) -> Dib:
        """高质量缩放（用来做「先缩小再放大」的柔化 = 轻微模糊）。"""
        dst = Dib(new_w, new_h)
        dst.fill(PAPER)
        sb = self.bitmap_from_dib(src)
        db = self.bitmap_from_dib(dst)
        g = ctypes.c_void_p()
        try:
            if self.dll.GdipGetImageGraphicsContext(db, ctypes.byref(g)) != 0:
                raise OSError("GdipGetImageGraphicsContext 失败")
            self.dll.GdipSetInterpolationMode(g, interp)
            self.dll.GdipSetPixelOffsetMode(g, PIXEL_OFFSET_HQ)
            self.dll.GdipSetSmoothingMode(g, SMOOTH_ANTIALIAS)
            st = self.dll.GdipDrawImageRectRect(g, sb, 0.0, 0.0, float(new_w), float(new_h),
                                                0.0, 0.0, float(src.w), float(src.h), 2,
                                                None, None, None)
            if st != 0:
                raise OSError("GdipDrawImageRectRect 失败: %d" % st)
        finally:
            if g:
                self.dll.GdipDisposeImage(g)
            self.dll.GdipDisposeImage(sb)
            self.dll.GdipDisposeImage(db)
        return dst

    def compose_rotated(self, src: Dib, angle_deg: float, dst: Dib, center=None) -> None:
        """把 src 居中、旋转 angle_deg 后合成到 dst 上（dst 需自行打底）。

        这里用 GdipDrawImagePointsRect **直接给三个目标角点**，而不是「世界变换 +
        绕中心旋转」：世界变换的 Append/Prepend 语义跟「绕画布中心转」的直觉并不一致，
        实测整页会被甩到画幅左侧、左边距的题干正好被切掉半个字。
        给角点是所见即所得，缩放/定位/旋转一次算清楚。
        """
        cxp, cyp = center or (dst.w / 2.0, dst.h / 2.0)
        hw, hh = src.w / 2.0, src.h / 2.0
        th = math.radians(angle_deg)
        cos_t, sin_t = math.cos(th), math.sin(th)

        def corner(vx, vy):
            return PointF(cxp + vx * cos_t - vy * sin_t, cyp + vx * sin_t + vy * cos_t)

        pts = (PointF * 3)(corner(-hw, -hh), corner(hw, -hh), corner(-hw, hh))
        sb = self.bitmap_from_dib(src)
        db = self.bitmap_from_dib(dst)
        g = ctypes.c_void_p()
        try:
            if self.dll.GdipGetImageGraphicsContext(db, ctypes.byref(g)) != 0:
                raise OSError("GdipGetImageGraphicsContext 失败")
            self.dll.GdipSetInterpolationMode(g, INTERP_HQ_BICUBIC)
            self.dll.GdipSetPixelOffsetMode(g, PIXEL_OFFSET_HQ)
            self.dll.GdipSetSmoothingMode(g, SMOOTH_ANTIALIAS)
            st = self.dll.GdipDrawImagePointsRect(g, sb, pts, 3, 0.0, 0.0,
                                                  float(src.w), float(src.h), 2,
                                                  None, None, None)
            if st != 0:
                raise OSError("GdipDrawImagePointsRect 失败: %d" % st)
        finally:
            if g:
                self.dll.GdipDisposeImage(g)
            self.dll.GdipDisposeImage(sb)
            self.dll.GdipDisposeImage(db)

    def shutdown(self):
        if self.ok:
            try:
                self.dll.GdiplusShutdown(self.token)
            except Exception:
                pass
            self.ok = False


# ================================================================ 图像后处理（纯字节操作）
def multiply_rgb(dib: Dib, kr: float, kg: float, kb: float) -> None:
    """逐通道乘法（压暗/偏色）。用 bytes.translate + 扩展切片，C 速度。"""
    data = bytearray(bytes(dib.buffer()))
    for off, k in ((0, kb), (1, kg), (2, kr)):
        if abs(k - 1.0) < 1e-6:
            continue
        table = bytes(min(255, int(v * k + 0.5)) for v in range(256))
        data[off::4] = data[off::4].translate(table)
    ctypes.memmove(dib.buffer(), bytes(data), len(data))


def blend_mask(dst: Dib, mask: Dib, ox: int, oy: int, strength: float,
               ink=WATERMARK_INK) -> None:
    """把「白底黑字」的水印遮罩按强度混进目标，只混有墨的像素（不留色块）。"""
    dbuf = bytearray(bytes(dst.buffer()))
    mbuf = bytes(mask.buffer())
    ir, ig, ib = ink
    for y in range(mask.h):
        dy = oy + y
        if not (0 <= dy < dst.h):
            continue
        mrow = y * mask.stride
        drow = dy * dst.stride
        for x in range(mask.w):
            mo = mrow + x * 4
            v = mbuf[mo]                      # 遮罩是灰阶：白底黑字
            if v >= 250:
                continue
            cov = (255 - v) / 255.0 * strength
            if cov <= 0.002:
                continue
            do = drow + (ox + x) * 4
            if not (0 <= ox + x < dst.w):
                continue
            k = 1.0 - cov
            dbuf[do + 0] = _clamp(int(dbuf[do + 0] * k + ib * cov))
            dbuf[do + 1] = _clamp(int(dbuf[do + 1] * k + ig * cov))
            dbuf[do + 2] = _clamp(int(dbuf[do + 2] * k + ir * cov))
    ctypes.memmove(dst.buffer(), bytes(dbuf), len(dbuf))


# ================================================================ BMP 兜底编码
def write_bmp24(dib: Dib, path: str) -> None:
    """GDI+ 不可用时的兜底：24bpp 自底向上 BMP。"""
    row_raw = dib.stride
    row_out = (dib.w * 3 + 3) & ~3
    data = bytes(dib.buffer())
    pad = b"\x00" * (row_out - dib.w * 3)
    body = bytearray()
    for y in range(dib.h - 1, -1, -1):        # DIB 是顶朝下，BMP 要自底向上
        line = data[y * row_raw:(y + 1) * row_raw]
        row = bytearray(dib.w * 3)
        row[0::3] = line[0::4]                # B
        row[1::3] = line[1::4]                # G
        row[2::3] = line[2::4]                # R
        body += row + pad
    file_header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(body), 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, dib.w, dib.h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(file_header + info + bytes(body))


# ================================================================ 版面构件
def page_header(c: Canvas, title: str, subtitle: str, rnd) -> int:
    """页眉：居中标题（DrawTextW）+ 副标题 + 姓名/班级/得分栏 + 分隔线。返回正文起始 y。"""
    y = 108
    # 标题用 DrawTextW（DT_CENTER|DT_VCENTER|DT_SINGLELINE）在版心矩形里居中 + 垂直居中
    c.text_box(MARGIN, y - 44, PAGE_W - MARGIN, y + 14, title, 40, color=INK,
               families=F_PRINT, bold=True)
    y += 46
    sw = c.measure(subtitle, 25, F_PRINT)
    c.text(MARGIN + 4, y, subtitle, size=25, color=INK, families=F_PRINT)
    y += 30

    # 得分栏（真实试卷右上角那种小方框）
    bx2, by1 = PAGE_W - MARGIN, y - 62
    c.frame_rect(bx2 - 108, by1, bx2, by1 + 62, color=INK_LIGHT, width=1)
    c.text(bx2 - 96, by1 + 30, "得分", size=24, color=INK_LIGHT, families=F_PRINT)

    x = MARGIN + 4
    for label, w in (("姓名：", 150), ("班级：", 130), ("学号：", 120)):
        x = c.text(x, y, label, size=26, color=INK, families=F_PRINT)
        c.line(x + 2, y + 7, x + w, y + 7, color=INK_LIGHT, width=1)
        x += w + 26
    y += 26
    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=2)
    y += 6
    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=1)
    return y + 52


def page_footer(c: Canvas, text: str = "第 1 页  共 1 页") -> None:
    c.text_box(MARGIN, PAGE_H - 78, PAGE_W - MARGIN, PAGE_H - 40, text, 24,
               color=INK_LIGHT, families=F_PRINT)


def bracket_blank(c: Canvas, x, base, width=72, size=30, color=INK):
    """画「（　　）」并返回中间空档的 (左 x, 宽度)，供手写答案落位。"""
    x = c.text(x, base, "（", size=size, color=color, families=F_PRINT)
    gap_x = x
    x = c.text(x + width, base, "）", size=size, color=color, families=F_PRINT)
    return gap_x, width, x


def option_row(c: Canvas, base, items, size=30, gap=46, color=INK):
    """一行选项（A. / B. / C. / D.），自动均分间距。"""
    widths = [c.measure(it, size, F_PRINT) for it in items]
    total = sum(widths) + gap * (len(items) - 1)
    x = MARGIN + 40
    if total > TEXT_W - 60:                     # 太挤就压缩间距（甚至换两行由调用方决定）
        gap = max(16, int((TEXT_W - 60 - sum(widths)) / max(1, len(items) - 1)))
    for it, w in zip(items, widths):
        c.text(x, base, it, size=size, color=color, families=F_PRINT)
        x += w + gap
    return x


def underline_blank(c: Canvas, x, y, width=190, color=INK_LIGHT, width_px=1):
    """填空处的印刷横线。"""
    c.line(x, y, x + width, y, color=color, width=width_px)
    return x + width


def text_fit(c: Canvas, x, base, s, size, max_x, color=INK, families=None, bold=False,
             min_size=20):
    """画一行印刷体，若超出版心就自动缩小字号，保证不会被页面裁掉。"""
    families = families or F_PRINT
    size0 = size
    while size > min_size and x + c.measure(s, size, families, bold) > max_x:
        size -= 1
    if size != size0:
        print("  · 适配版心：字号 %d → %d（%s...）" % (size0, size, s[:20]))
    return c.text(x, base, s, size=size, color=color, families=families, bold=bold)


def answer_area(c, y0, y1, label="草稿区", lines=6):
    """页面下半部的留白区：浅灰边框 + 淡横格线，填补真实试卷该有的空白，
    避免整页只有上半部分有内容、看起来像合成图。"""
    if y1 - y0 < 160:
        return
    c.frame_rect(MARGIN, y0, PAGE_W - MARGIN, y1, color=(188, 190, 192), width=1)
    c.text(MARGIN + 16, y0 + 32, label, size=23, color=INK_LIGHT, families=F_PRINT)
    step = (y1 - y0 - 56) / float(max(1, lines))
    yy = y0 + 56 + step
    for _ in range(lines):
        c.line(MARGIN + 26, yy, PAGE_W - MARGIN - 26, yy, color=(210, 212, 214), width=1)
        yy += step


def determinant(c: Canvas, x, y, rows, cell_w=86, cell_h=54, size=32, color=INK,
                label="D = "):
    """画行列式（左右竖线 + 数字格）。返回整体高度。"""
    if label:
        x = c.text(x, y + cell_h - 14, label, size=size, color=color, families=F_MATH)
    h = cell_h * len(rows)
    bar_w = 4
    c.fill_rect(x + 6, y, x + 6 + bar_w, y + h, color)
    for r, row in enumerate(rows):
        for cidx, val in enumerate(row):
            cx = x + 26 + cidx * cell_w
            w = c.measure(str(val), size, F_MATH)
            c.text(cx + (cell_w - 24 - w) / 2.0, y + r * cell_h + cell_h - 16, str(val),
                   size=size, color=color, families=F_MATH)
    x2 = x + 26 + len(rows[0]) * cell_w
    c.fill_rect(x2 - 4, y, x2, y + h, color)
    return h


def incline_figure(c: Canvas, x, y, w=320, h=180):
    """斜面示意图：直角三角形 + 物块 + θ 角标 + 加速度箭头。"""
    A = (x, y + h)
    B = (x + w, y + h)
    C = (x + w, y)
    c.polygon([A, B, C], color=(120, 126, 136), width=2, fill=(238, 238, 233))
    # 直角标记
    c.line(B[0] - 16, B[1], B[0] - 16, B[1] - 16, color=(120, 126, 136), width=1)
    c.line(B[0] - 16, B[1] - 16, B[0], B[1] - 16, color=(120, 126, 136), width=1)
    # θ 角弧 + 标注
    r = 62
    pts = []
    for i in range(19):
        t = math.radians(30.0 * i / 18.0)
        pts.append((A[0] + r * math.cos(t), A[1] - r * math.sin(t)))
    c.stroke(pts, color=INK, width=2, jitter=0.0, rnd=random.Random(0))
    c.text(A[0] + 66, A[1] - 14, "θ", size=32, color=INK, families=F_MATH)
    # 物块（贴着斜面）
    d = (C[0] - A[0], C[1] - A[1])
    L = math.hypot(*d)
    ux, uy = d[0] / L, d[1] / L
    nx, ny = uy, -ux
    t0 = 0.46
    px, py = A[0] + d[0] * t0, A[1] + d[1] * t0
    s = 40
    c.polygon([(px, py), (px + ux * s, py + uy * s),
               (px + ux * s + nx * s * 0.8, py + uy * s + ny * s * 0.8),
               (px + nx * s * 0.8, py + ny * s * 0.8)],
              color=INK, width=2, fill=(206, 210, 216))
    c.text(px + nx * 46, py + ny * 46, "m", size=28, color=INK, families=F_MATH)
    # 沿斜面向下的加速度箭头（物块是下滑，箭头必须朝坡下）
    ax, ay = A[0] + d[0] * 0.44, A[1] + d[1] * 0.44
    ex, ey = ax - ux * 56, ay - uy * 56
    c.line(ax, ay, ex, ey, color=INK, width=2)
    c.line(ex, ey, ex + ux * 15 + nx * 8, ey + uy * 15 + ny * 8, color=INK, width=2)
    c.line(ex, ey, ex + ux * 15 - nx * 8, ey + uy * 15 - ny * 8, color=INK, width=2)
    c.text(ex - 34, ey + 24, "a", size=28, color=INK, families=F_MATH)
    return y + h


# ================================================================ 样本内容定义
# 答案状态既驱动绘制（对勾/叉/空白），又是 ground_truth.json 的唯一数据源，
# 两者共用同一份常量，保证「画的」和「写的」永远一致。
S1_STATUS = ["correct", "wrong", "blank"]
S2_STATUS = ["correct", "correct", "blank"]
S3_STATUS = ["wrong"]
S4_STATUS = ["wrong", "correct", "correct"]
S5_STATUS = ["wrong"]
S6_STATUS = list(S1_STATUS)          # s6 与 s1 内容相同


# ---------------------------------------------------------------- s1 高中数学 选择题
def build_s1(c: Canvas, rnd) -> None:
    y = page_header(c, "高中数学 · 函数与导数 单元测验", "一、选择题（每小题 5 分，共 15 分）", rnd)
    body, opt = 30, 30
    step = 176                                     # 题间距（拉开一点，别挤在页面上半截）

    # ---- 第 1 题：学生选 B（正确），老师红笔打对勾 ----
    x = c.text(MARGIN, y, "1. 已知函数 f(x)=2x^2-3x+1，则 f(2) 的值为", size=body,
               color=INK, families=F_PRINT)
    bx, bw, ex = bracket_blank(c, x + 8, y, width=76)          # 括号永远印着，写不写看状态
    option_row(c, y + 52, ["A. 1", "B. 3", "C. 7", "D. 9"], size=opt)
    if S1_STATUS[0] == "correct":
        aw = c.measure("B", 32, F_HAND_LAT)
        hand_latin(c, bx + (bw - aw) / 2.0, y + 2, "B", size=32, rnd=rnd)
        hand_check(c, ex + 34, y - 8, size=30, rnd=rnd)        # 红笔 ✓
    y += step

    # ---- 第 2 题：学生选 B（错误，正确答案 C），老师红笔打叉 ----
    x = c.text(MARGIN, y, "2. 函数 f(x)=x^3-3x^2+2 的极小值是", size=body,
               color=INK, families=F_PRINT)
    bx2, bw2, ex2 = bracket_blank(c, x + 8, y, width=76)
    option_row(c, y + 52, ["A. 2", "B. 0", "C. -2", "D. -4"], size=opt)
    if S1_STATUS[1] == "wrong":
        aw = c.measure("B", 32, F_HAND_LAT)
        hand_latin(c, bx2 + (bw2 - aw) / 2.0, y + 2, "B", size=32, rnd=rnd)
        hand_cross(c, ex2 + 40, y - 6, size=34, rnd=rnd)       # 红笔 ✗
        hand_text(c, ex2 + 84, y - 4, "应选 C", size=25, rnd=rnd)
        # 红笔圈住题号（老师标记「这题错了」），按题号实测宽度居中
        nw = c.measure("2.", body, F_PRINT)
        hand_circle(c, MARGIN + nw / 2.0, y - 11, nw / 2.0 + 7, 19, rnd=rnd)
    y += step

    # ---- 第 3 题：空白未作答（括号印着，但没有任何手写痕迹） ----
    x = c.text(MARGIN, y, "3. 若 sinα=3/5，且 α 是第二象限角，则 cosα 的值是", size=body,
               color=INK, families=F_PRINT)
    bracket_blank(c, x + 8, y, width=76)
    option_row(c, y + 52, ["A. 3/5", "B. -3/5", "C. 4/5", "D. -4/5"], size=opt)
    y += step

    # ---- 页脚提示 + 草稿区（填补下方留白） ----
    text_fit(c, MARGIN, y, "提示：请把答案写在题后的括号内，只写选项字母。", 24,
             PAGE_W - MARGIN, color=INK_LIGHT)
    answer_area(c, y + 34, PAGE_H - 118, label="草稿区", lines=6)
    page_footer(c)


# ---------------------------------------------------------------- s2 大学微积分 填空题
def build_s2(c: Canvas, rnd) -> None:
    y = page_header(c, "高等数学 · 导数与积分 填空练习", "一、填空题（每小题 6 分，共 18 分）", rnd)
    body = 31
    step = 132

    # ---- 第 1 题：不定积分，学生写出正确答案 ----
    x = c.text(MARGIN, y, "1.  ∫(2x+1)dx =", size=body, color=INK, families=F_MATH)
    bx = x + 26
    underline_blank(c, bx, y + 8, 300)
    if S2_STATUS[0] != "blank":
        hand_latin(c, bx + 16, y - 6, "x^2+x+C", size=32, rnd=rnd)
    y += step

    # ---- 第 2 题：复合函数求导，学生写出正确答案 ----
    x = c.text(MARGIN, y, "2.  d/dx [ln(x^2+1)] =", size=body, color=INK, families=F_MATH)
    bx = x + 26
    underline_blank(c, bx, y + 8, 340)
    if S2_STATUS[1] != "blank":
        hand_latin(c, bx + 16, y - 6, "2x/(x^2+1)", size=32, rnd=rnd)
    y += step

    # ---- 第 3 题：定积分，整题空白未作答（横线印着，没有任何手写痕迹） ----
    x = c.text(MARGIN, y, "3.  ∫_0^1 e^x dx =", size=body, color=INK, families=F_MATH)
    bx = x + 38
    underline_blank(c, bx, y + 8, 300)
    if S2_STATUS[2] != "blank":
        hand_latin(c, bx + 16, y - 6, "e-1", size=32, rnd=rnd)
    y += step

    # ---- 印刷的公式回顾区 ----
    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=1)
    y += 46
    c.text(MARGIN, y, "常用公式：", size=26, color=INK, families=F_PRINT)
    y += 46
    for line in ("∫ x^n dx = x^{n+1}/(n+1) + C   (n ≠ -1)",
                 "[ln x]' = 1/x ,   [e^x]' = e^x",
                 "∫_a^b f(x)dx = F(b) - F(a)"):
        c.text(MARGIN + 40, y, line, size=28, color=INK, families=F_MATH)
        y += 48
    answer_area(c, y + 26, PAGE_H - 118, label="草稿区", lines=6)
    page_footer(c)


# ---------------------------------------------------------------- s3 大学线性代数 解答题
def build_s3(c: Canvas, rnd) -> None:
    y = page_header(c, "线性代数 · 行列式 解答题", "三、解答题（本题 12 分）", rnd)
    body = 30

    c.text(MARGIN, y, "1.（12 分）计算下列三阶行列式 D 的值：", size=body, color=INK,
           families=F_PRINT)
    y += 40
    h = determinant(c, MARGIN + 120, y, [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "10"]])
    y += h + 48

    c.text(MARGIN, y, "解：", size=body, color=INK, families=F_PRINT)
    y += 54

    # ---- 学生手写解题步骤：第二、三项的代数余子式符号写错 ----
    hand_x = MARGIN + 26
    hand_text(c, hand_x, y, "按第一行展开：", size=32, rnd=rnd)
    y += 62

    expr = "D = 1×(5×10-6×8) + 2×(4×10-6×7) - 3×(4×8-5×7)"
    hand_latin(c, hand_x, y, expr, size=30, rnd=rnd)
    # 老师只在写错的两个符号上划线（按前缀实测宽度精确落位，不是拍脑袋取比例）
    for sub in ("+ 2×", "- 3×"):
        off = c.measure(expr.split(sub)[0], 30, F_HAND_LAT)
        hand_underline(c, hand_x + off - 2, hand_x + off + 30, y + 9, rnd=rnd, wobble=2.0)
    w = c.measure(expr, 30, F_HAND_LAT)
    hand_cross(c, hand_x + w + 58, y + 6, size=34, rnd=rnd)
    hand_text(c, hand_x + w + 102, y + 4, "符号错", size=30, rnd=rnd)
    y += 62
    for txt in ("  = 1×2 + 2×(-2) - 3×(-3)", "  = 2 - 4 + 9 = 7"):
        hand_latin(c, hand_x, y, txt, size=30, rnd=rnd)
        y += 62

    y += 8
    hand_text(c, hand_x, y, "所以 D = 7", size=32, rnd=rnd)
    y += 56

    # ---- 老师红笔订正 ----
    hand_cross(c, MARGIN + 40, y + 40, size=36, rnd=rnd)
    hand_text_fit(c, MARGIN + 90, y + 44,
                  "符号错：第二项应为 -2×(4×10-6×7)，第三项应为 +3×(4×8-5×7)，正确答案 D = -3",
                  27, PAGE_W - MARGIN - 10, rnd=rnd)
    y += 96
    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=1)
    text_fit(c, MARGIN, y + 36, "教师批注：按第一行展开时，代数余子式的符号为 + - + 交替。",
             24, PAGE_W - MARGIN, color=INK_LIGHT)
    answer_area(c, y + 66, PAGE_H - 118, label="解答区", lines=5)
    page_footer(c)


# ---------------------------------------------------------------- s4 高中英语
def build_s4(c: Canvas, rnd) -> None:
    y = page_header(c, "高中英语 · 语法与词汇 专项练习", "一、选择与填空（每小题 5 分，共 15 分）", rnd)
    body = 30
    step = 192

    # ---- 第 1 题：选错（正确答案 C），红笔打叉 ----
    x = c.text(MARGIN, y, "1.  — How long have you ______ the dictionary?", size=body,
               color=INK, families=F_PRINT)
    bx, bw, ex = bracket_blank(c, x + 16, y, width=76)
    text_fit(c, MARGIN + 40, y + 40, "（批注：how long 表示一段时间，须与延续性动词连用）",
             24, PAGE_W - MARGIN, color=INK_LIGHT)
    option_row(c, y + 88, ["A. borrowed", "B. lent", "C. kept", "D. bought"], size=28)
    if S4_STATUS[0] == "wrong":
        aw = c.measure("B", 32, F_HAND_LAT)
        hand_latin(c, bx + (bw - aw) / 2.0, y + 2, "B", size=32, rnd=rnd)
        hand_cross(c, ex + 40, y - 6, size=34, rnd=rnd)
        hand_text(c, ex + 84, y - 4, "延续性动词！", size=25, rnd=rnd)
        nw = c.measure("1.", body, F_PRINT)
        hand_circle(c, MARGIN + nw / 2.0, y - 11, nw / 2.0 + 7, 19, rnd=rnd)
    y += step

    # ---- 第 2 题：被动语态，学生选 B（正确） ----
    x = c.text(MARGIN, y, "2.  The sports meeting ______ next Friday.", size=body,
               color=INK, families=F_PRINT)
    bx2, bw2, ex2 = bracket_blank(c, x + 16, y, width=76)
    text_fit(c, MARGIN + 40, y + 40, "（批注：主语与动词之间是被动关系）", 24,
             PAGE_W - MARGIN, color=INK_LIGHT)
    option_row(c, y + 88, ["A. will hold", "B. will be held", "C. holds", "D. is holding"],
               size=28)
    if S4_STATUS[1] == "correct":
        aw = c.measure("B", 32, F_HAND_LAT)
        hand_latin(c, bx2 + (bw2 - aw) / 2.0, y + 2, "B", size=32, rnd=rnd)
    y += step

    # ---- 第 3 题：非谓语填空，学生写 getting（正确） ----
    # 印刷横线加宽、括号提示右移，手写答案落在横线正上方，不会压到印刷内容
    x = c.text(MARGIN, y, "3.  He is used to", size=body, color=INK, families=F_PRINT)
    bx3 = x + 20
    underline_blank(c, bx3, y + 8, 172)
    c.text(bx3 + 186, y, "(get) up early in the morning.", size=body, color=INK,
           families=F_PRINT)
    text_fit(c, MARGIN + 40, y + 40, "（批注：be used to 中的 to 是介词，后接动名词）",
             24, PAGE_W - MARGIN, color=INK_LIGHT)
    if S4_STATUS[2] != "blank":
        hand_latin(c, bx3 + 22, y - 8, "getting", size=32, rnd=rnd)
    y += step

    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=1)
    text_fit(c, MARGIN, y + 36, "Words & Expressions:  borrow / lend / keep 的用法区别。",
             24, PAGE_W - MARGIN, color=INK_LIGHT)
    answer_area(c, y + 66, PAGE_H - 118, label="草稿区", lines=5)
    page_footer(c)


# ---------------------------------------------------------------- s5 高中物理 计算题
def build_s5(c: Canvas, rnd) -> None:
    y = page_header(c, "高中物理 · 牛顿第二定律 计算题", "四、计算题（本题 12 分）", rnd)
    body = 30
    prob_y = y                                     # 题干首行的基线（圈题号要用）

    text_fit(c, MARGIN, y, "1.（12 分）如图，质量 m = 2 kg 的物块由静止沿倾角 θ = 30° 的固定斜面下滑，",
             body, PAGE_W - MARGIN)
    y += 46
    text_fit(c, MARGIN + 26, y, "物块与斜面间的动摩擦因数 μ = 0.25，取 g = 10 m/s^2，sin30° = 0.5，",
             body, PAGE_W - MARGIN)
    y += 46
    text_fit(c, MARGIN + 26, y, "cos30° ≈ 0.87。求物块下滑的加速度 a 的大小（结果保留三位有效数字）。",
             body, PAGE_W - MARGIN)
    y += 38
    incline_figure(c, MARGIN + 90, y, 320, 180)
    y += 186 + 36

    c.text(MARGIN, y, "解：", size=body, color=INK, families=F_PRINT)
    y += 56
    hand_x = MARGIN + 26
    for txt, kind in (("由牛顿第二定律：ma = mg sinθ - μmg cosθ", "cjk"),
                      ("a = g sinθ - μg cosθ", "lat"),
                      ("  = 10×0.5 - 0.25×10×0.87", "lat"),
                      ("  = 5 - 2.175 = 2.815", "lat")):
        if kind == "cjk":
            hand_text(c, hand_x, y, txt, size=30, rnd=rnd)
        else:
            hand_latin(c, hand_x, y, txt, size=30, rnd=rnd)
        if txt.strip().startswith("= 5"):        # 学生的算术也错了（5-2.175 应为 2.825）
            # 手写体数字在字身框里位置偏低，画下滑线容易看成删除线，
            # 这里改成老师在这一步旁边直接打个小叉，语义更清楚
            w = c.measure(txt, 30, F_HAND_LAT)
            hand_cross(c, hand_x + w + 34, y + 6, size=28, rnd=rnd)
        y += 68

    # 学生最终答案：数值算错 + 漏写单位
    end_x = hand_text(c, hand_x, y, "所以 a ≈ 2.815", size=32, rnd=rnd)
    if S5_STATUS[0] == "wrong":
        hand_cross(c, end_x + 44, y + 6, size=36, rnd=rnd)
        hand_text(c, end_x + 90, y + 4, "单位漏了", size=30, rnd=rnd)
        nw = c.measure("1.", body, F_PRINT)
        hand_circle(c, MARGIN + nw / 2.0, prob_y - 11, nw / 2.0 + 7, 19, rnd=rnd)
    y += 56
    text_fit(c, hand_x, y, "（结果应写成 2.83 m/s^2 的形式，注意单位与有效数字）", 24,
             PAGE_W - MARGIN, color=INK_LIGHT)
    y += 62
    c.line(MARGIN, y, PAGE_W - MARGIN, y, color=RULE, width=1)
    text_fit(c, MARGIN, y + 36, "教师批注：受力分析正确，代入数据后减法出错，且最终结果未写单位。",
             24, PAGE_W - MARGIN, color=INK_LIGHT)
    answer_area(c, y + 66, PAGE_H - 118, label="解答区", lines=5)
    page_footer(c)


# ================================================================ 退化处理（s6）
DESK = (216, 214, 209)               # s6 背景（桌面）基色


def make_desk(seed: int, w: int = PAGE_W, h: int = PAGE_H, base=DESK) -> Dib:
    """s6 的「桌面」背景：平滑光照渐变 + 极轻颗粒。

    这里刻意不用 make_paper 那种逐行随机噪声 —— 整片平坦底色上叠高对比的行噪声，
    缩放预览/JPEG 色度下采样时会糊成一条条彩色横纹，一眼就是合成图。
    改成 128 级纵向光照 × 逐像素横向渐变，只有 ±1 的颗粒。
    """
    rnd = random.Random(seed)
    dib = Dib(w, h)
    steps = 128
    rows = []
    for i in range(steps):
        fv = 1.045 - 0.12 * (i / (steps - 1.0))      # 上亮下暗
        row = bytearray(w * 4)
        for x in range(w):
            f = fv * (0.975 + 0.05 * (x / (w - 1.0)))  # 左暗右亮
            d = rnd.randint(-1, 1)
            o = x * 4
            row[o + 0] = _clamp(int(base[2] * f) + d)
            row[o + 1] = _clamp(int(base[1] * f) + d)
            row[o + 2] = _clamp(int(base[0] * f) + d)
            row[o + 3] = 255
        rows.append(bytes(row))
    buf = dib.buffer()
    for y in range(h):
        r = rows[min(steps - 1, y * steps // h)]
        ctypes.memmove(ctypes.byref(buf, y * dib.stride), r, dib.stride)
    return dib


def photo_like(gp: GdiPlus, src: Dib, angle: float = 7.0) -> Dib:
    """模拟「手机拍歪 + 手抖 + 光线偏暗 + 带水印」的一张照片。

    1) 先按角度算出「整页都能装进画面」的缩放比（否则旋转后四角会被裁掉、
       连题干都会被切掉一半），缩小到该比例；
    2) 用高质量插值放大回原尺寸的同时旋转 7° —— 缩-放两级低通就是轻微模糊；
    3) 桌面底色打底，留出纸边，做成「整张纸躺在桌上」的实拍感；
    4) 整体压暗偏暖；
    5) 左上角混入半透明水印文字（只混有墨的像素，不留色块）。
    """
    th = math.radians(angle)
    cos_t, sin_t = abs(math.cos(th)), abs(math.sin(th))
    # 让「旋转后的外接矩形」装进画面；再乘 0.93 留出桌面边距 ——
    # 只按 1.0 算的话旋转后四角正好顶到画幅边缘，左边距的题干会被切掉半个字
    scale = 0.93 * min(src.w / (src.w * cos_t + src.h * sin_t),
                       src.h / (src.w * sin_t + src.h * cos_t))
    small = gp.resample(src, max(1, int(src.w * scale)), max(1, int(src.h * scale)))

    out = make_desk(99)                                  # 桌面背景（平滑光照渐变）
    gp.compose_rotated(small, angle, out)                # 旋转后居中合成
    small.close()
    multiply_rgb(out, kr=0.91, kg=0.89, kb=0.87)         # 整体压暗（略偏暖，室内光的感觉）

    # 水印遮罩：白底黑字，整体再倾斜 14°，只把有墨的像素混进去
    mask = Dib(940, 300)
    mask.fill((255, 255, 255))
    mc = Canvas(mask)
    for txt, size, esc, my in (("EXERCISE ARCHIVE", 54, -140, 70),
                               ("内部资料 · 请勿外传", 40, -140, 156)):
        hf = mc.font(size, "SimHei", bold=True, esc=esc)
        old = gdi32.SelectObject(mc.hdc, hf)
        gdi32.SetTextColor(mc.hdc, 0x000000)
        gdi32.TextOutW(mc.hdc, 26, my, txt, len(txt))
        gdi32.SelectObject(mc.hdc, old)
    gdi32.GdiFlush()
    blend_mask(out, mask, 24, 40, 0.32)
    mask.close()
    return out


# ================================================================ 样本清单（同时是 ground truth 数据源）
PAGES = [
    {
        "file": "s1_math_choice",
        "builder": build_s1,
        "seed": 20240301,
        "meta": {
            "subject": "数学",
            "subject_detail": "高中数学·函数与导数（选择题）",
            "grade": "高中",
            "question_count": 3,
            "types": ["选择题", "选择题", "选择题"],
            "expected_statuses": S1_STATUS,
        },
    },
    {
        "file": "s2_math_fill",
        "builder": build_s2,
        "seed": 20240302,
        "meta": {
            "subject": "数学",
            "subject_detail": "大学微积分（导数与积分、填空题）",
            "grade": "大学",
            "question_count": 3,
            "types": ["填空题", "填空题", "填空题"],
            "expected_statuses": S2_STATUS,
        },
    },
    {
        "file": "s3_math_solution",
        "builder": build_s3,
        "seed": 20240303,
        "meta": {
            "subject": "数学",
            "subject_detail": "大学线性代数（行列式、解答题）",
            "grade": "大学",
            "question_count": 1,
            "types": ["解答题"],
            "expected_statuses": S3_STATUS,
        },
    },
    {
        "file": "s4_english",
        "builder": build_s4,
        "seed": 20240304,
        "meta": {
            "subject": "英语",
            "subject_detail": "高中英语·语法与词汇（时态与非谓语）",
            "grade": "高中",
            "question_count": 3,
            "types": ["选择题", "选择题", "填空题"],
            "expected_statuses": S4_STATUS,
        },
    },
    {
        "file": "s5_physics",
        "builder": build_s5,
        "seed": 20240305,
        "meta": {
            "subject": "物理",
            "subject_detail": "高中物理·牛顿第二定律（斜面模型计算题）",
            "grade": "高中",
            "question_count": 1,
            "types": ["计算题"],
            "expected_statuses": S5_STATUS,
        },
    },
    {
        "file": "s6_math_tilted_blur",
        "builder": build_s1,                     # 与 s1 内容完全相同
        "seed": 20240301,                        # 同种子 → 纸纹/抖动完全一致
        "degrade": True,
        "meta": {
            "subject": "数学",
            "subject_detail": "高中数学·函数与导数（选择题，s1 的旋转模糊压暗带水印版本）",
            "grade": "高中",
            "question_count": 3,
            "types": ["选择题", "选择题", "选择题"],
            "expected_statuses": S6_STATUS,
        },
    },
]


# ================================================================ 生成
def render_page(spec, gp: GdiPlus) -> Dib:
    """按样本定义画一页（页眉/题目/手写批注）。"""
    rnd = random.Random(spec["seed"])
    dib = make_paper(spec["seed"])
    canvas = Canvas(dib)
    spec["builder"](canvas, rnd)
    gdi32.GdiFlush()
    if spec.get("degrade"):
        out = photo_like(gp, dib)
        dib.close()
        return out
    return dib


def save_image(gp: GdiPlus, dib: Dib, stem: str):
    """返回 (实际文件名, 是否用 JPEG)。GDI+ 不可用时兜底写 BMP。"""
    if gp.ok:
        path = os.path.join(OUT_DIR, stem + ".jpg")
        if gp.save_jpeg(dib, path):
            return os.path.basename(path)
    path = os.path.join(OUT_DIR, stem + ".bmp")
    write_bmp24(dib, path)
    return os.path.basename(path)


# ================================================================ 校验
def jpeg_dimensions(data: bytes):
    """只解析 JPEG 的 SOF 段拿宽高，同时确认 SOI/EOI 完整。"""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise ValueError("缺少 SOI 标记，不是 JPEG")
    if data[-2:] != b"\xff\xd9":
        raise ValueError("缺少 EOI 标记，文件可能被截断")
    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return int(w), int(h)
        i += 2 + seg_len
    raise ValueError("没找到 SOF 尺寸段")


def bmp_dimensions(data: bytes):
    if len(data) < 54 or data[0:2] != b"BM":
        raise ValueError("不是 BMP")
    w, h = struct.unpack_from("<ii", data, 18)
    return int(w), abs(int(h))


def verify(files) -> bool:
    """重新读回每个文件：打印字节数与像素尺寸，缺文件/过小/尺寸不对就判失败。"""
    print("\n===== 产物校验 =====")
    header = "%-28s %12s %14s  %s" % ("文件名", "字节数", "像素尺寸", "结果")
    print(header)
    print("-" * 74)
    all_ok = True
    for name in files:
        path = os.path.join(OUT_DIR, name)
        if not os.path.exists(path):
            print("%-28s %12s %14s  %s" % (name, "-", "-", "失败：文件不存在"))
            all_ok = False
            continue
        size = os.path.getsize(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
            if name.lower().endswith((".jpg", ".jpeg")):
                w, h = jpeg_dimensions(data)
                kind = "JPEG"
            else:
                w, h = bmp_dimensions(data)
                kind = "BMP"
        except Exception as exc:
            print("%-28s %12d %14s  %s" % (name, size, "-", "失败：%s" % exc))
            all_ok = False
            continue
        problems = []
        if size < MIN_BYTES:
            problems.append("小于 %d 字节" % MIN_BYTES)
        if (w, h) != (PAGE_W, PAGE_H):
            problems.append("尺寸应为 %dx%d" % (PAGE_W, PAGE_H))
        note = "%s %dx%d  OK" % (kind, w, h) if not problems else "失败：" + "，".join(problems)
        print("%-28s %12d %14s  %s" % (name, size, "%dx%d" % (w, h), note))
        if problems:
            all_ok = False
    print("-" * 74)
    print("结论：%s" % ("全部通过" if all_ok else "存在失败项"))
    return all_ok


# ================================================================ 主流程
def main() -> int:
    from exerarchive import logging_setup

    logging_setup.enable_utf8_console()
    os.makedirs(OUT_DIR, exist_ok=True)
    print("输出目录：%s" % OUT_DIR)

    gp = GdiPlus()
    print("GDI+ 可用：%s%s" % (gp.ok, "" if gp.ok else "（将兜底输出 24bpp BMP）"))

    files, ground_truth = [], {}
    try:
        for idx, spec in enumerate(PAGES, 1):
            dib = render_page(spec, gp)
            name = save_image(gp, dib, spec["file"])
            dib.close()
            files.append(name)
            ground_truth[name] = dict(spec["meta"])
            print("[%d/%d] 已生成 %s" % (idx, len(PAGES), name))
    finally:
        gp.shutdown()

    # ---- ground_truth.json：字段与绘制内容一一对应 ----
    gt_path = os.path.join(OUT_DIR, "ground_truth.json")
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(ground_truth, f, ensure_ascii=False, indent=2)
        f.write("\n")

    ok = verify(files)
    print("标准答案：%s" % gt_path)
    print("样本数量：%d" % len(files))
    if not ok:
        print("生成失败：校验未通过。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
