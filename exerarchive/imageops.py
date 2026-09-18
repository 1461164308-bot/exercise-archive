# -*- coding: utf-8 -*-
"""图像读写与预处理（零第三方依赖）。

后端优先级（自动探测，用户无需配置）：

1. **Pillow**（若用户自行装了，速度最快，支持格式最全）
2. **Windows GDI+**（系统自带 gdiplus.dll，JPEG/PNG/BMP/GIF/TIFF 全部支持，速度快）
3. **纯 Python 解码器**（PNG / BMP，任何平台都能跑，保证「极端环境下也能用」）
4. **macOS sips 命令**（系统自带，用于把 JPEG 转成 PNG 再交给纯 Python 解码）

对外只暴露 :class:`Image` 与少量函数：读图、保存、裁剪、纠偏、提亮、去水印、
质量评分、生成给模型用的压缩图、生成给 tkinter 显示的 PPM 数据。
"""

from __future__ import annotations

import base64
import io
import math
import os
import struct
import subprocess
import sys
import zlib
from typing import List, Optional, Sequence, Tuple

from .gdiplus import IS_WINDOWS

try:  # 可选依赖：装了就用，没装也完全不影响
    from PIL import Image as _PILImage  # type: ignore

    HAS_PIL = True
except Exception:  # pragma: no cover
    _PILImage = None
    HAS_PIL = False


# --------------------------------------------------------------------------- #
# 纯 Python 解码器：PNG
# --------------------------------------------------------------------------- #
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_unfilter(raw: bytes, width: int, height: int, bpp: int, stride: int) -> bytearray:
    """逐行反滤波（PNG filter type 0-4）。"""
    out = bytearray(stride * height)
    pos = 0
    prev = bytearray(stride)
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ft == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return out


def _decode_png(data: bytes) -> Tuple[int, int, bytearray]:
    """解码 PNG。支持 8/16 位、灰度/RGB/调色板/带 Alpha、Adam7 隔行。"""
    if not data.startswith(_PNG_SIG):
        raise ValueError("不是 PNG 文件")
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    idat = bytearray()
    palette = b""
    trns = b""
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk[:13]
            )
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if width <= 0 or height <= 0:
        raise ValueError("PNG 尺寸非法")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"不支持的 PNG 颜色类型：{color_type}")
    raw_all = zlib.decompress(bytes(idat))

    def expand(raw: bytes, w: int, h: int) -> bytearray:
        """把某一趟（pass）的像素展开成 3 字节 RGB。"""
        bits_pp = channels * bit_depth
        stride = (w * bits_pp + 7) // 8
        bpp = max(1, bits_pp // 8)
        lines = _png_unfilter(raw, w, h, bpp, stride)
        out = bytearray(w * h * 3)

        def sample(x: int) -> Tuple[int, int, int, int]:
            """取第 x 个像素 → (r,g,b,a)。"""
            if bit_depth == 8:
                i = x * channels
                if color_type == 0:
                    v = lines[i]
                    return v, v, v, 255
                if color_type == 2:
                    return lines[i], lines[i + 1], lines[i + 2], 255
                if color_type == 3:
                    idx = lines[i]
                    r, g, b = palette[idx * 3 : idx * 3 + 3]
                    a = trns[idx] if idx < len(trns) else 255
                    return r, g, b, a
                if color_type == 4:
                    v, a = lines[i], lines[i + 1]
                    return v, v, v, a
                return lines[i], lines[i + 1], lines[i + 2], lines[i + 3]
            if bit_depth == 16:
                i = x * channels * 2
                if color_type in (0, 4):
                    v = lines[i]
                    a = lines[i + 2] if color_type == 4 else 255
                    return v, v, v, a
                if color_type == 2:
                    return lines[i], lines[i + 2], lines[i + 4], 255
                return lines[i], lines[i + 2], lines[i + 4], lines[i + 6]
            # 1/2/4 位：仅灰度或调色板
            per_byte = 8 // bit_depth
            byte_i = x // per_byte
            shift = 8 - bit_depth * (x % per_byte + 1)
            val = (lines[byte_i] >> shift) & ((1 << bit_depth) - 1)
            if color_type == 3:
                r, g, b = palette[val * 3 : val * 3 + 3]
                a = trns[val] if val < len(trns) else 255
                return r, g, b, a
            v = val * (255 // ((1 << bit_depth) - 1))
            return v, v, v, 255

        for y in range(h):
            for x in range(w):
                r, g, b, a = sample(y * w + x)
                o = (y * w + x) * 3
                if a >= 250:
                    out[o], out[o + 1], out[o + 2] = r, g, b
                else:  # 半透明区域合成到白纸
                    na = a / 255.0
                    out[o] = int(r * na + 255 * (1 - na))
                    out[o + 1] = int(g * na + 255 * (1 - na))
                    out[o + 2] = int(b * na + 255 * (1 - na))
        return out

    if interlace == 0:
        return width, height, expand(raw_all, width, height)

    # Adam7 隔行：按 7 趟分别展开后拼回
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    canvas = bytearray(width * height * 3)
    offset = 0
    for x0, y0, dx, dy in passes:
        pw = (width - x0 + dx - 1) // dx
        ph = (height - y0 + dy - 1) // dy
        if pw <= 0 or ph <= 0:
            continue
        bits_pp = channels * bit_depth
        stride = (pw * bits_pp + 7) // 8
        need = (stride + 1) * ph
        sub = expand(raw_all[offset : offset + need], pw, ph)
        offset += need
        for y in range(ph):
            for x in range(pw):
                sx, sy = x0 + x * dx, y0 + y * dy
                si = (y * pw + x) * 3
                di = (sy * width + sx) * 3
                canvas[di : di + 3] = sub[si : si + 3]
    return width, height, canvas


# --------------------------------------------------------------------------- #
# 纯 Python 解码器：BMP
# --------------------------------------------------------------------------- #
def _decode_bmp(data: bytes) -> Tuple[int, int, bytearray]:
    """解码 BMP（24/32 位 BI_RGB、8 位调色板、以及常见的 16 位）。"""
    if data[:2] != b"BM":
        raise ValueError("不是 BMP 文件")
    (data_off,) = struct.unpack("<I", data[10:14])
    (hdr_size,) = struct.unpack("<I", data[14:18])
    if hdr_size < 40:
        raise ValueError("不支持的 BMP 头（OS/2 格式）")
    width, height, planes, bpp, comp = struct.unpack("<iiHHI", data[18:34])
    flip = height > 0
    height = abs(height)
    if comp not in (0, 3):
        raise ValueError(f"不支持的 BMP 压缩方式：{comp}")
    palette = []
    if bpp <= 8:
        count = 1 << bpp
        pbase = 14 + hdr_size
        for i in range(count):
            b, g, r, _ = data[pbase + i * 4 : pbase + i * 4 + 4]
            palette.append((r, g, b))
    stride = ((width * bpp + 31) // 32) * 4
    out = bytearray(width * height * 3)
    for y in range(height):
        src_y = (height - 1 - y) if flip else y
        row = data[data_off + src_y * stride : data_off + src_y * stride + stride]
        o = y * width * 3
        if bpp == 24:
            out[o : o + width * 3 : 3] = row[2::3]
            out[o + 1 : o + width * 3 : 3] = row[1::3]
            out[o + 2 : o + width * 3 : 3] = row[0::3]
        elif bpp == 32:
            out[o : o + width * 3 : 3] = row[2::4]
            out[o + 1 : o + width * 3 : 3] = row[1::4]
            out[o + 2 : o + width * 3 : 3] = row[0::4]
        elif bpp == 8:
            for x in range(width):
                r, g, b = palette[row[x]]
                out[o + x * 3] = r
                out[o + x * 3 + 1] = g
                out[o + x * 3 + 2] = b
        elif bpp == 16:
            for x in range(width):
                v = row[x * 2] | (row[x * 2 + 1] << 8)
                out[o + x * 3] = ((v >> 10) & 0x1F) * 255 // 31
                out[o + x * 3 + 1] = ((v >> 5) & 0x1F) * 255 // 31
                out[o + x * 3 + 2] = (v & 0x1F) * 255 // 31
        else:
            raise ValueError(f"不支持的 BMP 位深：{bpp}")
    return width, height, out


# --------------------------------------------------------------------------- #
# 纯 Python 编码器：PNG / BMP
# --------------------------------------------------------------------------- #
def encode_png(width: int, height: int, rgb: bytes, compress: int = 6) -> bytes:
    """把 RGB 缓冲编码为 PNG 字节串（filter 类型：逐行选 Sub/None 里更小的）。"""
    raw = bytearray()
    stride = width * 3
    prev = bytes(stride)
    for y in range(height):
        line = bytes(rgb[y * stride : (y + 1) * stride])
        # Sub 滤波对照片/文字扫描件通常更省空间，这里两种都算一下取小的
        sub = bytearray(stride)
        sub[0:3] = line[0:3]
        for i in range(3, stride):
            sub[i] = (line[i] - line[i - 3]) & 0xFF
        if sum(sub) < sum(line):
            raw.append(1)
            raw += sub
        else:
            raw.append(0)
            raw += line
        prev = line

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_SIG
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), compress))
        + chunk(b"IEND", b"")
    )


def encode_bmp(width: int, height: int, rgb: bytes) -> bytes:
    """把 RGB 缓冲编码为 24 位 BMP 字节串。"""
    stride = ((width * 3 + 3) // 4) * 4
    pad = stride - width * 3
    rows = []
    for y in range(height - 1, -1, -1):  # BMP 自底向上
        o = y * width * 3
        row = bytearray(stride)
        row[0 : width * 3 : 3] = rgb[o + 2 : o + width * 3 : 3]
        row[1 : width * 3 : 3] = rgb[o + 1 : o + width * 3 : 3]
        row[2 : width * 3 : 3] = rgb[o : o + width * 3 : 3]
        rows.append(bytes(row))
    body = b"".join(rows)
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(body), 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    return header + info + body


# --------------------------------------------------------------------------- #
# Image 类
# --------------------------------------------------------------------------- #
class Image:
    """极简 RGB 位图：``data`` 为 3 字节/像素的行优先缓冲。"""

    __slots__ = ("w", "h", "data")

    def __init__(self, w: int, h: int, data: bytes | bytearray):
        if w <= 0 or h <= 0:
            raise ValueError("图片尺寸必须为正")
        self.w = int(w)
        self.h = int(h)
        self.data = bytearray(data)
        if len(self.data) != self.w * self.h * 3:
            raise ValueError("像素缓冲长度与尺寸不匹配")

    # -- 基础 -------------------------------------------------------------- #
    def copy(self) -> "Image":
        return Image(self.w, self.h, bytearray(self.data))

    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        o = (y * self.w + x) * 3
        return self.data[o], self.data[o + 1], self.data[o + 2]

    def __repr__(self) -> str:
        return f"<Image {self.w}x{self.h}>"

    @property
    def size(self) -> Tuple[int, int]:
        return self.w, self.h

    # -- 几何变换 ---------------------------------------------------------- #
    def resize(self, nw: int, nh: int) -> "Image":
        """等比/非等比缩放（GDI+ 高质量双三次；无 GDI+ 时用双线性）。"""
        nw, nh = max(1, int(nw)), max(1, int(nh))
        if (nw, nh) == (self.w, self.h):
            return self.copy()
        from . import gdiplus

        if gdiplus.available():
            # 注意：GDI+ 侧通过「目标矩形尺寸」实现缩放，画布尺寸必须与缩放结果一致，
            # 否则就变成居中裁剪了（早期版本踩过这个坑）。
            w, h, px = gdiplus.transform_rgb(
                self.w, self.h, bytes(self.data), nw, nh, draw_size=(nw, nh)
            )
            return Image(w, h, px)
        return self._bilinear(nw, nh)

    def scale_to_fit(self, max_side: int) -> "Image":
        """按最长边限制缩放（只缩小，不放大）。"""
        max_side = int(max_side)
        if max_side <= 0 or max(self.w, self.h) <= max_side:
            return self.copy()
        k = max_side / float(max(self.w, self.h))
        return self.resize(max(1, int(self.w * k)), max(1, int(self.h * k)))

    def _bilinear(self, nw: int, nh: int) -> "Image":
        """纯 Python 双线性缩放（非 Windows 回退路径）。"""
        out = bytearray(nw * nh * 3)
        sx = self.w / nw
        sy = self.h / nh
        for y in range(nh):
            fy = (y + 0.5) * sy - 0.5
            y0 = max(0, min(self.h - 1, int(math.floor(fy))))
            y1 = min(self.h - 1, y0 + 1)
            wy = max(0.0, min(1.0, fy - y0))
            o0, o1 = y0 * self.w * 3, y1 * self.w * 3
            drow = y * nw * 3
            for x in range(nw):
                fx = (x + 0.5) * sx - 0.5
                x0 = max(0, min(self.w - 1, int(math.floor(fx))))
                x1 = min(self.w - 1, x0 + 1)
                wx = max(0.0, min(1.0, fx - x0))
                i00 = o0 + x0 * 3
                i10 = o0 + x1 * 3
                i01 = o1 + x0 * 3
                i11 = o1 + x1 * 3
                d = drow + x * 3
                for c in range(3):
                    top = self.data[i00 + c] * (1 - wx) + self.data[i10 + c] * wx
                    bot = self.data[i01 + c] * (1 - wx) + self.data[i11 + c] * wx
                    out[d + c] = int(top * (1 - wy) + bot * wy + 0.5)
        return Image(nw, nh, out)

    def crop(self, box: Sequence[int]) -> "Image":
        """裁剪，box=(left, top, right, bottom)。"""
        l, t, r, b = (int(v) for v in box)
        l = max(0, min(l, self.w - 1))
        t = max(0, min(t, self.h - 1))
        r = max(l + 1, min(r, self.w))
        b = max(t + 1, min(b, self.h))
        cw, ch = r - l, b - t
        out = bytearray(cw * ch * 3)
        for y in range(ch):
            src = ((t + y) * self.w + l) * 3
            out[y * cw * 3 : (y + 1) * cw * 3] = self.data[src : src + cw * 3]
        return Image(cw, ch, out)

    def rotate(self, angle: float, matte=(255, 255, 255), expand: bool = True) -> "Image":
        """绕中心旋转（度，逆时针），``expand=True`` 时扩大画布避免切角。"""
        angle = float(angle)
        if abs(angle) < 1e-6:
            return self.copy()
        if expand:
            rad = math.radians(angle)
            cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
            nw = int(self.w * cos_a + self.h * sin_a) + 1
            nh = int(self.w * sin_a + self.h * cos_a) + 1
        else:
            nw, nh = self.w, self.h
        from . import gdiplus

        if gdiplus.available():
            w, h, px = gdiplus.transform_rgb(
                self.w, self.h, bytes(self.data), nw, nh, angle=angle, matte=matte
            )
            return Image(w, h, px)
        return self._rotate_pure(angle, nw, nh, matte)

    def _rotate_pure(self, angle: float, nw: int, nh: int, matte) -> "Image":
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        cx, cy = self.w / 2.0, self.h / 2.0
        ncx, ncy = nw / 2.0, nh / 2.0
        out = bytearray(bytes(matte) * (nw * nh))
        for y in range(nh):
            dy = y - ncy
            for x in range(nw):
                dx = x - ncx
                # 逆变换到源坐标
                sx = cos_a * dx + sin_a * dy + cx
                sy = -sin_a * dx + cos_a * dy + cy
                ix, iy = int(sx + 0.5), int(sy + 0.5)
                if 0 <= ix < self.w and 0 <= iy < self.h:
                    si = (iy * self.w + ix) * 3
                    di = (y * nw + x) * 3
                    out[di : di + 3] = self.data[si : si + 3]
        return Image(nw, nh, out)

    def rotate90(self, times: int) -> "Image":
        """快捷 90° 旋转（times 可为负）。"""
        times = int(times) % 4
        if times == 0:
            return self.copy()
        img = self
        for _ in range(times):
            w, h = img.w, img.h
            out = bytearray(w * h * 3)
            src, sd = img.data, img.w
            for y in range(h):
                for x in range(w):
                    # 顺时针 90°：(x,y) -> (h-1-y, x)
                    si = (y * sd + x) * 3
                    di = (x * h + (h - 1 - y)) * 3
                    out[di : di + 3] = src[si : si + 3]
            img = Image(h, w, out)
        return img

    def pad(self, top: int = 0, bottom: int = 0, left: int = 0, right: int = 0,
            color=(255, 255, 255)) -> "Image":
        """四周补边（用纯色填充）。

        用途：把公式图垫高一点，好让它在文本控件里与相邻文字的基线对齐
        （Tk 的内嵌图片没有 -offset，只能靠留白微调）。
        """
        top, bottom = max(0, int(top)), max(0, int(bottom))
        left, right = max(0, int(left)), max(0, int(right))
        if not (top or bottom or left or right):
            return self.copy()
        nw, nh = self.w + left + right, self.h + top + bottom
        out = Image(nw, nh, bytearray(bytes(color) * (nw * nh)))
        for y in range(self.h):
            src = y * self.w * 3
            dst = ((y + top) * nw + left) * 3
            out.data[dst : dst + self.w * 3] = self.data[src : src + self.w * 3]
        return out

    def flip(self, horizontal: bool = True) -> "Image":
        out = bytearray(self.w * self.h * 3)
        for y in range(self.h):
            row = self.data[y * self.w * 3 : (y + 1) * self.w * 3]
            if horizontal:
                for x in range(self.w):
                    out[y * self.w * 3 + x * 3 : y * self.w * 3 + x * 3 + 3] = row[
                        (self.w - 1 - x) * 3 : (self.w - 1 - x) * 3 + 3
                    ]
            else:
                out[y * self.w * 3 : (y + 1) * self.w * 3] = row
        if not horizontal:
            flipped = bytearray(len(out))
            for y in range(self.h):
                src = (self.h - 1 - y) * self.w * 3
                flipped[y * self.w * 3 : (y + 1) * self.w * 3] = out[src : src + self.w * 3]
            out = flipped
        return Image(self.w, self.h, out)

    # -- 色彩增强（LUT 实现，速度接近 C） ---------------------------------- #
    def _apply_lut(self, lut: bytes) -> "Image":
        """对三通道应用同一张 LUT（256 字节）。"""
        return Image(self.w, self.h, bytes(self.data).translate(lut))

    @staticmethod
    def build_lut(brightness: float = 0.0, contrast: float = 1.0, gamma: float = 1.0) -> bytes:
        """生成 LUT：``brightness`` 为 -100..100 的加减，``contrast`` 1.0 为原样，``gamma`` 越小越亮。"""
        lut = bytearray(256)
        g = max(0.05, float(gamma)) if gamma else 1.0
        for i in range(256):
            v = i / 255.0
            v = v ** (1.0 / g)
            v = (v - 0.5) * float(contrast) + 0.5
            v = v * 255.0 + float(brightness) * 2.55
            lut[i] = 0 if v < 0 else (255 if v > 255 else int(v + 0.5))
        return bytes(lut)

    def adjust(self, brightness: float = 0.0, contrast: float = 1.0, gamma: float = 1.0) -> "Image":
        return self._apply_lut(self.build_lut(brightness, contrast, gamma))

    def grayscale(self) -> "Image":
        """转灰度（仍存为 3 通道，便于统一处理）。"""
        data = bytearray(self.w * self.h * 3)
        src = self.data
        for i in range(0, len(src), 3):
            v = (src[i] * 299 + src[i + 1] * 587 + src[i + 2] * 114) // 1000
            data[i] = data[i + 1] = data[i + 2] = v
        return Image(self.w, self.h, data)

    def blur(self, radius: int = 1) -> "Image":
        """盒式模糊（两次一维滑动平均，O(n)）。用于去噪、去水印。"""
        radius = max(1, int(radius))
        win = radius * 2 + 1
        w, h = self.w, self.h
        tmp = bytearray(len(self.data))
        src = self.data
        # 横向
        for y in range(h):
            base = y * w * 3
            for c in range(3):
                acc = 0
                for x in range(-radius, radius + 1):
                    acc += src[base + max(0, min(w - 1, x)) * 3 + c]
                for x in range(w):
                    tmp[base + x * 3 + c] = acc // win
                    add_x = min(w - 1, x + radius + 1)
                    sub_x = max(0, x - radius)
                    acc += src[base + add_x * 3 + c] - src[base + sub_x * 3 + c]
        # 纵向
        out = bytearray(len(src))
        for x in range(w):
            for c in range(3):
                acc = 0
                for y in range(-radius, radius + 1):
                    acc += tmp[max(0, min(h - 1, y)) * w * 3 + x * 3 + c]
                for y in range(h):
                    out[y * w * 3 + x * 3 + c] = acc // win
                    add_y = min(h - 1, y + radius + 1)
                    sub_y = max(0, y - radius)
                    acc += tmp[add_y * w * 3 + x * 3 + c] - tmp[sub_y * w * 3 + x * 3 + c]
        return Image(w, h, out)

    def sharpen(self, amount: float = 0.6) -> "Image":
        """非锐化掩模：原图 + amount*(原图-模糊图)。"""
        blurred = self.blur(1)
        out = bytearray(len(self.data))
        a = float(amount)
        for i in range(len(out)):
            v = self.data[i] + a * (self.data[i] - blurred.data[i])
            out[i] = 0 if v < 0 else (255 if v > 255 else int(v))
        return Image(self.w, self.h, out)

    # -- 自动增强 ---------------------------------------------------------- #
    def histogram(self) -> Tuple[List[int], List[int], List[int]]:
        """三通道直方图（步长采样以提速）。"""
        step = max(3, (len(self.data) // 3) // 200000 * 3)
        hs = [[0] * 256 for _ in range(3)]
        d = self.data
        for i in range(0, len(d), step):
            for c in range(3):
                if i + c < len(d):
                    hs[c][d[i + c]] += 1
        return hs[0], hs[1], hs[2]

    def mean_brightness(self) -> float:
        """平均亮度 0-255。"""
        step = max(3, (len(self.data) // 3) // 100000 * 3)
        total = n = 0
        d = self.data
        for i in range(0, len(d), step):
            total += (d[i] * 299 + d[i + 1] * 587 + d[i + 2] * 114) // 1000
            n += 1
        return total / max(1, n)

    def autocontrast(self, low_pct: float = 0.5, high_pct: float = 99.5) -> "Image":
        """自动对比度（按直方图分位点拉伸），并把偏暗/偏黄的纸面拉白。"""
        gray, _, _ = self.histogram()
        total = sum(gray) or 1
        lo = hi = 0
        acc = 0
        for i in range(256):
            acc += gray[i]
            if acc >= total * low_pct / 100.0:
                lo = i
                break
        acc = 0
        for i in range(256):
            acc += gray[i]
            if acc >= total * high_pct / 100.0:
                hi = i
                break
        if hi <= lo:
            return self.copy()
        scale = 255.0 / (hi - lo)
        lut = bytes(0 if (i - lo) * scale < 0 else (255 if (i - lo) * scale > 255 else int((i - lo) * scale + 0.5)) for i in range(256))
        return self._apply_lut(lut)

    def auto_brighten(self, target: float = 232.0) -> Tuple["Image", float]:
        """把纸面亮度拉到接近白纸。返回 (新图, 使用的 gamma)。"""
        mean = self.mean_brightness()
        if mean >= target or mean <= 1:
            return self.copy(), 1.0
        gamma = math.log(max(0.02, target / 255.0)) / math.log(max(0.02, mean / 255.0))
        gamma = max(0.35, min(2.5, gamma))
        return self.adjust(gamma=1.0 / gamma if gamma else 1.0), gamma

    # -- 质量分析 ---------------------------------------------------------- #
    def _gray_small(self, max_side: int = 512) -> Tuple[int, int, bytearray]:
        """缩到小尺寸并转灰度，供质量分析用（速度快）。"""
        img = self.scale_to_fit(max_side)
        gray = bytearray(img.w * img.h)
        d = img.data
        for i in range(0, len(d), 3):
            gray[i // 3] = (d[i] * 299 + d[i + 1] * 587 + d[i + 2] * 114) // 1000
        return img.w, img.h, gray

    def sharpness(self) -> float:
        """清晰度：拉普拉斯响应的方差，越大越清晰（模糊照片通常 < 60）。"""
        w, h, g = self._gray_small(512)
        if w < 3 or h < 3:
            return 0.0
        vals = []
        for y in range(1, h - 1):
            base = y * w
            for x in range(1, w - 1):
                lap = (
                    4 * g[base + x]
                    - g[base + x - 1]
                    - g[base + x + 1]
                    - g[base + x - w]
                    - g[base + x + w]
                )
                vals.append(lap)
        n = len(vals) or 1
        mean = sum(vals) / n
        return sum((v - mean) ** 2 for v in vals) / n

    def contrast_score(self) -> float:
        """对比度（灰度标准差，0-128 归一化）。"""
        w, h, g = self._gray_small(256)
        n = len(g) or 1
        mean = sum(g) / n
        var = sum((v - mean) ** 2 for v in g) / n
        return min(1.0, math.sqrt(var) / 64.0)

    def ink_ratio(self) -> float:
        """墨迹（深色像素）占比，用来判断「这张图里有没有字」。"""
        w, h, g = self._gray_small(256)
        dark = sum(1 for v in g if v < 160)
        return dark / max(1, len(g))

    def detect_skew(self, max_angle: float = 8.0, step: float = 0.5) -> float:
        """估计文本倾斜角（度，正=顺时针）。

        做法（投影剖面法）：把图缩到小尺寸并二值化，只取出「墨迹像素」的坐标；
        对每个候选角度做水平错切，统计每行的墨迹数量，行剖面**方差最大**的角度
        就是文字行的真实方向。先在 ±max_angle 内粗搜（step），再在最优值附近细化。

        两个关键细节（都是实拍图踩出来的）：
        - **先裁掉深色边框**：手机拍桌面时纸张外面是暗背景，若整图统计，"墨迹"里会混进
          大片桌面，投影剖面就被带偏（实测 7° 倾斜会误判成 0°）。所以先按行/列平均亮度
          把非纸面区域切掉，只在纸面内分析。
        - **阈值随纸面亮度自适应**：白纸用 0.72×纸面均值作阈值，既能抓住淡铅笔字，
          又不会把纸面阴影当墨迹。

        只遍历墨迹像素（约 3%-10%），所以几十个角度也只要零点几秒。
        """
        w, h, g = self._gray_small(600)
        if w < 20 or h < 20:
            return 0.0

        # 1) 裁掉深色边框，只留纸面
        row_mean = [sum(g[y * w : (y + 1) * w]) / w for y in range(h)]
        col_mean = [sum(g[y * w + x] for y in range(h)) / h for x in range(w)]
        overall = sum(row_mean) / h
        limit = max(100.0, overall * 0.82)
        top, bottom, left, right = 0, h - 1, 0, w - 1
        while top < h // 3 and row_mean[top] < limit:
            top += 1
        while bottom > h * 2 // 3 and row_mean[bottom] < limit:
            bottom -= 1
        while left < w // 3 and col_mean[left] < limit:
            left += 1
        while right > w * 2 // 3 and col_mean[right] < limit:
            right -= 1
        if right - left >= 40 and bottom - top >= 40 and (top or left or bottom < h - 1 or right < w - 1):
            tw, th = right - left + 1, bottom - top + 1
            trimmed = bytearray(tw * th)
            for y in range(th):
                src = (top + y) * w + left
                trimmed[y * tw : (y + 1) * tw] = g[src : src + tw]
            w, h, g = tw, th, trimmed

        # 2) 自适应二值化：纸面均值越低（阴影/泛黄），阈值同步下压
        paper = sum(g) / max(1, len(g))
        thresh = max(105.0, min(205.0, paper * 0.72))
        pts = []
        for y in range(h):
            base = y * w
            for x in range(w):
                if g[base + x] < thresh:
                    pts.append((x, y))
        ink = len(pts)
        if ink < 30 or ink > 0.6 * w * h:  # 几乎没字 / 整张都是黑（照片太暗）
            return 0.0
        if ink > 30000:  # 墨迹太多时抽样，控制耗时
            stride = ink // 30000 + 1
            pts = pts[::stride]
        cx = w / 2.0

        def score(angle: float) -> float:
            t = math.tan(math.radians(angle))
            rows = [0] * h
            for x, y in pts:
                yy = int(y - (x - cx) * t + 0.5)
                if 0 <= yy < h:
                    rows[yy] += 1
            n = len(rows)
            mean = sum(rows) / n
            return sum((r - mean) ** 2 for r in rows) / n

        best_angle, best_score = 0.0, -1.0
        a = -max_angle
        while a <= max_angle + 1e-9:
            s = score(a)
            if s > best_score:
                best_score, best_angle = s, a
            a += step
        # 细化：在粗搜最优点附近 ±step 内按 0.1° 再找一次
        a = best_angle - step
        while a <= best_angle + step + 1e-9:
            s = score(a)
            if s > best_score:
                best_score, best_angle = s, a
            a += 0.1
        best_angle = round(best_angle, 2)
        return 0.0 if abs(best_angle) < 0.3 else best_angle

    def quality_score(self) -> dict:
        """综合质量评分（0-1）与各项指标，用于决定是否需要云端兜底。"""
        sharp = self.sharpness()
        bright = self.mean_brightness()
        contrast = self.contrast_score()
        ink = self.ink_ratio()
        # 清晰度：60 分以上算清晰
        s_sharp = max(0.0, min(1.0, sharp / 120.0))
        # 亮度：170-240 最理想（白纸黑字）
        if bright < 90:
            s_bright = max(0.0, (bright - 40) / 50.0) * 0.6
        elif bright > 248:
            s_bright = 0.75
        else:
            s_bright = 1.0 - abs(bright - 205) / 260.0
            s_bright = max(0.3, min(1.0, s_bright + 0.35))
        s_ink = 1.0 if 0.005 < ink < 0.35 else (0.45 if ink >= 0.35 else 0.25)
        score = 0.5 * s_sharp + 0.25 * s_bright + 0.15 * contrast + 0.10 * s_ink
        return {
            "score": round(max(0.0, min(1.0, score)), 3),
            "sharpness": round(sharp, 1),
            "brightness": round(bright, 1),
            "contrast": round(contrast, 3),
            "ink": round(ink, 4),
            "need_cloud": score < 0.45,
        }

    # -- 输出 -------------------------------------------------------------- #
    def _encode(self, kind: str, quality: int = 88, pil_format: str = "") -> bytes:
        from . import gdiplus

        kind = kind.lower()
        if HAS_PIL:
            buf = io.BytesIO()
            _PILImage.frombytes("RGB", (self.w, self.h), bytes(self.data)).save(
                buf, format=pil_format or kind.upper(),
                quality=quality if kind in ("jpeg", "jpg") else None,
            )
            return buf.getvalue()
        if gdiplus.available():
            # 内存里直接编码，不落临时文件（受限环境下临时目录可能不可写）
            return gdiplus.save_image_bytes(self.w, self.h, bytes(self.data), kind, quality)
        if kind == "png":
            return encode_png(self.w, self.h, bytes(self.data))
        if kind == "bmp":
            return encode_bmp(self.w, self.h, bytes(self.data))
        # 没有 JPEG 编码器时退回 PNG（模型同样能读）
        return encode_png(self.w, self.h, bytes(self.data))

    def to_jpeg_bytes(self, quality: int = 88) -> bytes:
        return self._encode("jpeg", quality)

    def to_png_bytes(self) -> bytes:
        return self._encode("png")

    def to_bmp_bytes(self) -> bytes:
        return self._encode("bmp")

    def to_base64(self, quality: int = 88, kind: str = "jpeg") -> str:
        raw = self.to_jpeg_bytes(quality) if kind in ("jpeg", "jpg") else self.to_png_bytes()
        return base64.b64encode(raw).decode("ascii")

    def to_ppm(self) -> bytes:
        """P6 PPM 数据，供 tkinter PhotoImage(data=...) 显示（无需任何解码库）。"""
        return b"P6\n%d %d\n255\n" % (self.w, self.h) + bytes(self.data)

    def save(self, path, quality: int = 92) -> str:
        """按扩展名保存（jpg/jpeg/png/bmp）。"""
        path = str(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            raw = self.to_jpeg_bytes(quality)
        elif ext == ".png":
            raw = self.to_png_bytes()
        elif ext == ".bmp":
            raw = self.to_bmp_bytes()
        else:
            raise ValueError(f"不支持的保存格式：{ext}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
        return path


# --------------------------------------------------------------------------- #
# 读写入口（多后端自动降级）
# --------------------------------------------------------------------------- #
def _load_with_pil(path) -> Tuple[int, int, bytearray]:
    with _PILImage.open(path) as im:
        im = im.convert("RGB")
        return im.width, im.height, bytearray(im.tobytes())


def _load_with_sips(path) -> Tuple[int, int, bytearray]:
    """macOS：系统自带 sips 把任意格式转成 BMP，再用纯 Python 解码。"""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as tf:
        tmp = tf.name
    try:
        subprocess.run(
            ["sips", "-s", "format", "bmp", str(path), "--out", tmp],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        with open(tmp, "rb") as f:
            return _decode_bmp(f.read())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load(path) -> Image:
    """读取图片为 :class:`Image`（自动选择可用后端）。"""
    path = str(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"图片不存在：{path}")
    errors = []
    if HAS_PIL:
        try:
            w, h, d = _load_with_pil(path)
            return Image(w, h, d)
        except Exception as exc:
            errors.append(f"Pillow: {exc}")
    from . import gdiplus

    if gdiplus.available():
        try:
            w, h, d = gdiplus.load_rgb(path)
            return Image(w, h, d)
        except Exception as exc:
            errors.append(f"GDI+: {exc}")
    # 纯 Python：PNG / BMP 自解码
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        with open(path, "rb") as f:
            blob = f.read()
        if head.startswith(_PNG_SIG):
            w, h, d = _decode_png(blob)
        elif head[:2] == b"BM":
            w, h, d = _decode_bmp(blob)
        else:
            raise ValueError("纯 Python 解码器只支持 PNG/BMP")
        return Image(w, h, d)
    except Exception as exc:
        errors.append(f"纯Python: {exc}")
    if sys.platform == "darwin":
        try:
            w, h, d = _load_with_sips(path)
            return Image(w, h, d)
        except Exception as exc:
            errors.append(f"sips: {exc}")
    raise ValueError("无法解码图片：" + "；".join(errors) + "。建议安装 Pillow（pip install pillow）")


def probe(path) -> Optional[dict]:
    """快速探测图片信息（不整图解码，失败返回 None）。"""
    from . import gdiplus

    size = gdiplus.probe_size(path) if gdiplus.available() else None
    if size is None and HAS_PIL:
        try:
            with _PILImage.open(path) as im:
                size = (im.width, im.height)
        except Exception:
            size = None
    if size is None:
        try:
            with open(path, "rb") as f:
                head = f.read(32)
            if head.startswith(_PNG_SIG):
                w, h = struct.unpack(">II", head[16:24])
                size = (w, h)
            elif head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                size = (abs(w), abs(h))
        except Exception:
            size = None
    if size is None:
        return None
    return {"width": size[0], "height": size[1], "backend": backend_name()}


def backend_name() -> str:
    """当前使用的解码后端名称（自检界面显示用）。"""
    if HAS_PIL:
        return "Pillow"
    from . import gdiplus

    if gdiplus.available():
        return "Windows GDI+"
    if sys.platform == "darwin":
        return "macOS sips + 纯Python"
    return "纯Python（PNG/BMP）"


# --------------------------------------------------------------------------- #
# 预处理流水线
# --------------------------------------------------------------------------- #
def remove_watermark(img: Image, box: Sequence[int], mode: str = "inpaint") -> Image:
    """去除指定矩形区域的水印/印章。

    - ``white``  ：用纸面颜色直接覆盖（适合水印压在空白处）
    - ``median`` ：局部平滑，弱化半透明水印（适合水印压在文字上）
    - ``inpaint``：用区域四边颜色做双向线性插值填充（适合压住少量文字的印章）
    """
    l, t, r, b = (int(v) for v in box)
    l, t = max(0, l), max(0, t)
    r, b = min(img.w, r), min(img.h, b)
    if r - l < 2 or b - t < 2:
        return img.copy()
    out = img.copy()

    if mode == "white":
        # 用区域外一圈的亮色（纸面）作为填充色
        samples = []
        for x in range(l, r, max(1, (r - l) // 12)):
            if t - 2 >= 0:
                samples.append(img.pixel(x, t - 2))
            if b + 1 < img.h:
                samples.append(img.pixel(x, b + 1))
        samples.sort(key=lambda p: -(p[0] + p[1] + p[2]))
        paper = samples[len(samples) // 8] if samples else (255, 255, 255)
        fill = bytes(paper) * ((r - l) * (b - t))
        for y in range(t, b):
            o = (y * img.w + l) * 3
            out.data[o : o + (r - l) * 3] = fill
        return out

    if mode == "median":
        patch = img.crop((l, t, r, b)).blur(2)
        for y in range(t, b):
            o = (y * img.w + l) * 3
            po = ((y - t) * (r - l)) * 3
            out.data[o : o + (r - l) * 3] = patch.data[po : po + (r - l) * 3]
        return out

    # inpaint：横向插值 + 纵向插值取平均（简单但对印章/半透明水印效果够用）
    for y in range(t, b):
        wy = (y - t) / max(1, (b - t - 1))
        ly = max(0, min(img.h - 1, y))
        left = img.pixel(max(0, l - 1), ly)
        right = img.pixel(min(img.w - 1, r), ly)
        for x in range(l, r):
            wx = (x - l) / max(1, (r - l - 1))
            lx = max(0, min(img.w - 1, x))
            top = img.pixel(lx, max(0, t - 1))
            bot = img.pixel(lx, min(img.h - 1, b))
            o = (y * img.w + x) * 3
            for c in range(3):
                horiz = left[c] * (1 - wx) + right[c] * wx
                vert = top[c] * (1 - wy) + bot[c] * wy
                val = 0.5 * horiz + 0.5 * vert
                out.data[o + c] = 0 if val < 0 else (255 if val > 255 else int(val))
    return out


def auto_preprocess(img: Image, opts: Optional[dict] = None) -> Tuple[Image, List[str]]:
    """自动预处理：去水印（可选框）→ 纠偏 → 提亮/拉对比 → 适度锐化。

    返回 (处理后的图, 处理说明列表)。说明会写进归档文字里，方便回溯。
    """
    opts = opts or {}
    steps: List[str] = []
    out = img.copy()

    # 1) 手动指定/自动检测的水印框
    for box in opts.get("watermark_boxes", []) or []:
        out = remove_watermark(out, box, opts.get("watermark_mode", "inpaint"))
        steps.append(f"去除水印区域 {tuple(int(v) for v in box)}")

    # 2) 纠偏
    if opts.get("deskew", True):
        angle = out.detect_skew()
        if abs(angle) >= 0.6:
            out = out.rotate(angle)
            steps.append(f"纠偏 {angle:+.1f}°")

    # 3) 提亮 + 拉对比
    if opts.get("brighten", True):
        out2, gamma = out.auto_brighten()
        if gamma != 1.0:
            steps.append(f"提亮（gamma {gamma:.2f}）")
        out = out2
        if out.mean_brightness() < 200 or out.contrast_score() < 0.35:
            out = out.autocontrast()
            steps.append("自动对比度拉伸")

    # 4) 锐化（只在偏模糊时做，避免把噪点放大）
    if opts.get("sharpen", True) and out.sharpness() < 90:
        out = out.sharpen(0.5)
        steps.append("轻度锐化")

    # 5) 限制尺寸（识别用）
    max_side = int(opts.get("max_side", 1600) or 1600)
    if max(out.w, out.h) > max_side:
        out = out.scale_to_fit(max_side)
        steps.append(f"缩放至 {out.w}×{out.h}")

    return out, steps


def make_preview_file(src, dst, max_side: int = 900) -> str:
    """生成归档/预览用小图（失败则返回原图路径）。"""
    try:
        img = load(src).scale_to_fit(max_side)
        img.save(dst, quality=85)
        return dst
    except Exception:
        return str(src)
