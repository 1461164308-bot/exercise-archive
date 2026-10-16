# -*- coding: utf-8 -*-
"""错题分析面板：显示/生成/导出单题报告，并支持手动修正。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .. import imageops
from ..analysis import report_to_text
from ..archive import STATUS_CN
from ..logging_setup import get_logger
from .widgets import COLORS, ImageCanvas, RichText

log = get_logger("ui.report")


class ReportPanel(ttk.Frame):
    """错题分析和原题图片对照；窄窗口默认收起图片，优先阅读正文。"""

    def __init__(self, master, on_generate: Optional[Callable[[str, bool], None]] = None,
                 on_export: Optional[Callable[[str, str], None]] = None,
                 on_open_image: Optional[Callable[[str], None]] = None, **kw):
        super().__init__(master, **kw)
        self.on_generate = on_generate
        self.on_export = on_export
        self.on_open_image = on_open_image
        self.image_path = ""
        self._image_visible = True
        self._image_user_choice = False
        self._image_signature = None
        self._layout_mode = None
        self.uid: str = ""
        self.question = None
        self.report = None
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.title = ttk.Label(top, text="未选择题目", style="Panel.TLabel")
        self.title.pack(side="left")
        self.hint = ttk.Label(top, text="从左侧题库选一道题，开始回顾。",
                              style="Panel.TLabel", foreground=COLORS["muted"])
        self.hint.configure(wraplength=420)
        self.hint.pack(anchor="w", pady=(6, 0))
        self.title.pack_configure(side="top", anchor="w")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8)
        self.btn_local = ttk.Button(btns, text="生成 / 更新分析", style="Primary.TButton",
                                    command=lambda: self._gen(False))
        self.btn_local.pack(side="left", pady=4)
        self.btn_cloud = ttk.Button(btns, text="云端深度分析",
                                    command=lambda: self._gen(True))
        self.btn_cloud.pack(side="left", padx=6, pady=4)
        more = ttk.Menubutton(btns, text="报告操作 ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="复制报告", command=self.copy_report)
        menu.add_command(label="导出本报告…", command=self.export_report)
        more['menu'] = menu
        more.pack(side="right")
        self.image_toggle = ttk.Button(btns, text="收起原图", command=self.toggle_image)
        self.image_toggle.pack(side="right", padx=6)

        self.content = ttk.Frame(self)
        self.content.pack(fill="both", expand=True, padx=8, pady=8)
        self.report_frame = ttk.Frame(self.content)
        self.report_split = ttk.Panedwindow(self.report_frame, orient="vertical")
        self.report_split.pack(fill="both", expand=True)
        question_frame = ttk.Frame(self.report_split)
        self.report_split.add(question_frame, weight=1)
        question_header = ttk.Frame(question_frame)
        question_header.pack(fill="x", pady=(0, 6))
        ttk.Label(question_header, text="识别题干与选项", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Button(question_header, text="复制题干", command=self.copy_question).pack(side="right")
        self.question_text = RichText(question_frame, height=6, size=11)
        self.question_text.text.configure(width=36)
        self.question_text.pack(fill="both", expand=True)
        self.question_text.write_blocks([("para", "选择一道题，查看识别到的题干与选项。")])
        analysis_frame = ttk.Frame(self.report_split)
        self.report_split.add(analysis_frame, weight=3)
        ttk.Label(analysis_frame, text="错题分析", font=("Microsoft YaHei UI", 11, "bold")).pack(
            anchor="w", pady=(10, 6))
        self.text = RichText(analysis_frame, height=18)
        self.text.text.configure(width=36)
        self.text.pack(fill="both", expand=True)
        self._last_text_height = 0
        self.report_split.bind("<Configure>", self._resize_text)
        self.image_frame = ttk.Frame(self.content, padding=10, width=260, height=200)
        self.image_frame.pack_propagate(False)
        image_header = ttk.Frame(self.image_frame)
        image_header.pack(fill="x", pady=(0, 8))
        ttk.Label(image_header, text="原题图片", style="Panel.TLabel").pack(side="left")
        self.image_open = ttk.Button(image_header, text="放大查看", command=self.open_image, state="disabled")
        self.image_open.pack(side="right")
        self.image_preview = ImageCanvas(self.image_frame, bg="#eef2f8")
        self.image_preview.canvas.configure(width=240, height=150)
        self.image_preview.pack(fill="both", expand=True)
        self.image_preview.canvas.bind("<Double-1>", lambda _: self.open_image())
        self.image_message = ttk.Label(self.image_preview, text="选择一道题，查看原题图片。",
                                        style="Muted.TLabel", wraplength=220, justify="center")
        self.image_message.place(relx=.5, rely=.5, anchor="center")
        self.image_info = ttk.Label(self.image_frame, text="图片随选中的题目切换", style="Muted.TLabel",
                                     wraplength=240)
        self.image_info.pack(anchor="w", pady=(8, 0))
        self.content.bind("<Configure>", self._layout_image)
        self._layout_image()

    def _resize_text(self, event):
        if event.height < 100 or abs(event.height - self._last_text_height) < 50:
            return
        self._last_text_height = event.height
        # Keep the analysis readable on laptop screens; the divider is still draggable.
        self.report_split.sashpos(0, min(160, max(90, int(event.height*.30))))

    def copy_question(self):
        if not self.uid:
            return
        self.clipboard_clear()
        self.clipboard_append(self.question_text.get_text())

    def _layout_image(self, _event=None):
        width = self.content.winfo_width()
        wide = width >= 650 or width <= 1
        if not self._image_user_choice and width > 1:
            self._image_visible = wide
        self.image_toggle.configure(text="收起原图" if self._image_visible else "查看原图")
        mode = (self._image_visible, wide)
        if mode == self._layout_mode:
            return
        self._layout_mode = mode
        self.report_frame.grid_forget()
        self.image_frame.grid_forget()
        for index in (0, 1):
            self.content.columnconfigure(index, weight=0, minsize=0)
            self.content.rowconfigure(index, weight=0, minsize=0)
        self.content.columnconfigure(0, weight=3)
        if not self._image_visible:
            self.content.rowconfigure(0, weight=1)
            self.report_frame.grid(row=0, column=0, sticky="nsew")
        else:
            self.image_frame.configure(width=250 if wide else 190)
            self.content.columnconfigure(1, weight=1)
            self.content.rowconfigure(0, weight=1)
            self.report_frame.grid(row=0, column=0, sticky="nsew")
            self.image_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

    def toggle_image(self):
        self._image_user_choice = True
        self._image_visible = not self._image_visible
        self.image_toggle.configure(text="收起原图" if self._image_visible else "查看原图")
        self._layout_image()

    def _show_image(self):
        candidates = [(self.question.get("image_path"), "原始照片"),
                      (self.question.get("work_path"), "预处理图片")]
        self.image_path = ""
        errors = []
        for path, label in candidates:
            if not path or not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
                signature = (str(path), stat.st_mtime_ns, stat.st_size)
                if signature != self._image_signature or self.image_preview._image is None:
                    self.image_preview.set_image(imageops.load(path), max_side=2000)
                self._image_signature = signature
                self.image_path = str(path)
                self.image_message.place_forget()
                self.image_open.configure(state="normal")
                img = self.image_preview._image
                self.image_info.configure(text=f"{label} {img.w}×{img.h} · 双击可放大\n{os.path.basename(path)}")
                return
            except Exception as exc:
                errors.append(str(exc))
        # Clear the previous question's image even when this image is missing or damaged.
        self._image_signature = None
        self.image_preview.clear()
        self.image_open.configure(state="disabled")
        self.image_info.configure(text="可继续查看分析报告")
        self.image_message.configure(text="图片无法读取，请检查原文件。" if errors else
                                     "未找到这道题的图片。\n原文件可能已移动或删除。" if self.uid else
                                     "选择一道题，查看原题图片。")
        self.image_message.place(relx=.5, rely=.5, anchor="center")

    def open_image(self):
        if not self.image_path:
            return
        if not os.path.isfile(self.image_path):
            self._show_image()
            return
        if self.on_open_image:
            self.on_open_image(self.image_path)
        else:
            # Standalone ReportPanel users still have a larger image view.
            window = tk.Toplevel(self)
            window.title(f"{self.uid} · 原题图片")
            window.geometry("900x720")
            preview = ImageCanvas(window)
            preview.pack(fill="both", expand=True)
            preview.set_image(self.image_preview._image, max_side=2000)

    # ------------------------------------------------------------------ #
    def show(self, question: dict, report: Optional[dict]) -> None:
        self.question = question or {}
        self.report = report
        self.uid = str(self.question.get("uid", ""))
        recognized = str(self.question.get("stem") or "").strip() or (
            "尚未识别到题干，可在「我的题库」中核对或补充。")
        options = self.question.get("options") or []
        if options:
            recognized += "\n" + "\n".join(str(item) for item in options)
        self.question_text.write_blocks([("para", recognized)])
        self._show_image()
        status_cn = STATUS_CN.get(str(self.question.get("status")), "未判定")
        self.title.configure(
            text=f"{self.uid}　{status_cn}　{self.question.get('subject', '')}"
                 f"/{self.question.get('qtype', '')}"
        )
        self.btn_local.configure(state="normal" if self.uid else "disabled")
        self.btn_cloud.configure(state="normal" if self.uid else "disabled")
        if report:
            # 别把内部代码直接给学生看：depth 是 rule/local/cloud，这里翻成中文并带上引擎名，
            # 选了「云端优先」的用户能一眼看出这份报告到底是谁生成的。
            depth = str(report.get("_depth", "") or report.get("depth", ""))
            depth_label = {"rule": "本地规则生成（未调用模型）", "local": "本地模型生成",
                           "cloud": "云端模型生成"}.get(depth, depth or "未知来源")
            engine = str(report.get("engine", "") or "").strip()
            self.hint.configure(text="报告来源：" + depth_label + (f"（{engine}）" if engine else ""))
            self.text.write_blocks([
                ("h1", f"{self.uid} 错题分析"),
                ("kv", f"学科：{self.question.get('subject', '')}（{self.question.get('subject_detail', '')}）　"
                       f"题型：{self.question.get('qtype', '')}　知识点："
                       f"{'、'.join(str(k) for k in (self.question.get('knowledge_points') or []))}"),
                ("h2", "错题还原"), ("para", report.get("restate", "")),
                ("h2", "错误原因分析"),
            ] + [("bullet", f"[{c.get('type', '')}] {c.get('detail', '')}")
                 if isinstance(c, dict) else ("bullet", str(c))
                 for c in (report.get("causes") or [])] + [
                ("h2", "对应知识点"),
            ] + [("bullet", f"{k.get('name', '')}（{k.get('textbook', '')}）：{k.get('note', '')}")
                 if isinstance(k, dict) else ("bullet", str(k))
                 for k in (report.get("knowledge_points") or [])] + [
                ("h2", "标准解题步骤"),
            ] + [("bullet", f"{i}. {s}") for i, s in enumerate(report.get("solution_steps") or [], 1)]
              + ([("ok", "最终答案：" + str(report.get("final_answer")))] if report.get("final_answer") else [])
              + [("h2", "避错技巧")] + [("bullet", str(t)) for t in (report.get("avoid_tips") or [])]
              + [("h2", "举一反三（变式练习）")]
              + [("para", f"变式{i}：{v.get('stem', '')}\n　　答案：{v.get('answer', '')}"
                          f"\n　　提示：{v.get('hint', '')}") if isinstance(v, dict) else ("para", str(v))
                 for i, v in enumerate(report.get("variants") or [], 1)])
        else:
            self.hint.configure(text="暂无分析报告，点上面的按钮生成")
            self.text.write_blocks([
                ("h1", f"{self.uid} 错题分析"),
                ("kv", "这道题还没有生成分析报告。"),
                ("para", "点击「生成 / 更新分析」即可离线生成：错题还原、错误原因、"
                         "知识点、标准解法、避错技巧与变式练习。"),
                ("para", "如果已经配置了云端 API Key，点「云端深度分析」会得到更细的归因和更贴合的变式题。"),
                ("h2", "题干"), ("para", self.question.get("stem", "") or "（无）"),
            ] + ([("h2", "我的作答"), ("para", str(self.question.get("student_answer", "")))]
                 if self.question.get("student_answer") else []))

    # ------------------------------------------------------------------ #
    def _gen(self, cloud: bool) -> None:
        if not self.uid:
            messagebox.showinfo("提示", "请先在左侧选中一道错题")
            return
        if self.on_generate:
            self.on_generate(self.uid, cloud)

    def set_busy(self, busy: bool, text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.btn_local.configure(state=state)
        self.btn_cloud.configure(state=state)
        if text:
            self.hint.configure(text=text)

    def copy_report(self) -> None:
        if not self.report:
            messagebox.showinfo("提示", "还没有报告可以复制")
            return
        text = report_to_text(self.question or {}, self.report)
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("已复制", "报告已复制到剪贴板，可直接粘贴到微信/Word/笔记里。")

    def export_report(self) -> None:
        if not self.report:
            messagebox.showinfo("提示", "还没有报告可以导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出错题分析",
            defaultextension=".md",
            initialfile=f"{self.uid}_错题分析.md",
            filetypes=[("Markdown", "*.md"), ("纯文本", "*.txt"), ("网页", "*.html")],
        )
        if not path:
            return
        text = report_to_text(self.question or {}, self.report)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if self.on_export:
            self.on_export(path, self.uid)
        messagebox.showinfo("导出完成", f"已保存到：\n{path}")
