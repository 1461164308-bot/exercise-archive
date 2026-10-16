"""Soft glass surfaces and small, cancellable Tk animations. Standard library only."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .widgets import COLORS


def blend(a, b, amount):
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join(f"{round(int(a[i:i+2], 16) * (1-amount) + int(b[i:i+2], 16) * amount):02x}"
                         for i in (0, 2, 4))


def rounded(canvas, x0, y0, x1, y1, radius=18, **kw):
    r = min(radius, (x1-x0)/2, (y1-y0)/2)
    return canvas.create_polygon(x0+r, y0, x1-r, y0, x1, y0, x1, y0+r,
                                 x1, y1-r, x1, y1, x1-r, y1, x0+r, y1,
                                 x0, y1, x0, y1-r, x0, y0+r, x0, y0,
                                 smooth=True, splinesteps=24, **kw)


class AmbientBackground(tk.Canvas):
    """Precomposited soft light; redraw only after resize, never an idle animation."""
    def __init__(self, master):
        super().__init__(master, bg=COLORS["bg"], highlightthickness=0)
        self._job = None
        self.bind("<Configure>", self._schedule)

    def _schedule(self, _event=None):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(90, self._draw)

    def _draw(self):
        self._job = None
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        for y in range(0, h, 4):
            self.create_rectangle(0, y, w, y+4, outline="",
                                  fill=blend("#e9edf7", "#eef5f4", y/max(1, h)))
        for cx, cy, radius, color in ((w*.78, 60, 410, "#d2cfee"),
                                       (w*.20, h*.75, 350, "#cfe6e1")):
            for step in range(32, 0, -1):
                r = radius*step/32
                self.create_oval(cx-r, cy-r, cx+r, cy+r, outline="",
                                 fill=blend(COLORS["bg"], color, (1-step/33)*.32))


class GlassCard(tk.Canvas):
    """Rounded frosted surface, fine highlight and soft shadow around real widgets."""
    def __init__(self, master, padding=18, tint=None, **kw):
        super().__init__(master, bg=COLORS["bg"], highlightthickness=0, bd=0, **kw)
        self.tint = tint or COLORS["panel"]
        self.padding = padding
        self.body = tk.Frame(self, bg=self.tint)
        self._window = self.create_window(padding, padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self._layout)

    def _layout(self, event):
        w, h = event.width, event.height
        self.delete("surface")
        rounded(self, 2, 5, w-2, h-1, 22, fill="#dde3ed", outline="", tags="surface")
        rounded(self, 1, 1, w-3, h-5, 22, fill=self.tint, outline="#ffffff", width=1,
                tags="surface")
        self.tag_lower("surface")
        self.coords(self._window, self.padding, self.padding)
        self.itemconfigure(self._window, width=max(1, w-self.padding*2),
                           height=max(1, h-self.padding*2-4))


class SoftButton(tk.Canvas):
    """Keyboard-operable rounded button with 140 ms hover and press feedback."""
    def __init__(self, master, text="", command=None, primary=False, width=120, height=38,
                 textvariable=None, state="normal", anchor="center", quiet=False, **kw):
        super().__init__(master, width=width, height=height, highlightthickness=0,
                         bg=master.cget("bg"), bd=0, takefocus=1, **kw)
        self.command, self.primary, self.anchor = command, primary, anchor
        self.quiet = quiet
        self._text, self._variable, self._state = text, textvariable, state
        self._active, self._hover, self._job, self._trace = False, False, None, None
        self._current = COLORS["primary"] if primary else COLORS["panel"]
        if textvariable is not None:
            self._trace = textvariable.trace_add("write", lambda *_: self._draw())
        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<Enter>", lambda _: self._target(True))
        self.bind("<Leave>", lambda _: self._target(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda _: self.invoke())
        self.bind("<space>", lambda _: self.invoke())
        self.bind("<FocusIn>", lambda _: self._draw())
        self.bind("<FocusOut>", lambda _: self._draw())
        self.bind("<Destroy>", self._dispose)
        self._draw()

    def _dispose(self, event):
        if event.widget is not self:
            return
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        if self._trace is not None:
            self._variable.trace_remove("write", self._trace)

    def configure(self, cnf=None, **kw):
        if cnf is not None:
            return super().configure(cnf, **kw)
        redraw = False
        for name in ("text", "state"):
            if name in kw:
                setattr(self, "_"+name, kw.pop(name))
                redraw = True
        result = super().configure(**kw) if kw else None
        if redraw:
            self._draw()
        return result

    config = configure

    def cget(self, key):
        if key in ("text", "state"):
            return getattr(self, "_"+key)
        return super().cget(key)

    def invoke(self):
        if self._state != "disabled" and self.command:
            return self.command()

    def set_active(self, active):
        self._active = active
        self._target(self._hover)

    def _press(self, _event):
        if self._state != "disabled":
            self.focus_set()
            self._current = COLORS["primary_dark"] if self.primary else "#dce2f5"
            self._draw()

    def _release(self, event):
        self._target(self._hover)
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    def _target(self, hover):
        self._hover = hover
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        target = (COLORS["primary_dark"] if hover else COLORS["primary"]) if self.primary else (
            "#e4e9fa" if self._active else "#edf0fa" if hover else COLORS["panel"])
        start = self._current
        animate = getattr(self.winfo_toplevel(), "animations_enabled", True)
        def tick(frame=1):
            self._job = None
            t = 1-(1-frame/9)**3 if animate else 1
            self._current = blend(start, target, t)
            self._draw()
            if animate and frame < 9:
                self._job = self.after(16, tick, frame+1)
        tick()

    def _draw(self):
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        disabled = self._state == "disabled"
        fill = "#edf0f4" if disabled else self._current
        edge = COLORS["primary"] if self.focus_get() == self else (fill if self.primary or self.quiet else "#e3e7f0")
        rounded(self, 1, 1, w-1, h-1, 12, fill=fill, outline=edge)
        fg = "#a5adbb" if disabled else "#ffffff" if self.primary else (
            COLORS["primary_dark"] if self._active else COLORS["text"])
        text = self._variable.get() if self._variable is not None else self._text
        self.create_text(16 if self.anchor == "w" else w/2, h/2, anchor=self.anchor,
                         text=text, fill=fg, font=(getattr(self.winfo_toplevel(), "family", ""), 10,
                                                  "bold" if self.primary or self._active else "normal"))
        self.configure(cursor="arrow" if disabled else "hand2")


class ScrollableFrame(ttk.Frame):
    """Scroll long forms locally; avoid global mouse-wheel bindings."""
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=COLORS["panel"], highlightthickness=0, width=300)
        self.scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # Bind only descendants of this form. Native Text/Combobox scrolling stays native.
        self.after_idle(self._bind_wheel)

    def _bind_wheel(self):
        def visit(widget):
            if not isinstance(widget, (tk.Text, ttk.Combobox)):
                widget.bind("<MouseWheel>", self._wheel, add="+")
                widget.bind("<Button-4>", lambda _: self.canvas.yview_scroll(-3, "units"), add="+")
                widget.bind("<Button-5>", lambda _: self.canvas.yview_scroll(3, "units"), add="+")
            for child in widget.winfo_children():
                visit(child)
        visit(self)

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta/120) or (-1 if event.delta > 0 else 1), "units")
        return "break"


class WelcomePanel(tk.Canvas):
    """Real empty state with one action and a small paper illustration.

    ``on_setup`` 可选：首次使用引导用。没装本地模型又没配云端时，多显示一行醒目提示
    和一个「去配置云端模型」按钮 —— 否则用户看到"服务未就绪"会以为软件坏了。
    """
    def __init__(self, master, on_import, on_setup=None):
        super().__init__(master, bg=COLORS["panel"], highlightthickness=0)
        self.hint_text = ""
        self.button = SoftButton(self, text="＋  导入习题图片", command=on_import, primary=True,
                                 width=184, height=44)
        self._button_id = self.create_window(0, 0, window=self.button)
        self.setup_button = None
        self._setup_id = None
        if on_setup is not None:
            self.setup_button = SoftButton(self, text="去配置云端模型", command=on_setup,
                                           width=184, height=38, quiet=True)
            self._setup_id = self.create_window(0, 0, window=self.setup_button)
        self.bind("<Configure>", self._draw)

    def set_hint(self, text: str) -> None:
        """设置/清除首次使用提示（空字符串=不显示，也不显示配置按钮）。"""
        text = text or ""
        if text == self.hint_text:
            return
        self.hint_text = text
        self._draw_safe()

    def _draw_safe(self) -> None:
        try:
            self._draw_full(self.winfo_width(), self.winfo_height())
        except Exception:
            pass

    def _draw(self, event=None):
        w = event.width if event is not None else self.winfo_width()
        h = event.height if event is not None else self.winfo_height()
        self._draw_full(w, h)

    def _draw_full(self, w, h):
        self.delete("art")
        cx, cy = w/2, max(98, h*.32)
        for i in range(18, 0, -1):
            r = 65+i*3
            self.create_oval(cx-r, cy-r*.70, cx+r, cy+r*.70, outline="",
                             fill=blend(COLORS["panel"], "#dddff5", (19-i)/36), tags="art")
        rounded(self, cx-51, cy-57, cx+44, cy+66, 12, fill="#e1e5f4", outline="", tags="art")
        rounded(self, cx-43, cy-68, cx+52, cy+56, 12, fill="#ffffff", outline="#d7dfef", tags="art")
        for yy, length in ((-36, 52), (-18, 61), (0, 43), (18, 54)):
            self.create_line(cx-23, cy+yy, cx-23+length, cy+yy, fill="#d1d9e9", width=3, capstyle="round", tags="art")
        self.create_oval(cx+18, cy+22, cx+63, cy+67, fill="#7385c7", outline="#ffffff", width=3, tags="art")
        self.create_line(cx+29, cy+44, cx+38, cy+52, cx+53, cy+36, fill="#ffffff", width=3,
                         capstyle="round", joinstyle="round", tags="art")
        family = getattr(self.winfo_toplevel(), "family", "")
        self.create_text(cx, cy+107, text="从一张习题照片开始", fill=COLORS["text"],
                         font=(family, 20, "bold"), tags="art")
        self.create_text(cx, cy+144, text="拍照导入，自动识别，轻松整理每一道错题。", fill=COLORS["muted"],
                         font=(family, 10), tags="art")
        if self.hint_text:
            # 首次使用引导：醒目颜色 + 自动换行，并把按钮整体下移，避免与提示重叠
            self.create_text(cx, cy+178, text=self.hint_text, fill=COLORS["primary_dark"],
                             font=(family, 10), width=max(280, w-140), justify="center", tags="art")
            hint_lines = 1 + len(self.hint_text) // 42
            base = cy + 178 + hint_lines * 20
            self.coords(self._button_id, cx, base + 34)
            if self._setup_id is not None:
                self.coords(self._setup_id, cx, base + 84)
            self.create_text(cx, base + 122, text="也可以安装 Ollama 用本地模型完全离线（见 docs/07）",
                             fill=COLORS["muted"], font=(family, 9), tags="art")
        else:
            self.coords(self._button_id, cx, cy+197)
            if self._setup_id is not None:
                self.itemconfigure(self._setup_id, state="hidden")
            self.create_text(cx, cy+242, text="支持 JPG / PNG 等图片 · 可一次选择多张", fill=COLORS["muted"],
                             font=(family, 9), tags="art")
        if self.hint_text and self._setup_id is not None:
            self.itemconfigure(self._setup_id, state="normal")
        if h > 560 and not self.hint_text:
            self.create_text(cx, h-48, text="01  导入图片       →       02  开始识别       →       03  整理与复习",
                             fill="#8693aa", font=(family, 10), tags="art")
        self.tag_lower("art")
