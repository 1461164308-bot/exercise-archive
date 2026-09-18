# -*- coding: utf-8 -*-
"""图片预处理面板：裁剪、纠偏、提亮、去水印，改完直接用于识别与归档。

交互设计（面向不懂图像处理的学生）：
- 左边一张大图，直接鼠标框选就能裁剪；切到「去水印」再框选就是抹掉那块区域；
- 右边一排按钮/滑块，**每点一下都能立刻在左边看到效果**（所见即所得）；
- 顶部有「一键自动优化」，把模糊、偏暗、拍歪的照片一次性处理到位；
- 处理的是原图，右下角「保存处理结果」会写出 ``data/preprocessed`` 并同步给识别流程。
"""

from __future__ import annotations

import hashlib
import os
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from .. import imageops, paths
from ..logging_setup import get_logger
from .widgets import COLORS, ImageCanvas, pick_font_family

log = get_logger("ui.editor")


class EditorPanel(ttk.Frame):
    """预处理面板（嵌入主窗口的「图片预处理」页）。"""

    def __init__(self, master, on_apply: Optional[Callable[[str, List[str]], None]] = None, **kw):
        super().__init__(master, **kw)
        self.on_apply = on_apply
        self.family = pick_font_family(self)
        self.src_path: Optional[str] = None
        self.image = None            # 当前全分辨率图（imageops.Image）
        self.steps: List[str] = []
        self._mode = tk.StringVar(value="crop")
        self._brightness = tk.DoubleVar(value=0)
        self._contrast = tk.DoubleVar(value=1.0)
        self._gamma = tk.DoubleVar(value=1.0)
        self._wm_mode = tk.StringVar(value="inpaint")
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)
        self.canvas = ImageCanvas(left, on_select=self._on_select, select_mode=True)
        self.canvas.pack(fill="both", expand=True)
        self.info = ttk.Label(left, text="从上面的「识别与归档」页选中一张图片，然后点「载入编辑器」",
                              style="Muted.TLabel")
        self.info.pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(self, width=300)
        right.pack(side="right", fill="y", padx=(4, 8), pady=8)
        right.pack_propagate(False)

        ttk.Label(right, text="一键优化", style="Title.TLabel").pack(anchor="w")
        ttk.Button(right, text="✨ 自动纠偏 / 提亮 / 锐化", command=self.auto_enhance).pack(
            fill="x", pady=(6, 2))
        ttk.Button(right, text="↺ 恢复原图", command=self.reset).pack(fill="x", pady=2)
        ttk.Separator(right).pack(fill="x", pady=10)

        ttk.Label(right, text="框选操作模式", style="Title.TLabel").pack(anchor="w")
        for text, value in (("✂ 裁剪（框选要保留的区域）", "crop"),
                            ("🧽 去水印（框选要抹掉的区域）", "watermark")):
            ttk.Radiobutton(right, text=text, value=value, variable=self._mode).pack(
                anchor="w", pady=2)
        wm_row = ttk.Frame(right)
        wm_row.pack(fill="x", pady=(2, 6))
        ttk.Label(wm_row, text="去水印方式：", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(wm_row, textvariable=self._wm_mode, width=8, state="readonly",
                     values=["inpaint", "median", "white"]).pack(side="left")
        ttk.Label(right, text="inpaint=四周插值 · median=平滑弱化 · white=纸白覆盖",
                  style="Muted.TLabel", wraplength=270).pack(anchor="w")
        ttk.Separator(right).pack(fill="x", pady=10)

        ttk.Label(right, text="旋转纠偏", style="Title.TLabel").pack(anchor="w")
        rot = ttk.Frame(right)
        rot.pack(fill="x", pady=4)
        ttk.Button(rot, text="↺ 左转 90°", command=lambda: self.rotate90(-1)).pack(side="left", expand=True, fill="x")
        ttk.Button(rot, text="↻ 右转 90°", command=lambda: self.rotate90(1)).pack(side="left", expand=True, fill="x")
        fine = ttk.Frame(right)
        fine.pack(fill="x", pady=2)
        ttk.Label(fine, text="微调角度", style="Muted.TLabel").pack(side="left")
        self.angle_var = tk.DoubleVar(value=0.0)
        spin = ttk.Spinbox(fine, from_=-15, to=15, increment=0.5, textvariable=self.angle_var, width=6)
        spin.pack(side="left", padx=4)
        ttk.Button(fine, text="应用", width=6, command=self.apply_angle).pack(side="left")
        ttk.Button(right, text="🔍 自动检测倾斜并纠正", command=self.auto_deskew).pack(fill="x", pady=(4, 2))
        ttk.Separator(right).pack(fill="x", pady=10)

        ttk.Label(right, text="明暗与清晰度", style="Title.TLabel").pack(anchor="w")
        self._slider(right, "亮度", self._brightness, -60, 60, self.preview)
        self._slider(right, "对比度", self._contrast, 0.6, 1.8, self.preview)
        self._slider(right, "伽马（提亮暗部）", self._gamma, 0.5, 2.0, self.preview)
        row = ttk.Frame(right)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="应用明暗", command=self.apply_adjust).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="重置明暗", command=self.reset_adjust).pack(side="left", expand=True, fill="x")
        ttk.Separator(right).pack(fill="x", pady=10)

        ttk.Label(right, text="质量检测", style="Title.TLabel").pack(anchor="w")
        self.quality_label = ttk.Label(right, text="（未载入图片）", style="Muted.TLabel", wraplength=270)
        self.quality_label.pack(anchor="w", pady=2)
        ttk.Button(right, text="重新检测", command=self.show_quality).pack(fill="x", pady=2)
        ttk.Separator(right).pack(fill="x", pady=10)

        ttk.Button(right, text="💾 保存处理结果（供识别/归档）", command=self.save_result).pack(
            fill="x", pady=2)
        ttk.Label(right, text="已执行的处理：", style="Muted.TLabel").pack(anchor="w", pady=(8, 2))
        self.steps_text = tk.Text(right, height=6, wrap="word", relief="flat",
                                  bg=COLORS["panel"], fg=COLORS["muted"], font=(self.family, 9))
        self.steps_text.pack(fill="x")
        self.steps_text.configure(state="disabled")

    def _slider(self, parent, label: str, var: tk.DoubleVar, lo: float, hi: float, cmd) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=label, width=14, style="Muted.TLabel").pack(side="left")
        val = ttk.Label(row, textvariable=var, width=5)
        val.pack(side="right")
        s = ttk.Scale(row, from_=lo, to=hi, variable=var, command=lambda e: cmd())
        s.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------ #
    # 载入 / 显示
    # ------------------------------------------------------------------ #
    def load(self, path: str) -> None:
        try:
            t0 = time.time()
            self.src_path = str(path)
            self.image = imageops.load(path)
            self.steps = []
            self._render()
            self.show_quality()
            self.info.configure(
                text=f"{os.path.basename(path)}　{self.image.w}×{self.image.h}　"
                     f"载入用时 {time.time() - t0:.2f}s"
            )
        except Exception as exc:
            messagebox.showerror("载入失败", f"无法打开图片：{exc}")

    def _render(self) -> None:
        if self.image is not None:
            self.canvas.set_image(self.image, max_side=int(self.master.winfo_width() or 900) or 900)
        self._refresh_steps()

    def _refresh_steps(self) -> None:
        self.steps_text.configure(state="normal")
        self.steps_text.delete("1.0", "end")
        self.steps_text.insert("end", "\n".join(f"{i}. {s}" for i, s in enumerate(self.steps, 1)) or "（无）")
        self.steps_text.configure(state="disabled")

    def show_quality(self) -> None:
        if self.image is None:
            return
        q = self.image.quality_score()
        parts = [f"综合 {q['score']:.2f}", f"清晰度 {q['sharpness']:.0f}", f"亮度 {q['brightness']:.0f}"]
        if q.get("need_cloud"):
            parts.append("建议云端复核")
        self.quality_label.configure(text="　".join(parts))

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _on_select(self, box: Tuple[int, int, int, int]) -> None:
        if self.image is None:
            return
        mode = self._mode.get()
        if mode == "crop":
            self.image = self.image.crop(box)
            self.steps.append(f"裁剪区域 {tuple(int(v) for v in box)}")
        else:
            self.image = imageops.remove_watermark(self.image, box, self._wm_mode.get())
            self.steps.append(f"去水印 {tuple(int(v) for v in box)}（{self._wm_mode.get()}）")
        self._render()
        self.show_quality()

    def rotate90(self, times: int) -> None:
        if self.image is None:
            return
        self.image = self.image.rotate90(times)
        self.steps.append("顺时针旋转 90°" if times > 0 else "逆时针旋转 90°")
        self._render()

    def apply_angle(self) -> None:
        if self.image is None:
            return
        angle = float(self.angle_var.get() or 0)
        if abs(angle) < 0.05:
            return
        self.image = self.image.rotate(angle)
        self.steps.append(f"旋转 {angle:+.2f}°")
        self._render()

    def auto_deskew(self) -> None:
        if self.image is None:
            return
        angle = self.image.detect_skew()
        if abs(angle) < 0.3:
            messagebox.showinfo("倾斜检测", "没有检测到明显倾斜（小于 0.3°），无需纠正。")
            return
        self.image = self.image.rotate(angle)
        self.steps.append(f"自动纠偏 {angle:+.2f}°")
        self._render()
        self.info.configure(text=f"已按检测到的 {angle:+.2f}° 纠正倾斜")

    def auto_enhance(self) -> None:
        if self.image is None:
            messagebox.showinfo("提示", "请先载入一张图片")
            return
        img, steps = imageops.auto_preprocess(self.image, {"deskew": True, "brighten": True,
                                                           "sharpen": True, "max_side": 2400})
        self.image = img
        self.steps.extend(steps or ["无需调整（图片质量已经不错）"])
        self._render()
        self.show_quality()

    def preview(self) -> None:
        """滑块拖动时只做「临时预览」，不写进处理步骤。"""
        if self.image is None:
            return
        base = getattr(self, "_base_for_adjust", None)
        if base is None:
            self._base_for_adjust = self.image.copy()
            base = self._base_for_adjust
        tmp = base.adjust(brightness=float(self._brightness.get()),
                          contrast=float(self._contrast.get()),
                          gamma=float(self._gamma.get()))
        self.canvas.set_image(tmp, max_side=int(self.master.winfo_width() or 900) or 900)

    def apply_adjust(self) -> None:
        if self.image is None:
            return
        b, c, g = float(self._brightness.get()), float(self._contrast.get()), float(self._gamma.get())
        self.image = self.image.adjust(brightness=b, contrast=c, gamma=g)
        self._base_for_adjust = None
        self.steps.append(f"明暗调整（亮度 {b:+.0f}，对比度 {c:.2f}，伽马 {g:.2f}）")
        self._brightness.set(0)
        self._contrast.set(1.0)
        self._gamma.set(1.0)
        self._render()
        self.show_quality()

    def reset_adjust(self) -> None:
        self._brightness.set(0)
        self._contrast.set(1.0)
        self._gamma.set(1.0)
        base = getattr(self, "_base_for_adjust", None)
        if base is not None:
            self.image = base
            self._base_for_adjust = None
            self._render()

    def reset(self) -> None:
        if self.src_path:
            self.load(self.src_path)
            self.info.configure(text=f"已恢复原图：{os.path.basename(self.src_path)}")

    # ------------------------------------------------------------------ #
    def save_result(self) -> Optional[str]:
        """把处理结果写到 data/preprocessed，并通知外部（用于识别/归档）。"""
        if self.image is None:
            messagebox.showinfo("提示", "请先载入一张图片")
            return None
        # 文件名带路径指纹：不同文件夹里的同名图片（IMG_0001.jpg）不会互相覆盖
        base = os.path.splitext(os.path.basename(self.src_path or "image"))[0]
        tag = hashlib.sha1(str(self.src_path).encode("utf-8")).hexdigest()[:6] if self.src_path else "nohash"
        out = paths.preprocessed_dir() / f"{base}_{tag}_edited.jpg"
        try:
            self.image.save(str(out), quality=94)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return None
        self.info.configure(text=f"已保存：{out}（{self.image.w}×{self.image.h}）")
        if self.on_apply:
            self.on_apply(str(out), list(self.steps))
        return str(out)
