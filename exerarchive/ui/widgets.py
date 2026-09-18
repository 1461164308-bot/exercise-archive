# -*- coding: utf-8 -*-
"""通用界面组件：图片画布、柱状/饼图、统计卡片、可滚动文本、标签编辑器。

全部基于 tkinter/ttk 与 Canvas 手绘，**不依赖 matplotlib 等第三方库**，
保证「解压即用、离线可用」。
"""

from __future__ import annotations

import base64
import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# 配色（浅色主题）
COLORS = {
    "bg": "#f5f6f8",
    "panel": "#ffffff",
    "border": "#dfe3ea",
    "text": "#1c1c1e",
    "muted": "#6b7280",
    "primary": "#2f6fed",
    "primary_dark": "#1f4fbf",
    "wrong": "#e5484d",
    "correct": "#2f9e44",
    "blank": "#868e96",
    "warn": "#f59f00",
    "series": ["#2f6fed", "#e5484d", "#2f9e44", "#f59f00", "#7048e8", "#0ca678", "#e8590c", "#1098ad"],
}


def pick_font_family(root: tk.Misc, prefer: str = "微软雅黑") -> str:
    """挑一个系统里确实存在的中文字体族。"""
    families = set(tkfont.families(root))
    for name in (prefer, "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "PingFang SC",
                 "Hiragino Sans GB", "Noto Sans CJK SC", "SimHei", "DengXian", "SimSun"):
        if name in families:
            return name
    return "TkDefaultFont"


def install_style(root: tk.Tk, family: str, size: int = 10) -> ttk.Style:
    """统一 ttk 主题与字体（比默认好看很多，也更适合中文）。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=COLORS["bg"])
    base = (family, size)
    for name in ("TLabel", "TButton", "TEntry", "TCombobox", "TCheckbutton", "TRadiobutton",
                 "TLabelframe.Label", "TNotebook.Tab", "TTreeview", "TTreeview.Heading"):
        try:
            style.configure(name, font=base)
        except tk.TclError:
            pass
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Panel.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
    style.configure("Title.TLabel", font=(family, size + 5, "bold"), background=COLORS["bg"])
    style.configure("Card.TLabel", font=(family, size + 8, "bold"), background=COLORS["panel"],
                    foreground=COLORS["primary"])
    style.configure("Primary.TButton", font=(family, size, "bold"))
    style.configure("Treeview", rowheight=int(size * 2.4), fieldbackground=COLORS["panel"],
                    background=COLORS["panel"])
    style.configure("Treeview.Heading", font=(family, size, "bold"))
    style.map("Treeview", background=[("selected", COLORS["primary"])],
              foreground=[("selected", "white")])
    style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(14, 7), font=base)
    style.map("TNotebook.Tab", background=[("selected", COLORS["panel"])])
    style.configure("TLabelframe", background=COLORS["bg"])
    style.configure("TLabelframe.Label", background=COLORS["bg"], foreground=COLORS["muted"])
    style.configure("Status.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
    style.configure("Horizontal.TProgressbar", troughcolor="#e9ecf1", background=COLORS["primary"])
    return style


# --------------------------------------------------------------------------- #
# 图片画布
# --------------------------------------------------------------------------- #
def make_photo(img) -> "tk.PhotoImage":
    """把 :class:`~exerarchive.imageops.Image` 变成 tkinter 可显示的 PhotoImage。

    这里踩过一个**很隐蔽的坑**：早期版本用 ``PhotoImage(data=base64(ppm))``，
    结果 Tk 直接报 ``TclError: couldn't recognize image data`` ——
    Tk 的 ``-data`` 选项**只对 GIF/PNG 这类自带魔数识别的格式支持 base64**，
    对 PPM 只认**原始二进制**。表现是：预览区永远空白、点「载入编辑器」弹
    「无法打开图片：couldn't recognize image data」。

    现在按「原始 PPM 字节 → PNG base64」的顺序尝试：
    - PPM 是零编码开销的直传（实测 900×1200 只要 6 毫秒）；
    - 万一某个 Tk 版本不认 PPM，就退回 PNG（GDI+ 内存编码，约 15 毫秒；非 Windows 用纯 Python 编码器）。
    """
    last_error = None
    try:
        return tk.PhotoImage(data=img.to_ppm())
    except Exception as exc:
        last_error = exc
    try:
        from .. import gdiplus

        if gdiplus.available():
            png = gdiplus.save_image_bytes(img.w, img.h, bytes(img.data), "png")
        else:
            from .. import imageops

            png = imageops.encode_png(img.w, img.h, bytes(img.data))
        return tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
    except Exception as exc:
        raise RuntimeError(f"无法把图片显示到界面（PPM 与 PNG 两种方式都失败）：{last_error} / {exc}")


class ImageCanvas(ttk.Frame):
    """显示图片并支持框选（裁剪 / 去水印 / 圈定题目区域都能用）。"""

    def __init__(self, master, on_select: Optional[Callable[[Tuple[int, int, int, int]], None]] = None,
                 select_mode: bool = False, bg: str = "#e9ecf1", **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._photo: Optional[tk.PhotoImage] = None
        self._image = None                 # imageops.Image（当前显示的原图，未缩放）
        self._scale = 1.0
        self._offset = (0, 0)
        self._rect_id = None
        self._drag_start = None
        self._select_enabled = select_mode
        self.on_select = on_select
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    # -- 显示 -------------------------------------------------------------- #
    def set_image(self, img, max_side: int = 1000, reset_view: bool = True) -> None:
        """显示 imageops.Image（内部按 max_side 缩放以省内存）。"""
        self._image = img
        if reset_view:
            self._view = img.scale_to_fit(max_side)
            self._scale = self._view.w / img.w if img.w else 1.0
            self._offset = (0, 0)
        self._render()

    def _render(self) -> None:
        if self._image is None:
            return
        view = getattr(self, "_view", None)
        if view is None:
            return
        self._photo = make_photo(view)
        self.canvas.delete("all")
        self._rect_id = None
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, view.w, view.h))
        if self._select_enabled:
            self.canvas.create_text(8, 8, anchor="nw", fill="#666", font=("", 9),
                                    text="拖拽可框选区域（裁剪 / 去水印）")

    def clear(self) -> None:
        self.canvas.delete("all")
        self._photo = None
        self._image = None
        self._view = None

    # -- 坐标换算 ---------------------------------------------------------- #
    def _to_image_coords(self, x: int, y: int) -> Tuple[int, int]:
        view = getattr(self, "_view", None)
        if view is None or self._scale <= 0:
            return x, y
        return int(x / self._scale), int(y / self._scale)

    def get_selection(self) -> Optional[Tuple[int, int, int, int]]:
        """返回上一次框选的区域（原图坐标系）。"""
        return self._last_selection if hasattr(self, "_last_selection") else None

    # -- 事件 -------------------------------------------------------------- #
    def _on_press(self, event):
        if not self._select_enabled:
            return
        self._drag_start = (event.x, event.y)
        if self._rect_id:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=COLORS["primary"], width=2, dash=(4, 2)
        )

    def _on_drag(self, event):
        if not self._select_enabled or not self._drag_start or not self._rect_id:
            return
        x0, y0 = self._drag_start
        self.canvas.coords(self._rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event):
        if not self._select_enabled or not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1, y1 = event.x, event.y
        self._drag_start = None
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[2] - box[0] < 6 or box[3] - box[1] < 6:
            return
        img_box = (
            self._to_image_coords(box[0], box[1])[0], self._to_image_coords(box[0], box[1])[1],
            self._to_image_coords(box[2], box[3])[0], self._to_image_coords(box[2], box[3])[1],
        )
        self._last_selection = img_box
        if self.on_select:
            self.on_select(img_box)


# --------------------------------------------------------------------------- #
# 图表（Canvas 手绘）
# --------------------------------------------------------------------------- #
class ChartCanvas(tk.Canvas):
    """轻量图表：饼图 / 横向柱状图。"""

    def __init__(self, master, width: int = 380, height: int = 220, bg: str = COLORS["panel"], **kw):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, **kw)

    def draw_pie(self, data: Dict[str, float], title: str = "") -> None:
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        items = [(k, float(v)) for k, v in (data or {}).items() if float(v) > 0]
        if not items:
            self.create_text(w / 2, h / 2, text="暂无数据", fill=COLORS["muted"])
            return
        total = sum(v for _, v in items) or 1.0
        size = min(w, h) - 60
        x0, y0 = 20, 30
        start = 90.0
        for i, (name, val) in enumerate(items):
            # Tk 的 create_arc 在 |extent| >= 360 时什么都不画，所以 100% 要夹到 359.9
            extent = -359.9 if len(items) == 1 else -360.0 * val / total
            color = COLORS["series"][i % len(COLORS["series"])]
            self.create_arc(x0, y0, x0 + size, y0 + size, start=start, extent=extent,
                            fill=color, outline="white", width=2)
            mid = math.radians(start + extent / 2)
            cx, cy = x0 + size / 2, y0 + size / 2
            r = size / 2 * 0.62
            if val / total > 0.06:
                self.create_text(cx + r * math.cos(mid), cy - r * math.sin(mid),
                                 text=f"{val / total * 100:.0f}%", fill="white",
                                 font=("", 9, "bold"))
            start += extent
        # 图例
        lx = x0 + size + 14
        ly = y0 + 6
        for i, (name, val) in enumerate(items[:8]):
            color = COLORS["series"][i % len(COLORS["series"])]
            self.create_rectangle(lx, ly, lx + 10, ly + 10, fill=color, outline="")
            self.create_text(lx + 16, ly + 5, anchor="w", font=("", 9),
                             text=f"{name} {int(val)}")
            ly += 18
        if title:
            self.create_text(8, 8, anchor="nw", text=title, font=("", 10, "bold"),
                             fill=COLORS["text"])

    def draw_bars(self, data: Dict[str, float], title: str = "", max_items: int = 10,
                  color: str = None, value_suffix: str = "") -> None:
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        items = [(str(k), float(v)) for k, v in (data or {}).items() if float(v) > 0]
        items.sort(key=lambda kv: -kv[1])
        items = items[:max_items]
        if not items:
            self.create_text(w / 2, h / 2, text="暂无数据", fill=COLORS["muted"])
            return
        top = 34 if title else 12
        if title:
            self.create_text(8, 8, anchor="nw", text=title, font=("", 10, "bold"), fill=COLORS["text"])
        # 条目少时不要把条形拉得又粗又高：限制单行高度并整体垂直居中
        row_h = min(44.0, max(16.0, (h - top - 10) / len(items)))
        block_h = row_h * len(items)
        start_y = top + max(0.0, (h - top - 10 - block_h) / 2.0)
        label_w = 128
        bar_max = max(20, w - label_w - 60)
        vmax = max(v for _, v in items) or 1.0
        for i, (name, val) in enumerate(items):
            y = start_y + i * row_h
            self.create_text(label_w - 8, y + row_h / 2, anchor="e", font=("", 9),
                             text=name[:14], fill=COLORS["text"])
            bw = bar_max * val / vmax
            self.create_rectangle(label_w, y + 3, label_w + bw, y + row_h - 3,
                                  fill=color or COLORS["series"][i % len(COLORS["series"])], outline="")
            self.create_text(label_w + bw + 6, y + row_h / 2, anchor="w", font=("", 9),
                             text=f"{int(val)}{value_suffix}", fill=COLORS["muted"])


class StatCard(ttk.Frame):
    """统计卡片：一个大数字 + 说明文字。"""

    def __init__(self, master, label: str, value: str = "0", color: str = None):
        super().__init__(master, style="Panel.TFrame", padding=(14, 10))
        self._label = label
        self.value_var = tk.StringVar(value=value)
        self.value_label = tk.Label(self, textvariable=self.value_var, bg=COLORS["panel"],
                                    fg=color or COLORS["primary"], font=("", 20, "bold"))
        self.value_label.pack(anchor="w")
        tk.Label(self, text=label, bg=COLORS["panel"], fg=COLORS["muted"],
                 font=("", 9)).pack(anchor="w")

    def set(self, value, color: str = None) -> None:
        self.value_var.set(str(value))
        if color:
            self.value_label.configure(fg=color)


# --------------------------------------------------------------------------- #
# 文本显示
# --------------------------------------------------------------------------- #
def make_math_photo(latex: str, size_px: int = 17, fg=(17, 24, 39), bg=(255, 255, 255),
                    line_ascent: int = 0, line_height: int = 0, supersample: int = 3):
    """把一段 LaTeX 渲染成能直接嵌进 ``tk.Text`` 的 PhotoImage。

    基线对齐是这里唯一的难点：Tk 8.6 的内嵌图片**没有** ``-offset`` 选项，
    ``align="baseline"`` 又是「图片底边贴基线」，所以带分母/下标的公式会整体浮高。
    这里用 ``align="center"`` + 上下留白来补偿：

    设行高 L、文字上高 A，公式图高 H、其基线距图顶 b。居中摆放时图片顶边在
    ``行顶 + (L-H)/2``，要让公式基线与文字基线重合，需要 ``H - 2b = L - 2A``。
    不满足就给图补边（补上边会同时抬高 b，补下边只增加 H），直到等式成立。

    字号由调用方按行高算好传入；渲染本身走 3 倍超采样（见 mathdraw.render_image），
    小字号下也不会发虚。
    """
    from .. import mathdraw

    img, baseline = mathdraw.render_image(latex, size_px=size_px, color=fg, bg=bg,
                                          supersample=supersample)
    if line_height > 0:
        target = line_height - 2 * line_ascent
        delta = (img.h - 2 * baseline) - target
        if delta > 0:
            img = img.pad(top=delta, color=bg)
        elif delta < 0:
            img = img.pad(bottom=-delta, color=bg)
    return make_photo(img)


def _hex_rgb(color: str) -> Tuple[int, int, int]:
    """``#rrggbb`` → (r, g, b)。"""
    c = str(color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return (17, 24, 39)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return (17, 24, 39)


class RichText(ttk.Frame):
    """带滚动条的只读文本区，支持简单的标题/正文样式标记。"""

    def __init__(self, master, height: int = 16, font_family: str = "微软雅黑", size: int = 10, **kw):
        super().__init__(master, **kw)
        self.text = tk.Text(self, wrap="word", height=height, relief="flat", bg=COLORS["panel"],
                            fg=COLORS["text"], padx=12, pady=10, spacing1=2, spacing3=4,
                            font=(font_family, size), highlightthickness=1,
                            highlightbackground=COLORS["border"])
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("h1", font=(font_family, size + 4, "bold"), foreground=COLORS["primary"],
                                spacing1=8, spacing3=6)
        self.text.tag_configure("h2", font=(font_family, size + 1, "bold"), spacing1=8, spacing3=4)
        self.text.tag_configure("kv", foreground=COLORS["muted"])
        self.text.tag_configure("warn", foreground=COLORS["wrong"])
        self.text.tag_configure("ok", foreground=COLORS["correct"])
        self.text.tag_configure("mono", font=("Consolas", size))
        self._photos: List[tk.PhotoImage] = []      # 内嵌公式图必须持有引用
        self._plain_parts: List[str] = []           # 原始文本（公式仍是 LaTeX 源码）
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def append(self, content: str, tag: str = "") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", content + "\n", tag)
        self.text.configure(state="disabled")

    # -- 公式支持 ---------------------------------------------------------- #
    def _font_metrics(self) -> Tuple[int, int]:
        """返回 (上高, 行高)，用于给公式图做基线补偿。"""
        try:
            fam = str(self.text.cget("font"))
        except Exception:
            fam = ""
        try:
            f = tkfont.Font(font=self.text.cget("font"))
            return int(f.metrics("ascent")), int(f.metrics("linespace"))
        except Exception:
            return 12, 20

    def insert_rich(self, content: str, tag: str = "") -> None:
        """插入文本，其中的数学公式会渲染成教科书式图片内嵌显示。

        识别规则见 :mod:`exerarchive.mathfmt`：``$…$`` 显式公式 + 裸公式
        （``x^2``、``\\frac{a}{b}``）都会转成图片；普通文字原样插入。
        """
        from .. import mathdraw, mathfmt

        if not content:
            return
        segments = mathfmt.to_segments(content)
        if not mathdraw.available() or all(k == "text" for k, _ in segments):
            self.text.insert("end", mathfmt.format_for_export(content) if not mathdraw.available()
                             else content, tag)
            return
        ascent, linespace = self._font_metrics()
        # 公式比正文略大一点更像课本（数学字符视觉重量小），倍率与超采样可在设置里调
        scale, ss = 1.15, 3
        try:
            from .. import config as _cfg

            _c = _cfg.load_config()
            scale = float(_c.get("ui.math_scale", 1.15) or 1.15)
            ss = int(_c.get("ui.math_supersample", 3) or 3)
        except Exception:
            pass
        size = max(15, int(round(linespace * max(0.7, min(2.0, scale)))))
        for kind, value in segments:
            if kind == "text":
                self.text.insert("end", value, tag)
                continue
            try:
                photo = make_math_photo(value, size_px=size, fg=self._fg(),
                                        bg=self._bg(), line_ascent=ascent, line_height=linespace,
                                        supersample=ss)
            except Exception:
                # 渲染失败也不能丢内容：退回纯文本写法
                self.text.insert("end", mathfmt.math_to_plain(value), tag)
                continue
            self._photos.append(photo)          # 必须持有引用，否则被回收变空白
            self.text.image_create("end", image=photo, align="center", padx=1)

    def _fg(self) -> Tuple[int, int, int]:
        try:
            return _hex_rgb(str(self.text.cget("foreground")) or COLORS["text"])
        except Exception:
            return (17, 24, 39)

    def _bg(self) -> Tuple[int, int, int]:
        try:
            return _hex_rgb(str(self.text.cget("background")) or COLORS["panel"])
        except Exception:
            return (255, 255, 255)

    def write_blocks(self, blocks: Sequence[Tuple[str, str]]) -> None:
        """写入 [(kind, text)]，kind: h1/h2/para/kv/bullet/ok/warn。

        数学公式会自动排版成图片（见 :meth:`insert_rich`）。
        """
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._photos.clear()
        self._plain_parts.clear()
        for kind, text in blocks:
            self._plain_parts.append(text)
            if kind == "h1":
                self.text.insert("end", text + "\n", "h1")
            elif kind == "h2":
                self.text.insert("end", "◆ " + text + "\n", "h2")
            elif kind == "kv":
                self.insert_rich(text, "kv")
                self.text.insert("end", "\n", "kv")
            elif kind == "bullet":
                self.text.insert("end", "  · ")
                self.insert_rich(text)
                self.text.insert("end", "\n")
            elif kind == "ok":
                self.insert_rich(text, "ok")
                self.text.insert("end", "\n", "ok")
            elif kind == "warn":
                self.insert_rich(text, "warn")
                self.text.insert("end", "\n", "warn")
            else:
                self.insert_rich(text)
                self.text.insert("end", "\n")
            self.text.insert("end", "\n")
        self.text.configure(state="disabled")
        self.text.see("1.0")

    def get_text(self) -> str:
        """取完整文本（**包含公式**，公式转成可读的纯文本写法）。

        注意不能直接读控件内容：公式是以图片形式嵌进去的，控件里没有对应文字，
        直接读会把公式整段丢掉。所以这里用写入时留存的原文再转纯文本。
        """
        from .. import mathfmt

        if self._plain_parts:
            return mathfmt.format_for_export("\n".join(self._plain_parts)).strip()
        return mathfmt.format_for_export(self.text.get("1.0", "end")).strip()


# --------------------------------------------------------------------------- #
# 标签编辑器
# --------------------------------------------------------------------------- #
class TagEditor(ttk.Frame):
    """自定义标签编辑：输入回车即添加，点击标签可删除。"""

    def __init__(self, master, on_change: Optional[Callable[[List[str]], None]] = None, **kw):
        super().__init__(master, **kw)
        self.tags: List[str] = []
        self.on_change = on_change
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.entry = ttk.Entry(row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.add(self.entry.get()))
        ttk.Button(row, text="添加", width=6, command=lambda: self.add(self.entry.get())).pack(
            side="left", padx=(4, 0))
        self.chips = ttk.Frame(self)
        self.chips.pack(fill="x", pady=(4, 0))

    def set(self, tags: Sequence[str]) -> None:
        self.tags = [str(t) for t in tags or []]
        self._render()

    def add(self, tag: str) -> None:
        tag = (tag or "").strip()
        self.entry.delete(0, "end")
        if not tag or tag in self.tags:
            return
        self.tags.append(tag)
        self._render()
        if self.on_change:
            self.on_change(list(self.tags))

    def remove(self, tag: str) -> None:
        if tag in self.tags:
            self.tags.remove(tag)
            self._render()
            if self.on_change:
                self.on_change(list(self.tags))

    def _render(self) -> None:
        for child in self.chips.winfo_children():
            child.destroy()
        for tag in self.tags:
            chip = tk.Label(self.chips, text=f"{tag} ✕", bg="#e8efff", fg=COLORS["primary_dark"],
                            padx=8, pady=2, cursor="hand2", font=("", 9))
            chip.pack(side="left", padx=(0, 6), pady=2)
            chip.bind("<Button-1>", lambda e, t=tag: self.remove(t))


# --------------------------------------------------------------------------- #
# 状态栏
# --------------------------------------------------------------------------- #
class StatusBar(ttk.Frame):
    """底部状态栏：提示文字 + 进度条 + 进度文字。"""

    def __init__(self, master, **kw):
        super().__init__(master, style="Panel.TFrame", padding=(10, 6), **kw)
        self.msg = tk.StringVar(value="就绪")
        self.detail = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.msg, style="Status.TLabel").pack(side="left")
        ttk.Label(self, textvariable=self.detail, style="Status.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100, length=180)
        self.progress.pack(side="right", padx=10)

    def set(self, message: str, detail: str = "") -> None:
        self.msg.set(message)
        if detail:
            self.detail.set(detail)

    def set_progress(self, done: int, total: int, message: str = "") -> None:
        total = max(1, total)
        self.progress.configure(value=min(100, done * 100.0 / total))
        if message:
            self.msg.set(message)
        self.detail.set(f"{done}/{total}")
