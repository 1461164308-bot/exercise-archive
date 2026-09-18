# -*- coding: utf-8 -*-
"""数学公式栅格化：把 LaTeX 片段画成**教科书式**位图（纯 ctypes + GDI，零第三方依赖）。

为什么必须自己画：tkinter 的文本控件不做数学排版，Unicode 也凑不出二维结构 ——
组合上划线（U+0305）在 Tk 里宽度为 0，根号罩不住被开方数；``e^(2πik/6)`` 这种
纯文本写法在错题解析里根本没法读。所以这里把公式画成图片再嵌进文本控件。

支持的结构（覆盖中小学到大学的常见习题写法）：
- 上下标：``x^2``、``a_{i}``、``e^{2\\pi i k/6}``、``\\sum_{i=1}^{n}``、``x_i^2``（上下标叠在同一列）
- 分式：``\\frac{a}{b}``、``\\dfrac``（上下两层 + 分数线）
- 根号：``\\sqrt{x}``、``\\sqrt[3]{x}``（√ 形 + 罩住被开方数的横杠）
- 希腊字母与常用符号：``\\alpha \\pi \\theta \\le \\ge \\times \\cdot \\infty \\int \\sum \\lim`` 等
- 大括号/矩阵：``\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}`` 以及 ``\\begin{vmatrix}``

关键实现要点（都是踩过坑才写对的）：
1. **逐字符字形回退**：``√ Σ ∫ α`` 在等宽字体里常缺字形（画成空白或方框），
   所以每个字符都查 ``GetGlyphIndicesW``，挑第一个真有该字形的字体来画。
2. **GDI 不写 alpha**：文字绘制只写 RGB，画完是 (255,255,255,0)，看起来像没画上。
   因此字形一律用**白墨**绘制 —— 亮度即覆盖率，最后按亮度合成到目标背景色上
   （深色前景若直接当 alpha 会几乎全透明，实测踩过）。
3. **句柄必须包成 ctypes 类型**：直接传 Python int 给声明了 argtypes 的 GDI 函数
   会被当 32 位截断，句柄失效、画出来一片空白。
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from .gdiplus import IS_WINDOWS

DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
ANTIALIASED_QUALITY = 4
FIXED_PITCH = 0x31
VARIABLE_PITCH = 0x01
TRANSPARENT_BK = 1
TA_LEFT, TA_TOP, TA_BASELINE = 0, 0, 24
GGI_MARK_NONEXISTING_GLYPHS = 0x0001
BI_RGB = 0
DIB_RGB_COLORS = 0

# 字形回退顺序：先数学字体（教科书观感），再符号字体，最后中文与等宽兜底
FONT_STACK = ("Cambria Math", "Segoe UI Symbol", "Consolas",
              "Microsoft YaHei UI", "SimSun", "Segoe UI")

_gdi32 = None
_user32 = None
_ready = False


def available() -> bool:
    """当前平台能不能渲染公式（需要 Windows GDI）。"""
    return IS_WINDOWS and _load() is not None


def _load():
    global _gdi32, _user32, _ready
    if _ready:
        return _gdi32
    if not IS_WINDOWS:
        _ready = True
        return None
    try:
        g = ctypes.WinDLL("gdi32")
        u = ctypes.WinDLL("user32")
        g.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_wchar_p,
        ]
        g.CreateFontW.restype = ctypes.c_void_p
        g.GetTextExtentPoint32W.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                            ctypes.c_int, ctypes.POINTER(SIZE)]
        g.GetTextMetricsW.argtypes = [ctypes.c_void_p, ctypes.POINTER(TEXTMETRIC)]
        g.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        g.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.TextOutW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_wchar_p, ctypes.c_int]
        g.SetTextAlign.argtypes = [ctypes.c_void_p, wintypes.UINT]
        g.GetGlyphIndicesW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                       ctypes.POINTER(wintypes.WORD), wintypes.DWORD]
        g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                       ctypes.c_uint32]
        g.CreateDIBSection.restype = ctypes.c_void_p
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        g.DeleteDC.argtypes = [ctypes.c_void_p]
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.GetDC.restype = ctypes.c_void_p
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _gdi32, _user32 = g, u
    except Exception:
        _gdi32 = None
    _ready = True
    return _gdi32


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class TEXTMETRIC(ctypes.Structure):
    _fields_ = [
        ("tmHeight", wintypes.LONG), ("tmAscent", wintypes.LONG),
        ("tmDescent", wintypes.LONG), ("tmInternalLeading", wintypes.LONG),
        ("tmExternalLeading", wintypes.LONG), ("tmAveCharWidth", wintypes.LONG),
        ("tmMaxCharWidth", wintypes.LONG), ("tmWeight", wintypes.LONG),
        ("tmOverhang", wintypes.LONG), ("tmDigitizedAspectX", wintypes.LONG),
        ("tmDigitizedAspectY", wintypes.LONG), ("tmFirstChar", wintypes.WCHAR),
        ("tmLastChar", wintypes.WCHAR), ("tmDefaultChar", wintypes.WCHAR),
        ("tmBreakChar", wintypes.WCHAR), ("tmItalic", ctypes.c_byte),
        ("tmUnderlined", ctypes.c_byte), ("tmStruckOut", ctypes.c_byte),
        ("tmPitchAndFamily", ctypes.c_byte), ("tmCharSet", ctypes.c_byte),
    ]


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _Dib:
    """32 位、自顶向下的内存位图（BGRA），GDI 可直接往上画。"""

    def __init__(self, w: int, h: int):
        g = _load()
        if g is None:
            raise RuntimeError("GDI 不可用，无法渲染公式")
        self.w, self.h = max(1, int(w)), max(1, int(h))
        self.stride = self.w * 4
        self.size = self.stride * self.h
        self.bmi = _BMIH()
        self.bmi.biSize = ctypes.sizeof(_BMIH)
        self.bmi.biWidth = self.w
        self.bmi.biHeight = -self.h          # 负 = 自顶向下
        self.bmi.biPlanes = 1
        self.bmi.biBitCount = 32
        self.bmi.biCompression = BI_RGB
        self.bmi.biSizeImage = self.size
        self._bits = ctypes.c_void_p()
        screen = _user32.GetDC(None)
        self.dc = _gdi32.CreateCompatibleDC(screen)
        self.hbmp = _gdi32.CreateDIBSection(screen, ctypes.byref(self.bmi), DIB_RGB_COLORS,
                                            ctypes.byref(self._bits), None, 0)
        _user32.ReleaseDC(None, screen)
        if not self.hbmp:
            raise OSError("CreateDIBSection 失败")
        self._old = _gdi32.SelectObject(self.dc, self.hbmp)

    def buffer(self):
        return ctypes.cast(self._bits, ctypes.POINTER(ctypes.c_ubyte * self.size)).contents

    def hdc(self):
        """句柄必须包成 ctypes 类型，否则会被当 32 位截断（实测踩过）。"""
        return ctypes.c_void_p(self.dc)

    def close(self) -> None:
        try:
            _gdi32.SelectObject(self.dc, self._old)
            _gdi32.DeleteObject(self.hbmp)
            _gdi32.DeleteDC(self.dc)
        except Exception:
            pass


def _rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


def _hex(color) -> Tuple[int, int, int]:
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        return int(color[0]), int(color[1]), int(color[2])
    c = str(color or "#000000").lstrip("#")
    if len(c) != 6:
        return (0, 0, 0)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return (0, 0, 0)


class _FontSet:
    """一组同字号的候选字体，逐字符挑第一个真有该字形的来画。"""

    def __init__(self, size_px: int):
        self.size = size_px
        self.fonts: List[Tuple[int, str]] = []
        for family in FONT_STACK:
            hf = _gdi32.CreateFontW(
                -int(size_px), 0, 0, 0, 400, 0, 0, 0, DEFAULT_CHARSET,
                OUT_TT_PRECIS, 0, ANTIALIASED_QUALITY,
                FIXED_PITCH if family == "Consolas" else VARIABLE_PITCH, family)
            if hf:
                self.fonts.append((hf, family))
        if not self.fonts:
            self.fonts.append((_gdi32.CreateFontW(-int(size_px), 0, 0, 0, 400, 0, 0, 0,
                                                  0, 0, 0, 0, 0, "Segoe UI"), "Segoe UI"))
        self._pick: Dict[str, int] = {}
        self._width: Dict[str, int] = {}

    def pick(self, ch: str, dc) -> int:
        idx = self._pick.get(ch)
        if idx is not None:
            return idx
        best = 0
        for i, (hf, _name) in enumerate(self.fonts):
            old = _gdi32.SelectObject(dc, hf)
            glyphs = (wintypes.WORD * 1)()
            res = _gdi32.GetGlyphIndicesW(dc, ch, 1, glyphs, GGI_MARK_NONEXISTING_GLYPHS)
            _gdi32.SelectObject(dc, old)
            if res and glyphs[0] != 0xFFFF:
                best = i
                break
        self._pick[ch] = best
        return best

    def width(self, ch: str, dc) -> int:
        w = self._width.get(ch)
        if w is not None:
            return w
        hf, _ = self.fonts[self.pick(ch, dc)]
        old = _gdi32.SelectObject(dc, hf)
        size = SIZE()
        _gdi32.GetTextExtentPoint32W(dc, ch, 1, ctypes.byref(size))
        _gdi32.SelectObject(dc, old)
        self._width[ch] = int(size.cx)
        return int(size.cx)

    def metrics(self, dc) -> TEXTMETRIC:
        hf, _ = self.fonts[0]
        old = _gdi32.SelectObject(dc, hf)
        tm = TEXTMETRIC()
        _gdi32.GetTextMetricsW(dc, ctypes.byref(tm))
        _gdi32.SelectObject(dc, old)
        return tm


class _Canvas:
    """在一张 _Dib 上绘制（字形用白墨，最后按亮度合成到背景色）。"""

    def __init__(self, dib: _Dib):
        self.dib = dib
        _gdi32.SetBkMode(dib.hdc(), TRANSPARENT_BK)
        _gdi32.SetTextColor(dib.hdc(), _rgb(255, 255, 255))
        self._sets: Dict[int, _FontSet] = {}

    def fontset(self, size_px: int) -> _FontSet:
        fs = self._sets.get(size_px)
        if fs is None:
            fs = _FontSet(size_px)
            self._sets[size_px] = fs
        return fs

    def metrics(self, size_px: int) -> Tuple[int, int, int]:
        tm = self.fontset(size_px).metrics(self.dib.hdc())
        return int(tm.tmHeight), int(tm.tmAscent), int(tm.tmDescent)

    def text_width(self, text: str, size_px: int) -> int:
        fs = self.fontset(size_px)
        return sum(fs.width(ch, self.dib.hdc()) for ch in text)

    def draw_text(self, text: str, x: int, baseline: int, size_px: int) -> None:
        fs = self.fontset(size_px)
        pos = x
        for ch in text:
            if ch == " ":
                pos += fs.width(ch, self.dib.hdc())
                continue
            hf, _ = fs.fonts[fs.pick(ch, self.dib.hdc())]
            old = _gdi32.SelectObject(self.dib.hdc(), hf)
            _gdi32.SetTextAlign(self.dib.hdc(), TA_LEFT | TA_BASELINE)
            _gdi32.TextOutW(self.dib.hdc(), int(pos), int(baseline), ch, 1)
            _gdi32.SetTextAlign(self.dib.hdc(), TA_LEFT | TA_TOP)
            _gdi32.SelectObject(self.dib.hdc(), old)
            pos += fs.width(ch, self.dib.hdc())

    # -- 图元（分数线、根号斜撇、矩阵括号） ------------------------------- #
    def _ink(self, x: int, y: int, alpha: int = 255) -> None:
        if x < 0 or y < 0 or x >= self.dib.w or y >= self.dib.h:
            return
        o = y * self.dib.stride + x * 4
        buf = self.dib.buffer()
        buf[o] = buf[o + 1] = buf[o + 2] = 255     # 白墨；最后统一染色
        buf[o + 3] = alpha

    def hline(self, x0: int, x1: int, y: int, thickness: int = 1) -> None:
        for dy in range(max(1, thickness)):
            for x in range(int(min(x0, x1)), int(max(x0, x1)) + 1):
                self._ink(x, int(y) + dy)

    def vline(self, x: int, y0: int, y1: int, thickness: int = 1) -> None:
        for y in range(int(min(y0, y1)), int(max(y0, y1)) + 1):
            for dx in range(max(1, thickness)):
                self._ink(int(x) + dx, y)

    def diag(self, x0: int, y0: int, x1: int, y1: int, thickness: int = 1) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i / steps
            y = y0 + (y1 - y0) * i / steps
            for dx in range(max(1, thickness)):
                self._ink(int(round(x)) + dx, int(round(y)))

    # -- 字形实测（用来把括号缩放到"正好罩住内容"的高度） ------------------ #
    def glyph_ink(self, ch: str, size_px: int) -> Tuple[int, int, int]:
        """实测字符的墨迹：返回 ``(基线以上高度, 基线以下深度, 总高)``（像素，带缓存）。

        为什么不用 ``GetTextMetrics``：数学字体（Cambria Math）的 ascent/descent 能到
        字号的 5 倍以上，拿它算基线会把括号画到画面外（实测：括号整段被裁掉）。
        这里直接画一次、扫墨迹，拿到真实的上高/下深。
        """
        key = (ch, size_px)
        cache = getattr(self, "_ink_cache", None)
        if cache is None:
            cache = self._ink_cache = {}
        if key in cache:
            return cache[key]
        w = max(8, self.text_width(ch, size_px) + 8)
        h = max(12, int(size_px * 3))
        baseline_y = int(size_px * 1.8)
        dib = _Dib(w, h)
        try:
            scratch = _Canvas(dib)
            scratch.draw_text(ch, 4, baseline_y, size_px)
            buf = dib.buffer()
            top = bottom = None
            for y in range(h):
                base = y * w * 4
                for x in range(w):
                    if buf[base + x * 4 + 3]:
                        if top is None:
                            top = y
                        bottom = y
                        break
            if top is None:
                result = (max(1, int(size_px * 0.7)), max(0, int(size_px * 0.2)),
                          max(1, int(size_px * 0.9)))
            else:
                asc = max(0, baseline_y - top)
                desc = max(0, bottom - baseline_y)
                result = (asc, desc, bottom - top + 1)
        finally:
            dib.close()
        cache[key] = result
        return result

    def glyph_ink_height(self, ch: str, size_px: int) -> int:
        return self.glyph_ink(ch, size_px)[2]


# 文字模式命令：内容按普通文字排版（**保留空格**，如 \text{当且仅当 x>0}）
_TEXT_MODE_CMDS = ("text", "textrm", "textnormal", "textsf", "texttt", "mbox", "hbox",
                   "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal",
                   "mathbb", "mathfrak", "boldsymbol", "operatorname")

CMD_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ",
    "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "ast": "∗",
    "neq": "≠", "ne": "≠", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "ll": "≪", "gg": "≫", "approx": "≈", "equiv": "≡", "sim": "∼",
    "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "sum": "Σ", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃", "subseteq": "⊆",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "angle": "∠", "perp": "⊥",
    "parallel": "∥", "triangle": "△", "degree": "°", "circ": "∘",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "mapsto": "↦", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
    "ddots": "⋱", "quad": "  ", "qquad": "    ", " ": " ",
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "sec": "sec",
    "csc": "csc", "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan",
    "log": "log", "ln": "ln", "lg": "lg", "lim": "lim", "exp": "exp",
    "max": "max", "min": "min", "det": "det", "gcd": "gcd", "sup": "sup",
    "inf": "inf", "arg": "arg", "deg": "deg", "mod": "mod",
    "prime": "′", "circ": "∘", "bullet": "•", "star": "⋆",
}


# 关系符（两侧留中等间距）与二元运算符（两侧留细间距）—— 按 LaTeX 的数学间距规则
_REL_CHARS = "=<>≤≥≠≈≡→←↔⇒⇔∈∉⊂⊃"
_BIN_CHARS = "+-±∓×÷·⋅∪∩"
_OPENERS = "([{|,;:"
# 这些位置上的 + - 是一元符号（如 -b、x^{-1}、(a, -b)），不留间距
_UNARY_BEFORE = set("([{|,;:^_") | set(_REL_CHARS) | set(_BIN_CHARS)

# 函数名/算子名：LaTeX 在它们后面留一个细空格（``\sin x`` 排成 "sin x" 而不是 "sinx"）。
# 注意 ``\pi`` 这类**符号**不在此列 —— 符号后面不留空格（``\pi r`` 就是 "πr"）。
_OPNAME_CMDS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "log", "ln", "lg", "exp", "mod",
}
# 上下限要排在**正上方/正下方**的算符（LaTeX 里带 \limits 的那批）。
# 求和/连乘是符号，lim/max/min 是函数名，两类都算。
_BIGOP_SYMBOLS = {
    "sum", "prod", "coprod", "bigcup", "bigcap", "bigvee", "bigwedge",
    "bigoplus", "bigotimes", "bigodot", "biguplus", "bigsqcup",
}
_LIMITS_CMDS = _BIGOP_SYMBOLS | {
    "lim", "max", "min", "sup", "inf", "det", "gcd", "arg", "dim", "ker", "hom", "deg",
}
# 带上限时算符符号放大的倍数：课本里 Σ/∏ 明显比正文大一圈（LaTeX display style 同理）。
# 只放大**符号**，不放大 lim/max 这类函数名（那是单词，放大就不像了）。
_BIGOP_GROW = 1.30
# 紧跟在这些字符后面时，函数名不需要补细空格（``\lim_{x\to 0}``、``\sin^2 x``）
_NO_SPACE_AFTER_OP = set("^_}]),;:")


def _auto_space(latex: str) -> str:
    """给运算符两侧补上数学间距（LaTeX 会自动做，我们得自己来）。

    不做的话 ``x=1``、``a+b`` 会挤在一起，看着不像课本（用户反馈"看着奇怪"）。
    ``\\text{...}`` 里的内容原样不动；一元符号（``-b``、``x^{-1}``）不加间距。

    另一件同样重要的事：**命令名后面的空格不能一律丢掉**。
    ``\\pi r`` 丢掉空格就成了 ``\\pir``（不存在的命令），于是 π 被当成字面文本画成 "pir"
    —— 用户报过「pi 没有用圆周率符号表示」，根因就在这里。
    LaTeX 里那个空格正是命令名的终止符，必须留住；函数名（``\\sin x``）后面还要再补一个细空格。
    """
    src = latex or ""
    out: List[str] = []
    i, n = 0, len(src)
    depth_prev = ""            # 上一个有意义的字符（判断是否一元上下文）
    # 上下标（script style）里**不加**关系符/运算符间距 —— TeX 的规则：
    # mu 间距只在 display/text style 里插，script/scriptscript 不插。
    # 所以课本里 Σ 下面的限写作 "k=0"、"i=1"、"x→0"，而不是 "k = 0"。
    brace_ctx: List[bool] = []     # 每层 { 记录：它是不是 ^/_ 的参数
    script_next = False
    while i < n:
        ch = src[i]
        if ch == "\\":
            m = re.match(r"\\[A-Za-z]+|\\.", src[i:])
            if m:
                cmd = m.group(0)
                out.append(cmd)
                i += len(cmd)
                # \text{...} 这类文字模式整段跳过
                if cmd[1:].lstrip("\\") in _TEXT_MODE_CMDS and i < n and src[i] == "{":
                    depth, j = 1, i + 1
                    while j < n and depth:
                        if src[j] == "{":
                            depth += 1
                        elif src[j] == "}":
                            depth -= 1
                        j += 1
                    out.append(src[i:j])
                    i = j
                # 函数名后面补细空格：`\sin x` 要排成 "sin x"，而不是挤成 "sinx"
                elif cmd[1:] in _OPNAME_CMDS:
                    j = i
                    while j < n and src[j] in " \t":
                        j += 1
                    if j < n and src[j] not in _NO_SPACE_AFTER_OP:
                        out.append("\\,")
                        i = j                 # 原来的空格被这个细空格取代
                depth_prev = cmd
                script_next = False
                continue
        if ch in _REL_CHARS:
            if any(brace_ctx):
                out.append(ch)                  # 上下标里：紧排
            else:
                out.append("\\:" if depth_prev not in ("", "\\:") else "")
                out.append(ch)
                out.append("\\:")
            depth_prev = ch
            script_next = False
            i += 1
            continue
        if ch in _BIN_CHARS:
            if any(brace_ctx):
                out.append(ch)                  # 上下标里：紧排
            elif depth_prev in ("",) or depth_prev in _UNARY_BEFORE:
                out.append(ch)                  # 一元：不加间距
            else:
                out.append("\\,")
                out.append(ch)
                out.append("\\,")
            depth_prev = ch
            script_next = False
            i += 1
            continue
        if ch in "{":
            out.append(ch)
            brace_ctx.append(script_next)
            script_next = False
            depth_prev = ""                 # 组开头 → 视为一元上下文
            i += 1
            continue
        if ch == "}":
            out.append(ch)
            if brace_ctx:
                brace_ctx.pop()
            i += 1
            continue
        if ch in " \t":
            # 数学模式里空格一般只是分隔符，可以丢 —— 但**命令名后面那个空格不能丢**：
            # `\pi r` 丢掉空格就成了 `\pir`（不存在的命令），π 于是被画成字面文本 "pir"。
            # 只有「上一个输出是命令名」且「下一个非空字符是字母」时才保留这一个空格。
            nxt = src[i + 1] if i + 1 < n else ""
            if re.fullmatch(r"\\[A-Za-z]+", depth_prev or "") and nxt.isalpha():
                out.append(" ")
            i += 1
            continue
        out.append(ch)
        if ch in "^_":
            script_next = True
        elif ch not in "^_":
            depth_prev = ch
            script_next = False
        i += 1
    return "".join(out)


def _tighten(box: dict, k: float = 0.80) -> dict:
    """把包围盒的上下留白按比例收紧。

    数学字形很少用满字体的 ascent/descent（Cambria Math 尤其宽松），
    不收紧的话矩阵行距会非常空、外框括号也会跟着变得巨大。
    只改 above/below（用于算外框尺寸），**不动绘制基线**，所以文字位置不受影响。
    """
    out = dict(box)
    out["above"] = max(1, int(box.get("above", 0) * k))
    out["below"] = max(0, int(box.get("below", 0) * k))
    return out


class Renderer:
    """把 LaTeX token 排成「原子」，并画到画布上（自动算包围盒）。"""

    def __init__(self, canvas: _Canvas, size_px: int = 17):
        self.c = canvas
        self.size = size_px
        self.scale = max(0.62, size_px / 17.0)

    # ---------------------------------------------------------------- 解析
    @staticmethod
    def tokenize(src: str, keep_spaces: bool = False) -> list:
        """切 token：("text", …) / ("cmd", name) / ("group", [tokens]) / ("matrix", …)。

        ``^``/``_`` 必须单独成 token，参数（{...} 或紧随的一个字符）也切开 ——
        否则 ``x^2`` 会整段当普通文本，画出来就是字面的 "x^2"。

        **空格规则**：数学模式里空格只是分隔符，LaTeX 不显示它 ——
        ``e^{2\\pi i k/n}`` 应当渲染成 ``e^{2πik/n}``（而不是 ``2πi k/n``，用户报过这个）。
        所以默认丢掉源码里的空白；只有 ``\\text{...}`` 这类文字模式才保留（``keep_spaces=True``）。
        显式间距命令 ``\\,`` ``\\;`` ``\\quad`` 不受影响，它们走的是自己的分支。
        """
        src = _strip_matrix_env(src)
        toks: list = []
        i, n = 0, len(src)
        buf: List[str] = []

        def flush() -> None:
            if buf:
                toks.append(("text", "".join(buf)))
                buf.clear()

        def read_group(start: int, keep: bool = False) -> Tuple[list, int]:
            depth, j = 1, start + 1
            while j < n and depth:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                j += 1
            return Renderer.tokenize(src[start + 1:j - 1], keep), j

        def read_paren(start: int) -> Tuple[str, int]:
            """``(`` 起读，返回 (括号内文本, ``)`` 之后的位置)。

            纯文本写法 ``e^(2πik/6)`` 里，上标参数是**一整个括号组**；
            早期只取紧跟的一个字符（那个左括号），于是渲染成 ``e`` 加一个悬空的 ``(``
            —— 用户报过这个 bug。这里按括号配对整组读取，并**去掉多余的外层括号**
            （``e^{(n-1)}`` 在课本里写作 ``e^{n-1}``）。
            """
            depth, j = 1, start + 1
            while j < n and depth:
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                j += 1
            return src[start + 1:j - 1], j

        while i < n:
            ch = src[i]
            if ch == "\x00":            # 矩阵占位符：\x00{env}\x04{rows}\x01
                flush()
                j = src.index("\x01", i)
                payload = src[i + 1:j]
                env, _, rows_blob = payload.partition("\x04")
                rows = [r.split("\x02") for r in rows_blob.split("\x03") if r]
                toks.append(("matrix", rows, env))
                i = j + 1
                continue
            if ch == "\\":
                m = re.match(r"\\([A-Za-z]+)\s*", src[i:])
                if m:
                    flush()
                    name = m.group(1)
                    i += m.end()
                    # 文字模式（\text \mathrm \operatorname…）：内容原样保留，含空格
                    if name in _TEXT_MODE_CMDS and i < n and src[i] == "{":
                        body, i = read_group(i, keep=True)
                        toks.append(("group", body))
                    else:
                        toks.append(("cmd", name))
                    continue
                # \, \; \: \! 这类「细空格」，以及 \{ \} \% 等转义符号
                nxt = src[i + 1] if i + 1 < n else "\\"
                flush()
                if nxt in " ,;:!>":
                    toks.append(("text", " " if nxt in ",;: >" else ""))
                else:
                    toks.append(("text", nxt))
                i += 2
                continue
            if ch in "^_":
                flush()
                toks.append(("text", ch))
                i += 1
                if i < n and src[i] == "{":
                    body, i = read_group(i)
                    toks.append(("group", body))
                elif i < n and src[i] == "(":
                    # 纯文本写法 e^(2πik/6)：整组当上标，并去掉多余外层括号
                    body, i = read_paren(i)
                    toks.append(("group", Renderer.tokenize(body)))
                elif i < n:
                    toks.append(("text", src[i]))
                    i += 1
                continue
            if ch == "{":
                flush()
                body, i = read_group(i)
                toks.append(("group", body))
                continue
            if ch == "}":
                i += 1
                continue
            if ch in " \t\r\n" and not keep_spaces:
                i += 1                  # 数学模式：空白只是分隔符，丢掉
                continue
            buf.append(ch)
            i += 1
        flush()
        return toks

    @staticmethod
    def _script_arg(toks: list, k: int):
        if k >= len(toks):
            return None, k
        tok = toks[k]
        if tok[0] == "group":
            return tok[1], k + 1
        return [tok], k + 1

    # ---------------------------------------------------------------- 布局
    def layout(self, toks: list, size_px: Optional[int] = None) -> dict:
        size = size_px or self.size
        atoms: List[dict] = []
        # 同上：用字号比例而不是字体行高
        above, below = max(1, int(size * 0.72)), max(0, int(size * 0.22))

        i = 0
        while i < len(toks):
            tok = toks[i]
            kind = tok[0]
            value = tok[1] if len(tok) > 1 else None
            nxt = toks[i + 1] if i + 1 < len(toks) else None

            if kind == "matrix":
                # token 形式：("matrix", rows, env)
                atoms.append(self.matrix(value, size, tok[2] if len(tok) > 2 else "pmatrix"))
                i += 1
            elif kind == "cmd" and value in ("frac", "dfrac", "tfrac") \
                    and i + 2 < len(toks) and toks[i + 1][0] == "group" \
                    and toks[i + 2][0] == "group":
                atoms.append(self.fraction(toks[i + 1][1], toks[i + 2][1], size))
                i += 3
            elif kind == "cmd" and value == "sqrt":
                j = i + 1
                degree = None
                if j < len(toks) and toks[j][0] == "text" and toks[j][1].startswith("["):
                    deg_buf = toks[j][1]
                    while j + 1 < len(toks) and "]" not in deg_buf:
                        j += 1
                        if toks[j][0] == "text":
                            deg_buf += toks[j][1]
                    degree = deg_buf.strip("[]")
                    j += 1
                if j < len(toks) and toks[j][0] == "group":
                    atoms.append(self.radical(toks[j][1], size, degree))
                    i = j + 1
                else:
                    atoms.append(self.plain("√", size))
                    i += 1
            elif kind == "cmd" and value in ("left", "right", "displaystyle",
                                             "textstyle", "limits", "nolimits"):
                i += 1
            elif kind == "cmd" and value in ("text", "mathrm", "mathbf", "mathit",
                                             "operatorname", "mbox", "boxed", "textbf"):
                if nxt and nxt[0] == "group":
                    atoms.append(self.layout(nxt[1], size))
                    i += 2
                else:
                    i += 1
            elif kind == "cmd":
                atom = self.plain(CMD_SYMBOLS.get(value, value), size)
                if value in _LIMITS_CMDS:
                    # 标记成「大算符」：上下限要排在正上/正下方，而不是像 x^2 那样甩到右边
                    atom["limits"] = True
                    atom["grow"] = value in _BIGOP_SYMBOLS
                atoms.append(atom)
                i += 1
            elif kind == "group":
                atoms.append(self.layout(value, size))
                i += 1
            else:
                text = value
                if not text:
                    i += 1
                    continue
                if text[0] in "^_" and len(text) == 1:
                    arg, j = self._script_arg(toks, i + 1)
                    if arg is None:
                        i += 1
                    elif not atoms:
                        i = j
                    else:
                        base = atoms.pop()
                        sup_arg = arg if text[0] == "^" else None
                        sub_arg = arg if text[0] == "_" else None
                        # 大算符（Σ/∏/lim…）的上下限排正上/正下方；
                        # 注意 base 也可能是**已经带上限的 bigop 盒**（\sum_a^b 分两次进来），
                        # 那时同样要并进去，否则第二个限又会甩到右边（"岔开"就是这么来的）。
                        if base.get("limits") or base.get("kind") == "bigop":
                            atoms.append(self.limits_box(base, sup_arg, sub_arg, size))
                        else:
                            atoms.append(self.script_box(base, sup_arg, sub_arg, size))
                        i = j
                    continue
                j = i + 1
                sup = sub_ = None
                while j < len(toks) and toks[j][0] == "text" and toks[j][1][:1] in ("^", "_"):
                    marker = toks[j][1][0]
                    rest = toks[j][1][1:]
                    if rest:
                        arg, j = [("text", rest)], j + 1
                    else:
                        arg, j = self._script_arg(toks, j + 1)
                        if arg is None:
                            break
                    if marker == "^":
                        sup = arg
                    else:
                        sub_ = arg
                if sup or sub_:
                    atoms.append(self.scripted(text, sup, sub_, size))
                else:
                    atoms.append(self.plain(text, size))
                i = j
        return {"kind": "atoms", "atoms": atoms,
                "width": sum(a["width"] for a in atoms),
                "above": max([above] + [a["above"] for a in atoms]),
                "below": max([below] + [a["below"] for a in atoms])}

    def plain(self, text: str, size: int) -> dict:
        """普通文本原子。

        **不能**用字体度量（``GetTextMetrics``）当高度：Cambria Math 这类数学字体的
        行高约为字号的 2.2 倍（它要为分数/大算符预留空间），拿它当内容高度会让矩阵
        行距、上下标间距全都空得离谱（实测：2×2 矩阵被撑成两倍高）。
        所以按字号的固定比例给上下留白（约 0.72 / 0.22 em），与 LaTeX 的做法一致。
        """
        w = self.c.text_width(text, size)
        return {"kind": "text", "text": text, "size": size, "width": w,
                "above": max(1, int(size * 0.72)), "below": max(0, int(size * 0.22))}

    def scripted(self, base: str, sup, sub_, size: int) -> dict:
        return self.script_box(self.plain(base, size), sup, sub_, size)

    def limits_box(self, base_box: dict, sup, sub_, size: int) -> dict:
        """把上/下限排在算符的**正上方、正下方**（求和、连乘、lim 这类大算符的教科书写法）。

        与 :meth:`script_box` 的区别：script_box 把上下标甩到符号**右侧**（``x^2`` 那样），
        而大算符按 LaTeX 的 display style 是**居中排在正上/正下**。
        用户报过「求和符号的上下参数岔开了」，就是它被当成了普通上标 —— 画出来像
        ``Σ_{k=0}^{n-1}`` 那样斜着拖在右边，而不是课本里的 ``Σ`` 上下一摞。

        ``\\sum_a^b`` 会分两次进来（先 ``_`` 后 ``^``，或反过来），所以第二次要把第一个并进来。
        """
        sub_size = max(8, int(size * 0.70))
        if base_box.get("kind") == "bigop":
            base = base_box["base"]
            gap = base_box["gap"]
            sup_box = self.layout(sup, sub_size) if sup else base_box.get("sup")
            sub_box = self.layout(sub_, sub_size) if sub_ else base_box.get("sub")
        else:
            # Σ/∏ 这类**符号**放大一档，上下限才不至于比符号还显眼（课本就是这个比例）；
            # lim/max 这类函数名保持原字号。
            if base_box.get("grow"):
                op_size = max(base_box["size"] + 1, int(round(base_box["size"] * _BIGOP_GROW)))
                base = self.plain(base_box["text"], op_size)
            else:
                base = base_box
            gap = max(1, int(size * 0.08))
            sup_box = self.layout(sup, sub_size) if sup else None
            sub_box = self.layout(sub_, sub_size) if sub_ else None

        # 用**实测墨迹**而不是 plain() 的固定比例来定上下限位置：Σ/∏ 的字形只占字号的一部分，
        # 按 0.72em 排会让上下限离符号很远 —— 观感上"岔开"有一半来自这里。
        if base.get("kind") == "text":
            ink_asc, ink_desc, _h = self.c.glyph_ink(base["text"], base["size"])
        else:
            ink_asc, ink_desc = base["above"], base["below"]
        width = max([base["width"]] + [b["width"] for b in (sup_box, sub_box) if b])
        above = max(base["above"],
                    ink_asc + (gap + sup_box["above"] + sup_box["below"] if sup_box else 0))
        below = max(base["below"],
                    ink_desc + (gap + sub_box["above"] + sub_box["below"] if sub_box else 0))
        return {"kind": "bigop", "base": base, "sup": sup_box, "sub": sub_box, "gap": gap,
                "ink_asc": ink_asc, "ink_desc": ink_desc,
                "width": width, "above": above, "below": below, "size": size}

    def script_box(self, base_box: dict, sup, sub_, size: int) -> dict:
        """给任意原子加下上标（底也可以是分式、根号、整组）。"""
        sub_size = max(8, int(size * 0.70))
        sup_box = self.layout(sup, sub_size) if sup else None
        sub_box = self.layout(sub_, sub_size) if sub_ else None
        raise_ = max(int(size * 0.36), min(int(size * 0.52), int(base_box["above"] * 0.62)))
        drop = max(int(size * 0.10), min(int(size * 0.28), int(base_box["below"] * 0.55)))
        dx = max(1, int(size * 0.10))
        w, above, below = base_box["width"], base_box["above"], base_box["below"]
        if sup_box:
            above = max(above, raise_ + sup_box["above"])
        if sub_box:
            below = max(below, drop + sub_box["below"])
        if sup_box and sub_box:
            w += dx + max(sup_box["width"], sub_box["width"])
        elif sup_box:
            w += dx + sup_box["width"]
        elif sub_box:
            w += dx + sub_box["width"]
        return {"kind": "script", "base": base_box, "sup": sup_box, "sub": sub_box,
                "width": w, "above": above, "below": below, "size": size,
                "raise": raise_, "drop": drop, "dx": dx}

    def fraction(self, num_toks, den_toks, size: int) -> dict:
        inner = max(9, int(size * 0.94))
        num = self.layout(num_toks, inner)
        den = self.layout(den_toks, inner)
        pad = max(2, int(size * 0.16))
        bar = max(1, round(self.scale * 1.1))
        gap = max(1, int(size * 0.12))
        width = max(num["width"], den["width"]) + pad * 2
        above = num["above"] + num["below"] + gap + bar
        below = den["above"] + den["below"] + gap
        return {"kind": "frac", "num": num, "den": den, "width": width,
                "above": above, "below": below, "pad": pad, "gap": gap, "bar": bar}

    def radical(self, body_toks, size: int, degree: Optional[str] = None) -> dict:
        body = self.layout(body_toks, size)
        pad_x = max(2, int(size * 0.16))
        tail = max(3, int(size * 0.20))
        hole = max(3, int(size * 0.26))
        hook = max(6, int(size * 0.40))
        degree_box = None
        lead = 0
        if degree:
            degree_box = self.layout([("text", degree)], max(8, int(size * 0.58)))
            lead = degree_box["width"]
        width = lead + hook + hole + body["width"] + pad_x + tail
        bar = max(1, round(self.scale * 1.1))
        above = max(body["above"] + int(size * 0.24), int(size * 0.58))
        return {"kind": "sqrt", "body": body, "degree": degree_box, "width": width,
                "above": above, "below": body["below"], "hook": hook, "hole": hole,
                "pad": pad_x, "tail": tail, "bar": bar, "lead": lead}

    def matrix(self, rows: List[List[str]], size: int, env: str = "pmatrix") -> dict:
        """矩阵 / 行列式：每行按列宽对齐，外面按环境加括号。

        - ``pmatrix``/``bmatrix``：圆括号/方括号（竖线 + 朝内的上下小横钩）
        - ``vmatrix``/``Vmatrix``：行列式的竖线（不画横钩）
        - ``cases``：只画左边的大括号
        """
        inner = max(9, int(size * 0.94))
        cells = [[_tighten(self.layout(Renderer.tokenize(c.strip()), inner)) for c in row]
                 for row in rows]
        cols = max((len(r) for r in cells), default=1)
        col_w = [max((cells[r][c]["width"] if c < len(cells[r]) else 0)
                     for r in range(len(cells))) for c in range(cols)]
        gap = max(5, int(size * 0.50))
        line_h = max((cell["above"] + cell["below"] for row in cells for cell in row),
                     default=int(size * 1.0))
        row_h = [line_h + int(size * 0.18) for _ in cells] or [int(size * 1.2)]
        body_w = sum(col_w) + gap * max(0, cols - 1)
        body_h = sum(row_h)
        bar = max(1, round(self.scale * 1.2))
        # 行列式（vmatrix/Vmatrix）两侧都是竖线；cases 只有左边大括号
        left = env in ("pmatrix", "bmatrix", "vmatrix", "Vmatrix", "cases")
        right = env in ("pmatrix", "bmatrix", "vmatrix", "Vmatrix")
        # 括号要**按字形实际宽度**预留空间，字号按实测比例反推：
        # Cambria Math 的括号比字号本身高不少，直接拿 body_h 当字号会大出一圈
        lch = {"pmatrix": "(", "bmatrix": "[", "cases": "{"}.get(env, "")
        rch = {"pmatrix": ")", "bmatrix": "]"}.get(env, "")
        bsize = self._fit_size(max(lch, rch), body_h) if (lch or rch) else 0
        wl = (self.c.text_width(lch, bsize) if lch else (bar + 3)) if left else 0
        wr = (self.c.text_width(rch, bsize) if rch else (bar + 3)) if right else 0
        pad = max(2, int(size * 0.14))
        width = wl + wr + body_w + pad * 2
        above = body_h // 2 + int(size * 0.26)
        below = body_h - above + int(size * 0.06)
        return {"kind": "matrix", "cells": cells, "col_w": col_w, "row_h": row_h,
                "gap": gap, "body_w": body_w, "body_h": body_h, "env": env,
                "width": width, "above": above, "below": below,
                "bar": bar, "bsize": bsize, "wl": wl, "wr": wr, "pad": pad,
                "lch": lch, "rch": rch, "left": left, "right": right}

    def _fit_size(self, ch: str, target_h: int) -> int:
        """反推「让字符 ch 的**墨迹高度**约等于 target_h」所需的字号（实测比例）。"""
        if not ch:
            return max(9, target_h)
        probe = 40
        ink = self.c.glyph_ink_height(ch, probe)
        ratio = max(0.3, ink / float(probe))
        return max(9, int(target_h / ratio))

    # ---------------------------------------------------------------- 绘制
    def draw(self, box: dict, x: int, baseline: int) -> int:
        kind = box.get("kind")
        if kind == "text":
            self.c.draw_text(box["text"], x, baseline, box["size"])
            return x + box["width"]
        if kind == "atoms":
            for a in box["atoms"]:
                x = self.draw(a, x, baseline)
            return x
        if kind == "script":
            x = self.draw(box["base"], x, baseline)
            sup, sub_ = box["sup"], box["sub"]
            dx, raise_, drop = box["dx"], box["raise"], box["drop"]
            if sup and sub_:
                self.draw(sup, x + dx, baseline - raise_)
                self.draw(sub_, x + dx, baseline + drop)
                x += dx + max(sup["width"], sub_["width"])
            elif sup:
                self.draw(sup, x + dx, baseline - raise_)
                x += dx + sup["width"]
            elif sub_:
                self.draw(sub_, x + dx, baseline + drop)
                x += dx + sub_["width"]
            return x
        if kind == "bigop":
            # 大算符：上下限居中排在正上方/正下方（见 limits_box）
            base, sup, sub_ = box["base"], box["sup"], box["sub"]
            gap = box["gap"]
            asc = box.get("ink_asc", base["above"])
            desc = box.get("ink_desc", base["below"])
            self.draw(base, x + (box["width"] - base["width"]) // 2, baseline)
            if sup:
                self.draw(sup, x + (box["width"] - sup["width"]) // 2,
                          baseline - asc - gap - sup["below"])
            if sub_:
                self.draw(sub_, x + (box["width"] - sub_["width"]) // 2,
                          baseline + desc + gap + sub_["above"])
            return x + box["width"]
        if kind == "frac":
            num, den = box["num"], box["den"]
            bar_y = baseline - box["above"] + num["above"] + num["below"] + box["gap"]
            self.draw(num, x + (box["width"] - num["width"]) // 2,
                      bar_y - int(num["below"] * 0.30) - box["gap"])
            self.draw(den, x + (box["width"] - den["width"]) // 2,
                      bar_y + box["bar"] + box["gap"] + int(den["above"] * 0.80))
            self.c.hline(x, x + box["width"] - 1, bar_y, box["bar"])
            return x + box["width"]
        if kind == "sqrt":
            body = box["body"]
            top = baseline - box["above"]
            bottom = baseline + box["below"]
            bx = x + box["lead"]
            if box["degree"]:
                d = box["degree"]
                self.draw(d, x, top + d["above"] + max(0, int(box["bar"] * 2)))
            hook = box["hook"]
            mid_y = baseline + int((bottom - baseline) * 0.35)
            self.c.diag(bx, mid_y - int(hook * 0.55), bx + hook // 3, mid_y, int(box["bar"]))
            self.c.diag(bx + hook // 3, mid_y, bx + hook, top + box["bar"], int(box["bar"]))
            bar_x0 = bx + hook
            self.c.hline(bar_x0, x + box["width"] - 1, top + int(box["bar"]) - 1, box["bar"])
            self.draw(body, bar_x0 + box["hole"], baseline)
            return x + box["width"]
        if kind == "matrix":
            cells, col_w, row_h = box["cells"], box["col_w"], box["row_h"]
            top = baseline - box["above"]
            body_h, bar = box["body_h"], box["bar"]
            pad, wl = box["pad"], box["wl"]
            body_x = x + pad + wl
            y = top + max(0, (body_h - sum(row_h)) // 2)
            for r, row in enumerate(cells):
                cx = body_x
                for c, cell in enumerate(row):
                    self.draw(cell, cx + (col_w[c] - cell["width"]) // 2,
                              y + row_h[r] // 2 + (cell["above"] - cell["below"]) // 2)
                    cx += col_w[c] + box["gap"]
                y += row_h[r]

            def draw_bracket(bx: int, char: str, vertical: bool) -> None:
                if vertical:                       # 行列式的竖线
                    self.c.vline(bx, top + bar, top + body_h - bar, bar)
                else:                              # 真实括号字形：按主体高度放大并垂直居中
                    bsize = box["bsize"]
                    asc_ink, _desc_ink, ink_h = self.c.glyph_ink(char, bsize)
                    baseline_y = top + max(0, (body_h - ink_h) // 2) + asc_ink
                    self.c.draw_text(char, bx, baseline_y, bsize)

            if box["left"]:
                draw_bracket(x + pad, box["lch"], not box["lch"])
            if box["right"]:
                rx = x + pad + wl + box["body_w"] + pad
                draw_bracket(rx, box["rch"], not box["rch"])
            return x + box["width"]
        return x


_MATRIX_RE = re.compile(
    r"\\begin\{(?P<env>[pbvBV]?matrix|array|cases)\}(?P<body>.*?)\\end\{\1\}",
    re.S,
)


def _strip_matrix_env(src: str) -> str:
    """把矩阵环境替换成占位符，交给 tokenize 处理成 ("matrix", rows, env)。

    占位符用 \\x00…\\x01 包住，环境名与行之间用 \\x04 分隔，行分隔 \\x03、列分隔 \\x02
    （都是正文里不会出现的控制字符，避免与公式内容冲突）。
    """
    if "\\begin{" not in src:
        return src

    def repl(m: re.Match) -> str:
        env = m.group("env")
        rows = []
        for raw_row in re.split(r"\\\\", m.group("body")):
            row = raw_row.strip()
            if not row:
                continue
            rows.append("\x02".join(c.strip() for c in row.split("&")))
        if not rows:
            return ""
        return "\x00" + env + "\x04" + "\x03".join(rows) + "\x01"

    return _MATRIX_RE.sub(repl, src)


def render_rgba(latex: str, size_px: int = 17) -> Tuple[bytes, int, int, int]:
    """渲染 LaTeX，返回 ``(RGBA 字节, 宽, 高, 基线距顶部像素数)``。

    背景透明（alpha=0），字形颜色为白色 —— 调用方按需合成到自己的背景色。
    """
    if not available():
        raise RuntimeError("当前平台不支持公式渲染（需要 Windows GDI）")
    toks = Renderer.tokenize(_auto_space((latex or "").strip()))
    probe = _Canvas(_Dib(1, 1))
    try:
        box = Renderer(probe, size_px).layout(toks, size_px)
    finally:
        probe.dib.close()
    margin = max(2, int(size_px * 0.20))
    width = max(1, box["width"] + margin * 2)
    height = max(1, box["above"] + box["below"] + margin * 2)
    dib = _Dib(width, height)
    try:
        canvas = _Canvas(dib)
        Renderer(canvas, size_px).draw(box, margin, margin + box["above"])
        # GDI 只写 RGB 不写 alpha：白墨的亮度即覆盖率，这里把它搬到 alpha 通道
        buf = dib.buffer()
        out = bytearray(dib.size)
        for i in range(width * height):
            o = i * 4
            b, g, r = buf[o], buf[o + 1], buf[o + 2]
            a = r if r >= g and r >= b else (g if g >= b else b)
            if a:
                out[o] = out[o + 1] = out[o + 2] = 255
                out[o + 3] = a
        return bytes(out), width, height, margin + box["above"]
    finally:
        dib.close()


def render_image(latex: str, size_px: int = 17, color=(17, 24, 39), bg=(255, 255, 255),
                 supersample: int = 3):
    """渲染 LaTeX 成 :class:`~exerarchive.imageops.Image`（RGB，背景为 ``bg``）。

    返回 ``(image, 基线距顶部像素数)`` —— 基线用于在文本控件里对齐。

    **超采样（supersample）**：GDI 在小字号下的抗锯齿只有灰度一档，直接按 13~18px 渲染
    出来的公式会发虚（用户反馈"太小太糊"）。所以默认按 3 倍尺寸渲染，再高质量缩回目标尺寸 ——
    相当于每个像素取 9 个样本，笔画边缘明显更干净。
    """
    from .imageops import Image

    ss = max(1, int(supersample))
    rgba, w, h, baseline = render_rgba(latex, size_px * ss)

    r0, g0, b0 = _hex(color) if not isinstance(color, (tuple, list)) else tuple(color)[:3]
    br, bgc, bb = bg
    # 合成走查表 + 切片赋值（C 层），比逐像素 Python 循环快两个数量级：
    #   out = bg + (fg - bg) * a/255  只与 alpha 有关，所以可以预生成 256 项查表
    alphas = rgba[3::4]
    lut_r = bytes(br + (r0 - br) * a // 255 for a in range(256))
    lut_g = bytes(bgc + (g0 - bgc) * a // 255 for a in range(256))
    lut_b = bytes(bb + (b0 - bb) * a // 255 for a in range(256))
    n = w * h
    rgb = bytearray(n * 3)
    rgb[0::3] = alphas.translate(lut_r)
    rgb[1::3] = alphas.translate(lut_g)
    rgb[2::3] = alphas.translate(lut_b)
    img = Image(w, h, rgb)
    if ss > 1:
        tw = max(1, int(round(w / ss)))
        th = max(1, int(round(h / ss)))
        img = img.resize(tw, th)
        baseline = int(round(baseline / float(ss)))
    return img, baseline


if __name__ == "__main__":      # 手动跑一下，看看渲染效果
    import sys

    from .imageops import Image

    samples = [r"e^{2\pi i k/6}", r"x^{n-1}+x^{n-2}+\cdots+1", r"\frac{-b\pm\sqrt{b^2-4ac}}{2a}",
               r"\int_0^1 x^2\,dx=\frac{1}{3}", r"\sum_{i=1}^{n} a_i", r"\lim_{x\to 0}\frac{\sin x}{x}=1",
               r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}", r"D=\begin{vmatrix} 1 & 2 \\ 3 & 4 \end{vmatrix}=-2",
               r"\sqrt[3]{x+1}", r"\alpha\le\beta\Rightarrow\gamma"]
    imgs = []
    for s in samples:
        img, base = render_image(s, size_px=26)
        imgs.append(img)
        print(f"{s:52s} → {img.w}x{img.h} 基线={base}")
    W = max(i.w for i in imgs) + 20
    H = sum(i.h + 8 for i in imgs) + 10
    canvas = Image(W, H, bytearray(bytes((250, 250, 252)) * (W * H)))
    y = 5
    for img in imgs:
        for yy in range(img.h):
            src = yy * img.w * 3
            dst = ((y + yy) * W + 10) * 3
            canvas.data[dst:dst + img.w * 3] = img.data[src:src + img.w * 3]
        y += img.h + 8
    out = sys.argv[1] if len(sys.argv) > 1 else "verify/math-preview.png"
    canvas.save(out)
    print("预览已保存：", out)
