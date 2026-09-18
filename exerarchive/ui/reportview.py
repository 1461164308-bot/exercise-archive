# -*- coding: utf-8 -*-
"""错题分析面板：显示/生成/导出单题报告，并支持手动修正。"""

from __future__ import annotations

import os
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from ..analysis import report_to_text
from ..archive import STATUS_CN
from ..logging_setup import get_logger
from .widgets import COLORS, RichText

log = get_logger("ui.report")


class ReportPanel(ttk.Frame):
    """错题分析页：左边题目信息，右边报告正文。"""

    def __init__(self, master, on_generate: Optional[Callable[[str, bool], None]] = None,
                 on_export: Optional[Callable[[str, str], None]] = None, **kw):
        super().__init__(master, **kw)
        self.on_generate = on_generate
        self.on_export = on_export
        self.uid: str = ""
        self.question = None
        self.report = None
        self._build()

    def _build(self) -> None:
        top = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.title = ttk.Label(top, text="未选择题目", style="Panel.TLabel")
        self.title.pack(side="left")
        self.hint = ttk.Label(top, text="在「识别与归档」页选中一道错题，这里会显示它的分析报告",
                              style="Panel.TLabel", foreground=COLORS["muted"])
        self.hint.pack(side="left", padx=12)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8)
        self.btn_local = ttk.Button(btns, text="🧠 生成/更新分析（本地模型）",
                                    command=lambda: self._gen(False))
        self.btn_local.pack(side="left", pady=4)
        self.btn_cloud = ttk.Button(btns, text="☁ 云端深度分析",
                                    command=lambda: self._gen(True))
        self.btn_cloud.pack(side="left", padx=6, pady=4)
        ttk.Button(btns, text="📋 复制报告", command=self.copy_report).pack(side="left", padx=6)
        ttk.Button(btns, text="📄 导出本报告", command=self.export_report).pack(side="left", padx=6)

        self.text = RichText(self, height=28)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)

    # ------------------------------------------------------------------ #
    def show(self, question: dict, report: Optional[dict]) -> None:
        self.question = question or {}
        self.report = report
        self.uid = str(self.question.get("uid", ""))
        status_cn = STATUS_CN.get(str(self.question.get("status")), "未判定")
        self.title.configure(
            text=f"{self.uid}　{status_cn}　{self.question.get('subject', '')}"
                 f"/{self.question.get('qtype', '')}"
        )
        self.btn_local.configure(state="normal" if self.uid else "disabled")
        self.btn_cloud.configure(state="normal" if self.uid else "disabled")
        if report:
            self.hint.configure(text=f"报告来源：{report.get('_depth', '') or report.get('depth', '')}")
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
                ("para", "点击「生成/更新分析（本地模型）」即可离线生成：错题还原、错误原因、"
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
