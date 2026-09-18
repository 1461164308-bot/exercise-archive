# -*- coding: utf-8 -*-
"""Windows GDI+ 图像后端（ctypes 直接调用系统库，无需任何第三方依赖）。

本软件要求「零第三方依赖、离线可用」，但手机照片基本都是 JPEG，纯 Python 解 JPEG
不现实。Windows 自带 ``gdiplus.dll`` 能解码/编码 JPEG / PNG / BMP / GIF / TIFF，
于是这里用 ctypes 把它包一层，提供三件事：

1. ``load_rgb(path)``   —— 任何系统支持的格式 → RGB 像素缓冲（``bytearray``，3 字节/像素）
2. ``save_image(...)``  —— RGB 缓冲 → JPEG（可控画质）/ PNG / BMP
3. ``draw_*``           —— 把文字/线条画进位图（导出 PDF、生成示例图时用）

非 Windows 或 gdiplus 不可用时，``available()`` 返回 False，上层会自动退回
纯 Python 的 PNG/BMP 解码器和 tkinter 显示路径。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

# GDI+ 常量
PixelFormat32bppARGB = 0x0026200A
ImageLockModeRead = 1
ImageLockModeWrite = 2
UnitPixel = 2
FontStyleRegular = 0
SmoothingModeAntiAlias = 4


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int32),
        ("SuppressExternalCodecs", ctypes.c_int32),
    ]


class BitmapData(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("stride", ctypes.c_int32),
        ("PixelFormat", ctypes.c_int32),
        ("Scan0", ctypes.c_void_p),
        ("Reserved", ctypes.c_void_p),
    ]


class ImageCodecInfo(ctypes.Structure):
    _fields_ = [
        ("Clsid", GUID),
        ("FormatID", GUID),
        ("CodecName", ctypes.c_wchar_p),
        ("DllName", ctypes.c_wchar_p),
        ("FormatDescription", ctypes.c_wchar_p),
        ("FilenameExtension", ctypes.c_wchar_p),
        ("MimeType", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("Version", ctypes.c_uint32),
        ("SigCount", ctypes.c_uint32),
        ("SigSize", ctypes.c_uint32),
        ("SigPattern", ctypes.c_void_p),
        ("SigMask", ctypes.c_void_p),
    ]


class EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", GUID),
        ("NumberOfValues", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("Value", ctypes.c_void_p),
    ]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", EncoderParameter * 1)]


# 编码器质量参数的 GUID：{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}
ENCODER_QUALITY = GUID(
    0x1D5BE4B5,
    0xFA4A,
    0x452D,
    (ctypes.c_ubyte * 8)(0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB),
)

_lock = threading.Lock()
_token = ctypes.c_void_p(0)
_gdiplus = None
_init_error: Optional[str] = None


def _load():
    """加载 gdiplus.dll 并完成 GdiplusStartup（进程内只做一次）。"""
    global _gdiplus, _token, _init_error
    if _gdiplus is not None or _init_error is not None:
        return _gdiplus
    with _lock:
        if _gdiplus is not None or _init_error is not None:
            return _gdiplus
        if not IS_WINDOWS:
            _init_error = "非 Windows 系统，不使用 GDI+"
            return None
        try:
            gp = ctypes.WinDLL("gdiplus")
            gp.GdiplusStartup.argtypes = [
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(GdiplusStartupInput),
                ctypes.c_void_p,
            ]
            gp.GdiplusStartup.restype = ctypes.c_int32
            gp.GdiplusShutdown.argtypes = [ctypes.c_void_p]

            gp.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
            gp.GdipCreateBitmapFromFile.restype = ctypes.c_int32
            gp.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            gp.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            gp.GdipBitmapLockBits.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.RECT),
                ctypes.c_uint32,
                ctypes.c_int32,
                ctypes.POINTER(BitmapData),
            ]
            gp.GdipBitmapUnlockBits.argtypes = [ctypes.c_void_p, ctypes.POINTER(BitmapData)]
            gp.GdipCreateBitmapFromScan0.argtypes = [
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.c_int32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            gp.GdipSaveImageToFile.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.POINTER(GUID),
                ctypes.c_void_p,
            ]
            gp.GdipGetImageEncodersSize.argtypes = [
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
            ]
            gp.GdipGetImageEncoders.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ImageCodecInfo),
            ]
            gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
            # 绘图相关（可选，失败不影响解码编码）
            for name, argtypes in (
                ("GdipCreateFromImage", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
                ("GdipCreateSolidFill", [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]),
                ("GdipFillRectangleI", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                                        ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]),
                ("GdipSetSmoothingMode", [ctypes.c_void_p, ctypes.c_int32]),
                ("GdipDrawLineI", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                   ctypes.c_int32, ctypes.c_int32]),
                ("GdipCreatePen1", [ctypes.c_uint32, ctypes.c_float, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_void_p)]),
            ):
                try:
                    fn = getattr(gp, name)
                    fn.argtypes = argtypes
                except AttributeError:
                    pass

            si = GdiplusStartupInput(1, None, 0, 0)
            tok = ctypes.c_void_p(0)
            st = gp.GdiplusStartup(ctypes.byref(tok), ctypes.byref(si), None)
            if st != 0:
                _init_error = f"GdiplusStartup 失败，状态码 {st}"
                return None
            _token = tok
            _gdiplus = gp
        except Exception as exc:  # pragma: no cover
            _init_error = f"加载 gdiplus 失败：{exc}"
            return None
    return _gdiplus


def available() -> bool:
    return _load() is not None


def init_error() -> str:
    _load()
    return _init_error or ""


def _encoder_clsid(mime: str) -> Optional[GUID]:
    """取得指定 MIME 类型编码器的 CLSID（如 image/jpeg）。"""
    gp = _load()
    if gp is None:
        return None
    num = ctypes.c_uint32(0)
    size = ctypes.c_uint32(0)
    if gp.GdipGetImageEncodersSize(ctypes.byref(num), ctypes.byref(size)) != 0 or size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    arr = ctypes.cast(buf, ctypes.POINTER(ImageCodecInfo))
    if gp.GdipGetImageEncoders(num.value, size.value, arr) != 0:
        return None
    for i in range(num.value):
        info = arr[i]
        if info.MimeType and info.MimeType.lower() == mime.lower():
            return info.Clsid
    return None


def load_rgb(path) -> Tuple[int, int, bytearray]:
    """解码任意系统支持的图片为 RGB 缓冲。返回 (宽, 高, bytearray)，失败抛异常。"""
    gp = _load()
    if gp is None:
        raise RuntimeError(init_error() or "GDI+ 不可用")
    path = os.path.abspath(str(path))
    bmp = ctypes.c_void_p(0)
    st = gp.GdipCreateBitmapFromFile(ctypes.c_wchar_p(path), ctypes.byref(bmp))
    if st != 0 or not bmp:
        raise ValueError(f"GDI+ 无法解码图片（状态码 {st}）：{path}")
    try:
        w = ctypes.c_uint32(0)
        h = ctypes.c_uint32(0)
        gp.GdipGetImageWidth(bmp, ctypes.byref(w))
        gp.GdipGetImageHeight(bmp, ctypes.byref(h))
        width, height = int(w.value), int(h.value)
        if width <= 0 or height <= 0:
            raise ValueError("图片尺寸非法")
        rect = wintypes.RECT(0, 0, width, height)
        data = BitmapData()
        st = gp.GdipBitmapLockBits(
            bmp, ctypes.byref(rect), ImageLockModeRead, PixelFormat32bppARGB, ctypes.byref(data)
        )
        if st != 0:
            raise ValueError(f"GDI+ LockBits 失败（状态码 {st}）")
        try:
            stride = int(data.stride)
            base = int(data.Scan0 or 0)
            out = bytearray(width * height * 3)
            row_bytes = width * 4
            flat = ctypes.string_at(
                base + (0 if stride >= 0 else (height - 1) * stride), abs(stride) * height
            )
            # 用扩展切片赋值做 BGRA→RGB，避免逐像素 Python 循环（2000 万像素级也能秒级完成）
            for y in range(height):
                if stride >= 0:
                    row = flat[y * stride : y * stride + row_bytes]
                    o = y * width * 3
                else:
                    row = flat[(height - 1 - y) * abs(stride) : (height - 1 - y) * abs(stride) + row_bytes]
                    o = y * width * 3
                out[o : o + width * 3 : 3] = row[2::4]
                out[o + 1 : o + width * 3 : 3] = row[1::4]
                out[o + 2 : o + width * 3 : 3] = row[0::4]
            return width, height, out
        finally:
            gp.GdipBitmapUnlockBits(bmp, ctypes.byref(data))
    finally:
        gp.GdipDisposeImage(bmp)


def save_image_bytes(width: int, height: int, rgb: bytes, kind: str = "jpeg", quality: int = 88) -> bytes:
    """把 RGB 缓冲编码成图片**字节流**（不落盘）。

    实现：GDI+ 支持把图像写进内存流（``IStream``）—— 先用 ``CreateStreamOnHGlobal``
    拿一个内存流，``GdipSaveImageToStream`` 写进去，再用 ``GetHGlobalFromStream``
    把字节拷出来。这样就不需要临时文件（临时目录被安全策略限制时尤其重要），
    导出 PDF/上传模型都走这条路。
    """
    gp = _load()
    if gp is None:
        raise RuntimeError(init_error() or "GDI+ 不可用")
    mime = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "bmp": "image/bmp"}.get(
        kind.lower()
    )
    if mime is None:
        raise ValueError(f"不支持的输出格式：{kind}")
    clsid = _encoder_clsid(mime)
    if clsid is None:
        raise RuntimeError(f"系统没有 {mime} 编码器")

    buf = _bgra_from_rgb(width, height, rgb)
    bmp = ctypes.c_void_p(0)
    if gp.GdipCreateBitmapFromScan0(
        width, height, width * 4, PixelFormat32bppARGB, buf, ctypes.byref(bmp)
    ) != 0:
        raise RuntimeError("GdipCreateBitmapFromScan0 失败")

    ole32 = ctypes.WinDLL("ole32")
    kernel32 = ctypes.WinDLL("kernel32")
    ole32.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    ole32.CreateStreamOnHGlobal.restype = ctypes.c_int32
    ole32.GetHGlobalFromStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole32.GetHGlobalFromStream.restype = ctypes.c_int32
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    if not hasattr(gp, "GdipSaveImageToStream"):
        gp.GdipSaveImageToStream = getattr(gp, "GdipSaveImageToStream", None)
    try:
        gp.GdipSaveImageToStream.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p
        ]
    except Exception:
        pass

    pstream = ctypes.c_void_p(0)
    try:
        if ole32.CreateStreamOnHGlobal(None, 1, ctypes.byref(pstream)) != 0 or not pstream:
            raise RuntimeError("CreateStreamOnHGlobal 失败")
        params = None
        if mime == "image/jpeg":
            q = ctypes.c_uint32(max(10, min(100, int(quality))))
            params = EncoderParameters()
            params.Count = 1
            params.Parameter[0].Guid = ENCODER_QUALITY
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4
            params.Parameter[0].Value = ctypes.cast(ctypes.byref(q), ctypes.c_void_p)
            st = gp.GdipSaveImageToStream(
                bmp, pstream, ctypes.byref(clsid), ctypes.byref(params)
            )
        else:
            st = gp.GdipSaveImageToStream(bmp, pstream, ctypes.byref(clsid), None)
        if st != 0:
            raise RuntimeError(f"GdipSaveImageToStream 失败（状态码 {st}）")
        hglobal = ctypes.c_void_p(0)
        if ole32.GetHGlobalFromStream(pstream, ctypes.byref(hglobal)) != 0 or not hglobal:
            raise RuntimeError("GetHGlobalFromStream 失败")
        size = int(kernel32.GlobalSize(hglobal))
        ptr = kernel32.GlobalLock(hglobal)
        if not ptr or size <= 0:
            raise RuntimeError("内存流为空")
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(hglobal)
    finally:
        # 释放 IStream（IUnknown 的第三个虚函数）
        if pstream:
            try:
                vtbl = ctypes.cast(pstream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                release = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)(vtbl[2])
                release(pstream)
            except Exception:
                pass
        gp.GdipDisposeImage(bmp)


def save_image(width: int, height: int, rgb: bytes, path, kind: str = "jpeg", quality: int = 88) -> None:
    """把 RGB 缓冲编码成图片文件。kind: jpeg / png / bmp。"""
    gp = _load()
    if gp is None:
        raise RuntimeError(init_error() or "GDI+ 不可用")
    mime = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "bmp": "image/bmp"}.get(
        kind.lower()
    )
    if mime is None:
        raise ValueError(f"不支持的输出格式：{kind}")
    clsid = _encoder_clsid(mime)
    if clsid is None:
        raise RuntimeError(f"系统没有 {mime} 编码器")

    # 组装 BGRA（行 4 字节对齐）
    stride = width * 4
    buf = _bgra_from_rgb(width, height, rgb)

    bmp = ctypes.c_void_p(0)
    st = gp.GdipCreateBitmapFromScan0(width, height, stride, PixelFormat32bppARGB, buf, ctypes.byref(bmp))
    if st != 0 or not bmp:
        raise RuntimeError(f"GdipCreateBitmapFromScan0 失败（状态码 {st}）")
    try:
        path = os.path.abspath(str(path))
        params = None
        if mime == "image/jpeg":
            q = ctypes.c_uint32(max(10, min(100, int(quality))))
            params = EncoderParameters()
            params.Count = 1
            params.Parameter[0].Guid = ENCODER_QUALITY
            params.Parameter[0].NumberOfValues = 1
            params.Parameter[0].Type = 4  # EncoderParameterValueTypeLong
            params.Parameter[0].Value = ctypes.cast(ctypes.byref(q), ctypes.c_void_p)
            st = gp.GdipSaveImageToFile(
                bmp, ctypes.c_wchar_p(path), ctypes.byref(clsid), ctypes.byref(params)
            )
        else:
            st = gp.GdipSaveImageToFile(bmp, ctypes.c_wchar_p(path), ctypes.byref(clsid), None)
        if st != 0:
            raise RuntimeError(f"保存图片失败（状态码 {st}）：{path}")
    finally:
        gp.GdipDisposeImage(bmp)


def probe_size(path) -> Optional[Tuple[int, int]]:
    """只取宽高（不读像素），用于快速过滤损坏图片。"""
    gp = _load()
    if gp is None:
        return None
    bmp = ctypes.c_void_p(0)
    try:
        if gp.GdipCreateBitmapFromFile(ctypes.c_wchar_p(os.path.abspath(str(path))), ctypes.byref(bmp)) != 0:
            return None
        w = ctypes.c_uint32(0)
        h = ctypes.c_uint32(0)
        gp.GdipGetImageWidth(bmp, ctypes.byref(w))
        gp.GdipGetImageHeight(bmp, ctypes.byref(h))
        return int(w.value), int(h.value)
    except Exception:
        return None
    finally:
        if bmp:
            gp.GdipDisposeImage(bmp)


def draw_text_bitmap(
    width: int,
    height: int,
    bg_rgb=(255, 255, 255),
    lines=None,
    font_size: int = 28,
    font_name: str = "SimHei",
    color=(0, 0, 0),
    margin: int = 40,
    line_spacing: float = 1.45,
):
    """用 GDI（不是 GDI+）把多行文字画成 RGB 缓冲 —— 供「图片版 PDF」/示例图使用。

    GDI 的 ``DrawTextW`` 对中文支持最稳（自动选字体、自动按宽度换行）；
    返回 ``(宽, 高, bytearray)``。
    """
    if not IS_WINDOWS:
        raise RuntimeError("draw_text_bitmap 仅支持 Windows")
    gdi32 = ctypes.WinDLL("gdi32")
    user32 = ctypes.WinDLL("user32")
    _declare_gdi(gdi32, user32)

    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    info = BITMAPINFO_HEADER(width, height)
    bits = ctypes.c_void_p(0)
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(info.header), 0, ctypes.byref(bits), None, 0)
    if not hbmp:
        raise RuntimeError("CreateDIBSection 失败")
    old_bmp = gdi32.SelectObject(hdc, hbmp)
    font = None
    old_font = None

    try:
        # 背景
        brush = gdi32.CreateSolidBrush(_rgb_ref(*bg_rgb))
        rect = wintypes.RECT(0, 0, width, height)
        user32.FillRect(hdc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)

        # 字体（负高度 = 字符高度，中文用 SimHei/黑体最清晰）
        font = gdi32.CreateFontW(
            -int(font_size), 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 0, 0, str(font_name)
        )
        old_font = gdi32.SelectObject(hdc, font)
        gdi32.SetTextColor(hdc, _rgb_ref(*color))
        gdi32.SetBkMode(hdc, 1)  # TRANSPARENT

        line_h = int(font_size * line_spacing)
        y = margin
        for text in lines or []:
            if y + line_h > height - margin // 2:
                break
            r = wintypes.RECT(margin, y, width - margin, y + line_h)
            # DT_LEFT(0) | DT_WORDBREAK(0x10) | DT_NOPREFIX(0x800)
            user32.DrawTextW(hdc, str(text), -1, ctypes.byref(r), 0x10 | 0x800)
            y += line_h

        # 取回像素（biHeight 为负 = 自顶向下，按原顺序拷即可；24bpp 时每像素 BGR）
        stride = ((width * 3 + 3) // 4) * 4
        raw = ctypes.string_at(bits, stride * height)
        rgb = bytearray(width * height * 3)
        for yy in range(height):
            s = yy * stride
            d = yy * width * 3
            row = raw[s : s + width * 3]
            rgb[d : d + width * 3 : 3] = row[2::3]
            rgb[d + 1 : d + width * 3 : 3] = row[1::3]
            rgb[d + 2 : d + width * 3 : 3] = row[0::3]
    finally:
        if old_font:
            gdi32.SelectObject(hdc, old_font)
        if font:
            gdi32.DeleteObject(font)
        gdi32.SelectObject(hdc, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, hdc_screen)
    return width, height, rgb


_GDI_DECLARED = False


def _declare_gdi(gdi32, user32) -> None:
    """声明 GDI/GDI32 函数的参数与返回类型。

    关键点：句柄必须是 64 位（``c_void_p``）。如果沿用 ctypes 默认的 ``c_int`` 返回类型，
    高 32 位会被截断，导致 SelectObject/DeleteObject 拿到非法句柄而静默失败。
    """
    global _GDI_DECLARED
    if _GDI_DECLARED:
        return
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint32]
    gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p,
    ]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    gdi32.SetTextColor.restype = ctypes.c_uint32
    gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.FillRect.restype = ctypes.c_int
    user32.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    user32.DrawTextW.restype = ctypes.c_int
    _GDI_DECLARED = True


def _rgb_ref(r: int, g: int, b: int) -> int:
    """GDI 的 COLORREF 是 0x00BBGGRR。"""
    return (int(b) << 16) | (int(g) << 8) | int(r)


def transform_rgb(
    src_w: int,
    src_h: int,
    rgb: bytes,
    out_w: int,
    out_h: int,
    angle: float = 0.0,
    offset=(0.0, 0.0),
    draw_size=None,
    matte=(255, 255, 255),
) -> Tuple[int, int, bytearray]:
    """用 GDI+ 做高质量几何变换（缩放 / 任意角度旋转 / 平移），一次性完成。

    做法（比堆世界变换更可靠）：把输出画布的原点平移到画布中心，按需旋转，
    再让 GDI+ 把源图**拉伸绘制**到指定的目标矩形 —— 目标矩形的尺寸就是缩放结果。

    - ``draw_size=None``：按源图原始尺寸绘制（旋转专用）
    - ``draw_size=(nw, nh)``：缩放到 nw×nh（缩放专用）

    输出画布底色为 ``matte``（纸张白）。非 Windows 平台由 imageops 的纯 Python 版本回退。
    """
    gp = _load()
    if gp is None:
        raise RuntimeError(init_error() or "GDI+ 不可用")
    if draw_size is None:
        dw, dh = int(src_w), int(src_h)
    else:
        dw, dh = int(draw_size[0]), int(draw_size[1])
    dw, dh = max(1, dw), max(1, dh)
    for name, argtypes in (
        ("GdipGetImageGraphicsContext", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        ("GdipSetInterpolationMode", [ctypes.c_void_p, ctypes.c_int32]),
        ("GdipGraphicsClear", [ctypes.c_void_p, ctypes.c_uint32]),
        ("GdipTranslateWorldTransform", [ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_int32]),
        ("GdipRotateWorldTransform", [ctypes.c_void_p, ctypes.c_float, ctypes.c_int32]),
        ("GdipDrawImageRectI", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                ctypes.c_int32, ctypes.c_int32]),
        ("GdipDeleteGraphics", [ctypes.c_void_p]),
        ("GdipScaleWorldTransform", [ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_int32]),
    ):
        fn = getattr(gp, name, None)
        if fn is not None and not fn.argtypes:
            fn.argtypes = argtypes

    src_buf = _bgra_from_rgb(src_w, src_h, rgb)
    dst_buf = (ctypes.c_ubyte * (out_w * out_h * 4))()

    src_bmp = ctypes.c_void_p(0)
    dst_bmp = ctypes.c_void_p(0)
    graphics = ctypes.c_void_p(0)
    if gp.GdipCreateBitmapFromScan0(
        src_w, src_h, src_w * 4, PixelFormat32bppARGB, src_buf, ctypes.byref(src_bmp)
    ) != 0:
        raise RuntimeError("创建源位图失败")
    try:
        if gp.GdipCreateBitmapFromScan0(
            out_w, out_h, out_w * 4, PixelFormat32bppARGB, dst_buf, ctypes.byref(dst_bmp)
        ) != 0:
            raise RuntimeError("创建目标位图失败")
        try:
            if gp.GdipGetImageGraphicsContext(dst_bmp, ctypes.byref(graphics)) != 0:
                raise RuntimeError("创建绘图上下文失败")
            argb = (255 << 24) | (int(matte[0]) << 16) | (int(matte[1]) << 8) | int(matte[2])
            gp.GdipGraphicsClear(graphics, ctypes.c_uint32(argb))
            gp.GdipSetInterpolationMode(graphics, 7)  # HighQualityBicubic
            # 画布中心 → 旋转 → 把源图拉伸画到居中的目标矩形（矩形尺寸即缩放后尺寸）
            gp.GdipTranslateWorldTransform(
                graphics, out_w / 2.0 + float(offset[0]), out_h / 2.0 + float(offset[1]), 1
            )
            if angle:
                gp.GdipRotateWorldTransform(graphics, float(angle), 1)
            gp.GdipDrawImageRectI(graphics, src_bmp, -dw // 2, -dh // 2, dw, dh)
            gp.GdipDeleteGraphics(graphics)
            graphics = ctypes.c_void_p(0)
            return out_w, out_h, _rgb_from_bgra(out_w, out_h, bytes(dst_buf))
        finally:
            if graphics:
                gp.GdipDeleteGraphics(graphics)
            gp.GdipDisposeImage(dst_bmp)
    finally:
        gp.GdipDisposeImage(src_bmp)


def _bgra_from_rgb(w: int, h: int, rgb: bytes):
    """RGB → BGRA（ctypes 缓冲，供 GDI+ 直接读）。"""
    stride = w * 4
    bgra = bytearray(stride * h)
    src = bytes(rgb)
    for y in range(h):
        o = y * w * 3
        d = y * stride
        row = src[o : o + w * 3]
        bgra[d : d + stride : 4] = row[2::3]
        bgra[d + 1 : d + stride : 4] = row[1::3]
        bgra[d + 2 : d + stride : 4] = row[0::3]
        bgra[d + 3 : d + stride : 4] = b"\xff" * w
    return (ctypes.c_ubyte * len(bgra)).from_buffer_copy(bgra)


def _rgb_from_bgra(w: int, h: int, bgra: bytes) -> bytearray:
    """BGRA → RGB 缓冲。"""
    stride = w * 4
    out = bytearray(w * h * 3)
    for y in range(h):
        row = bgra[y * stride : y * stride + w * 4]
        o = y * w * 3
        out[o : o + w * 3 : 3] = row[2::4]
        out[o + 1 : o + w * 3 : 3] = row[1::4]
        out[o + 2 : o + w * 3 : 3] = row[0::4]
    return out


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO_HEADER:
    """CreateDIBSection 需要的 BITMAPINFO（头 + 调色板占位）。

    默认用 **24 位**：GDI 的 32 位 DIB 会把 alpha 写成 0，后面复制像素时容易踩坑；
    24 位没有这个问题，且行按 4 字节对齐（读取时要按 ``stride`` 走）。
    """

    def __init__(self, width: int, height: int, bitcount: int = 24):
        self.header = BITMAPINFOHEADER()
        self.header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.header.biWidth = width
        self.header.biHeight = -height  # 负值 = 自顶向下，省一次翻转
        self.header.biPlanes = 1
        self.header.biBitCount = bitcount
        self.header.biCompression = 0  # BI_RGB
        self.stride = ((width * bitcount // 8 + 3) // 4) * 4
        self.header.biSizeImage = self.stride * height


if __name__ == "__main__":  # 简易自检
    print("GDI+ 可用:", available(), init_error())
