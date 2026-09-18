# -*- coding: utf-8 -*-
"""主窗口：上传区 / 预览区 / 识别结果区 / 错题分析区 / 归档目录区 / 搜索区 / 统计区。

界面逻辑（一屏走完全流程）::

    导入图片 → 自动预处理 → 本地模型识别 →（不确定就云端复核）→ 自动归档
        → 错题自动生成分析报告 → 搜索 / 统计 / 一键导出

线程模型：
- 识别、分析、导出都在后台线程跑，避免界面卡死；
- 后台线程只往 ``queue`` 里塞消息，主线程用 ``after(120)`` 消费并更新界面
  （tkinter 不是线程安全的，所有控件操作必须回主线程）。
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from .. import config, imageops, logging_setup, paths, prompts
from ..analysis import Analyzer, question_to_text
from ..archive import Archiver, STATUS_CN
from ..exporter import EXPORT_FORMATS, Exporter
from ..pipeline import Pipeline
from ..store import Store
from ..vlm import ModelClient
from .editor import EditorPanel
from .reportview import ReportPanel
from .settingsdlg import SettingsDialog
from .widgets import (COLORS, ChartCanvas, ImageCanvas, RichText, StatCard, StatusBar,
                      TagEditor, install_style, pick_font_family)

log = logging_setup.get_logger("ui.app")

# 题库页「知识点」筛选下拉框里的“全部”选项（它对应的真实筛选值是空字符串）
KP_ALL = "全部知识点"


def pick_recognition_source(item: Dict) -> tuple:
    """决定这张图识别时用哪个文件、要不要跳过自动预处理。

    - 用户在「图片预处理」页点过「保存处理结果」→ ``item["manual_edit"]`` 为真，
      识别就用那张手工调好的图（``skip_preprocess=True``），否则手工纠正会被
      自动预处理覆盖掉；
    - 没手工调过 → 用导入的原图，走正常的自动预处理。
    返回 ``(路径, 是否跳过预处理)``。抽成独立函数是为了能被单元测试直接覆盖。
    """
    work = str(item.get("work_path") or "")
    if item.get("manual_edit") and work and os.path.exists(work):
        return work, True
    return str(item.get("path") or ""), False


class MainWindow(tk.Tk):
    """应用主窗口。"""

    def __init__(self):
        super().__init__()
        logging_setup.setup()
        self.cfg = config.load_config()
        config.bootstrap_local_model(self.cfg)

        self.title(f"{paths.APP_NAME} v{paths.APP_VERSION}　—　本地离线识别 + 云端增强")
        self.geometry(self._initial_geometry())
        self.minsize(1100, 700)

        self.store = Store(encrypt=bool(self.cfg.get("privacy.encrypt_db", True)))
        self.pipeline = Pipeline(self.cfg, on_log=self._log_threadsafe, on_stage=self._stage_threadsafe)
        self.analyzer = Analyzer(self.cfg, on_log=self._log_threadsafe)
        self.archiver = Archiver(self.cfg, self.store)
        self.exporter = Exporter(self.cfg, self.store)

        self.family = pick_font_family(self, str(self.cfg.get("ui.font_family", "微软雅黑")))
        install_style(self, self.family, int(self.cfg.get("ui.font_size", 10)))

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.pending: List[Dict] = []          # 待识别图片 [{uid, path, name, status}]
        self.busy = False
        self.stop_flag = False
        self.current_image_uid = ""
        self.current_question_uid = ""

        self._build()
        logging_setup.add_listener(self._log_listener)
        self.after(120, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_all()
        self._startup_message()

    # ------------------------------------------------------------------ #
    def _initial_geometry(self) -> str:
        saved = str(self.cfg.get("ui.window", "") or "")
        if saved and "x" in saved:
            return saved
        w, h = 1440, 900
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        return f"{w}x{h}+{x}+{y}"

    def _startup_message(self) -> None:
        mode = self.cfg.mode_label()
        self.status.set(f"就绪　模式：{mode}", f"本地模型 {self.cfg.get('local.model')}")
        self.log(f"数据目录：{paths.data_dir()}")
        self.log(f"运行模式：{mode}　云端增强：{'开启' if self.cfg.cloud_enabled() else '关闭'}")
        if self.cfg.get("local.auto_start", True):
            threading.Thread(target=self._ensure_model_async, daemon=True).start()

    def _ensure_model_async(self) -> None:
        try:
            ok = ModelClient("local", self.cfg).manager.ensure_running(on_log=self._log_threadsafe)
            self._post(("status", "本地模型服务已就绪" if ok else "本地模型服务未就绪（可在设置里检查）"))
        except Exception as exc:
            self._log_threadsafe(f"启动本地模型服务失败：{exc}")

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self._build_header()
        self._build_toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.paned = ttk.Panedwindow(body, orient="horizontal")
        self.paned.pack(fill="both", expand=True)
        self._build_left(self.paned)
        self._build_center(self.paned)
        self.status = StatusBar(self)
        self.status.pack(fill="x", side="bottom")

    def _build_header(self) -> None:
        head = ttk.Frame(self, style="Panel.TFrame", padding=(12, 10))
        head.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(head, text=paths.APP_NAME, style="Title.TLabel").pack(side="left")
        self.mode_label = ttk.Label(head, text="", style="Status.TLabel")
        self.mode_label.pack(side="left", padx=12)
        self.cloud_btn_text = tk.StringVar()
        ttk.Button(head, textvariable=self.cloud_btn_text, width=16,
                   command=self.toggle_cloud).pack(side="left")
        ttk.Button(head, text="环境自检", command=self.run_selfcheck).pack(side="right", padx=4)
        ttk.Button(head, text="设置", command=self.open_settings).pack(side="right", padx=4)
        more = ttk.Menubutton(head, text="更多 ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="打开数据目录", command=lambda: self._open(str(paths.data_dir())))
        menu.add_command(label="打开归档目录", command=lambda: self._open(str(self.archiver.root())))
        menu.add_command(label="查看运行日志文件", command=self._open_log_file)
        menu.add_separator()
        menu.add_command(label="重建归档索引", command=self.rebuild_index)
        menu.add_command(label="导出全部数据 (JSON)", command=self.export_json_all)
        menu.add_separator()
        menu.add_command(label="备份数据（打包 zip）", command=self.backup_data)
        menu.add_command(label="从备份恢复…", command=self.restore_data)
        menu.add_separator()
        menu.add_command(label="清空全部数据…", command=self.clear_data)
        menu.add_separator()
        menu.add_command(label="使用说明（打开文档）", command=self.open_docs)
        menu.add_command(label="关于", command=self.show_about)
        more["menu"] = menu
        more.pack(side="right", padx=4)
        self._refresh_mode_labels()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(bar, text="📁 导入图片", command=self.import_images).pack(side="left")
        ttk.Button(bar, text="📂 导入文件夹", command=self.import_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="🗑 移除选中", command=self.remove_selected_pending).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_recognize = ttk.Button(bar, text="▶ 开始识别", command=self.start_recognition)
        self.btn_recognize.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■ 停止", command=self.stop_recognition, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_analyze = ttk.Button(bar, text="🧠 生成错题分析", command=lambda: self.analyze_batch(False))
        self.btn_analyze.pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        export = ttk.Menubutton(bar, text="⬇ 导出 ▾")
        emenu = tk.Menu(export, tearoff=False)
        emenu.add_command(label="导出选中题目…", command=lambda: self.export_dialog("selection"))
        emenu.add_command(label="导出全部错题（个人错题本）…", command=lambda: self.export_dialog("wrong"))
        emenu.add_command(label="导出知识点薄弱清单…", command=lambda: self.export_dialog("weak"))
        emenu.add_command(label="导出错题汇总…", command=lambda: self.export_dialog("summary"))
        emenu.add_command(label="导出全部题目…", command=lambda: self.export_dialog("all"))
        export["menu"] = emenu
        export.pack(side="left")
        ttk.Button(bar, text="🗂 归档选中", command=self.archive_selected).pack(side="left", padx=4)
        self.busy_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.busy_label.pack(side="right")

    def _build_left(self, parent) -> None:
        frame = ttk.Frame(parent, width=420)
        parent.add(frame, weight=1)
        nb = ttk.Notebook(frame)
        nb.pack(fill="both", expand=True)
        self.left_nb = nb          # 供冒烟测试/脚本切页（题库页的截图要用它）

        # 待处理
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text="待识别图片")
        cols = ("file", "status", "count", "quality")
        self.pending_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("file", "图片", 210), ("status", "状态", 80), ("count", "题数", 50),
                        ("quality", "质量", 60)):
            self.pending_tree.heading(c, text=t)
            self.pending_tree.column(c, width=w, anchor="w")
        self.pending_tree.pack(fill="both", expand=True)
        self.pending_tree.bind("<<TreeviewSelect>>", lambda e: self.on_pending_select())
        self.pending_tree.bind("<Double-1>", lambda e: self.start_recognition(only_selected=True))
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="载入编辑器", command=self.load_into_editor).pack(side="left")
        ttk.Button(row, text="本地重识别", command=lambda: self.recognize_selected(force="local")).pack(
            side="left", padx=4)
        ttk.Button(row, text="云端重识别", command=lambda: self.recognize_selected(force="cloud")).pack(
            side="left")

        # 题目列表（可按状态/学科/知识点过滤）
        tab2 = ttk.Frame(nb, padding=6)
        nb.add(tab2, text="题库")
        filt = ttk.Frame(tab2)
        filt.pack(fill="x", pady=(0, 4))
        self.filter_status = tk.StringVar(value="")
        self.filter_subject = tk.StringVar(value="")
        self.filter_knowledge = tk.StringVar(value=KP_ALL)
        self._kp_map: Dict[str, str] = {KP_ALL: ""}     # 下拉显示（"复数（3）"）→ 真正的知识点名
        ttk.Label(filt, text="状态").pack(side="left")
        cb = ttk.Combobox(filt, textvariable=self.filter_status, width=7, state="readonly",
                          values=["", "wrong", "correct", "blank"])
        cb.pack(side="left", padx=(2, 6))
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_questions())
        ttk.Label(filt, text="学科").pack(side="left")
        self.subject_combo = ttk.Combobox(filt, textvariable=self.filter_subject, width=6, state="readonly",
                                          values=[""] + prompts.SUBJECTS)
        self.subject_combo.pack(side="left", padx=(2, 6))
        self.subject_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_questions())
        # 知识点筛选：识别时模型给出的知识点（如"复数"）会作为分类维度出现在这里
        ttk.Label(filt, text="知识点").pack(side="left")
        self.kp_combo = ttk.Combobox(filt, textvariable=self.filter_knowledge, width=12,
                                     state="readonly", values=[KP_ALL])
        self.kp_combo.pack(side="left", padx=2)
        self.kp_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_questions())
        cols = ("uid", "subject", "kp", "qtype", "status", "stem")
        self.q_tree = ttk.Treeview(tab2, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("uid", "题号", 92), ("subject", "学科", 44), ("kp", "知识点", 116),
                        ("qtype", "题型", 52), ("status", "状态", 46), ("stem", "题干", 130)):
            self.q_tree.heading(c, text=t)
            self.q_tree.column(c, width=w, anchor="w")
        self.q_tree.pack(fill="both", expand=True)
        self.q_tree.bind("<<TreeviewSelect>>", lambda e: self.on_question_select())
        self.q_tree.tag_configure("wrong", foreground=COLORS["wrong"])
        self.q_tree.tag_configure("correct", foreground=COLORS["correct"])
        self.q_tree.tag_configure("blank", foreground=COLORS["muted"])
        row = ttk.Frame(tab2)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="查看/分析", command=self.on_question_select).pack(side="left")
        ttk.Button(row, text="删除选中题", command=self.delete_selected_questions).pack(side="left", padx=4)

        # 归档目录树
        tab3 = ttk.Frame(nb, padding=6)
        nb.add(tab3, text="归档目录")
        self.archive_tree = ttk.Treeview(tab3, show="tree")
        self.archive_tree.pack(fill="both", expand=True)
        ttk.Button(tab3, text="刷新目录树", command=self.refresh_archive_tree).pack(pady=4)

    def _build_center(self, parent) -> None:
        nb = ttk.Notebook(parent)
        parent.add(nb, weight=3)
        self.nb = nb

        # --- 识别与结果 ---
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text="识别与归档")
        inner = ttk.Panedwindow(tab, orient="horizontal")
        inner.pack(fill="both", expand=True)
        left = ttk.Frame(inner)
        inner.add(left, weight=3)
        self.preview = ImageCanvas(left)
        self.preview.pack(fill="both", expand=True)
        self.preview_info = ttk.Label(left, text="导入图片后在这里预览（滚轮/按钮可切换）",
                                      style="Muted.TLabel")
        self.preview_info.pack(anchor="w", pady=(4, 0))
        nav = ttk.Frame(left)
        nav.pack(fill="x", pady=2)
        ttk.Button(nav, text="上一张", command=lambda: self.step_pending(-1)).pack(side="left")
        ttk.Button(nav, text="下一张", command=lambda: self.step_pending(1)).pack(side="left", padx=4)
        ttk.Button(nav, text="重新识别本张", command=lambda: self.recognize_selected()).pack(side="left", padx=4)
        ttk.Button(nav, text="载入预处理", command=self.load_into_editor).pack(side="left", padx=4)

        right = ttk.Frame(inner)
        inner.add(right, weight=2)
        self.result_box = ttk.Frame(right)
        self.result_box.pack(fill="both", expand=True)
        self._build_result_editor(self.result_box)

        # --- 预处理 ---
        self.editor = EditorPanel(nb, on_apply=self.on_editor_applied)
        nb.add(self.editor, text="图片预处理")

        # --- 错题分析 ---
        self.report = ReportPanel(nb, on_generate=self.generate_report)
        nb.add(self.report, text="错题分析")

        # --- 搜索 ---
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="全局搜索")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.search_var, width=40)
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self.do_search())
        ttk.Button(bar, text="搜索", command=self.do_search).pack(side="left", padx=4)
        ttk.Label(bar, text="（可搜题干、作答、答案、知识点、错题报告内容）",
                  style="Muted.TLabel").pack(side="left", padx=6)
        self.search_status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.search_status_var, style="Muted.TLabel").pack(side="right")
        cols = ("uid", "subject", "qtype", "status", "kp", "stem")
        self.search_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("uid", "题号", 110), ("subject", "学科", 60), ("qtype", "题型", 70),
                        ("status", "状态", 60), ("kp", "知识点", 140), ("stem", "题干", 420)):
            self.search_tree.heading(c, text=t)
            self.search_tree.column(c, width=w, anchor="w")
        self.search_tree.pack(fill="both", expand=True, pady=6)
        self.search_tree.bind("<<TreeviewSelect>>", lambda e: self.on_search_select())
        self.search_text = RichText(tab, height=10)
        self.search_text.pack(fill="both", expand=False)

        # --- 统计 ---
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="统计看板")
        cards = ttk.Frame(tab)
        cards.pack(fill="x")
        self.card_images = StatCard(cards, "图片总数")
        self.card_questions = StatCard(cards, "题目总数")
        self.card_wrong = StatCard(cards, "错题数", color=COLORS["wrong"])
        self.card_correct = StatCard(cards, "正确题", color=COLORS["correct"])
        self.card_blank = StatCard(cards, "未做题", color=COLORS["blank"])
        self.card_reports = StatCard(cards, "错题分析报告")
        for c in (self.card_images, self.card_questions, self.card_wrong, self.card_correct,
                  self.card_blank, self.card_reports):
            c.pack(side="left", padx=6, pady=6, fill="y")
        charts = ttk.Frame(tab)
        charts.pack(fill="both", expand=True)
        self.chart_subject = ChartCanvas(charts, width=380, height=250)
        self.chart_subject.pack(side="left", padx=6, pady=6)
        self.chart_type = ChartCanvas(charts, width=380, height=250)
        self.chart_type.pack(side="left", padx=6, pady=6)
        charts2 = ttk.Frame(tab)
        charts2.pack(fill="both", expand=True)
        self.chart_weak = ChartCanvas(charts2, width=380, height=260)
        self.chart_weak.pack(side="left", padx=6, pady=6)
        self.chart_cause = ChartCanvas(charts2, width=380, height=260)
        self.chart_cause.pack(side="left", padx=6, pady=6)

        # --- 日志 ---
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="运行日志")
        self.log_text = RichText(tab, height=24, font_family="Consolas")
        self.log_text.pack(fill="both", expand=True)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="清空显示", command=self.log_text.clear).pack(side="left")
        ttk.Button(row, text="打开日志文件", command=self._open_log_file).pack(side="left", padx=6)

    def _build_result_editor(self, parent) -> None:
        """右侧「识别结果」区：可直接修改并保存回数据库。"""
        wrap = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        wrap.pack(fill="both", expand=True)
        self.result_title = ttk.Label(wrap, text="识别结果", style="Panel.TLabel")
        self.result_title.pack(anchor="w")
        self.result_hint = ttk.Label(wrap, text="导入并识别后，这里显示每道题的结构化结果",
                                     style="Panel.TLabel", foreground=COLORS["muted"],
                                     wraplength=380)
        self.result_hint.pack(anchor="w", pady=(0, 6))

        listwrap = ttk.Frame(wrap)
        listwrap.pack(fill="x")
        self.result_list = ttk.Treeview(listwrap, columns=("idx", "type", "status", "conf"),
                                       show="headings", height=5, selectmode="browse")
        for c, t, w in (("idx", "题号", 50), ("type", "题型", 80), ("status", "状态", 70),
                        ("conf", "置信度", 60)):
            self.result_list.heading(c, text=t)
            self.result_list.column(c, width=w, anchor="center")
        self.result_list.pack(fill="x")
        self.result_list.bind("<<TreeviewSelect>>", lambda e: self.on_result_select())

        form = ttk.Frame(wrap)
        form.pack(fill="both", expand=True, pady=6)
        self.field_vars: Dict[str, tk.Variable] = {}
        self.field_widgets: Dict[str, tk.Widget] = {}

        def row(label: str, key: str, values=None, height=None):
            f = ttk.Frame(form)
            f.pack(fill="x", pady=2)
            ttk.Label(f, text=label, width=8).pack(side="left", anchor="n")
            var = tk.StringVar()
            self.field_vars[key] = var
            if values is not None:
                w = ttk.Combobox(f, textvariable=var, values=values, width=18)
            elif height:
                w = tk.Text(f, height=height, wrap="word", relief="flat", bg="#fbfcfe",
                            highlightthickness=1, highlightbackground=COLORS["border"],
                            font=(self.family, 10))
                var = None
            else:
                w = ttk.Entry(f, textvariable=var)
            w.pack(side="left", fill="both", expand=True)
            self.field_widgets[key] = w

        row("学科", "subject", values=prompts.SUBJECTS)
        row("细分学科", "subject_detail")
        row("学段", "grade", values=prompts.GRADES)
        row("题型", "qtype", values=prompts.QTYPES)
        row("状态", "status", values=["wrong", "correct", "blank"])
        row("题干", "stem", height=5)
        row("选项", "options", height=3)
        row("我的答案", "student_answer")
        row("我的步骤", "student_steps", height=3)
        row("参考答案", "correct_answer", height=3)
        row("知识点", "knowledge")
        f = ttk.Frame(form)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="标签", width=8).pack(side="left", anchor="n")
        self.tag_editor = TagEditor(f, on_change=self.on_tags_changed)
        self.tag_editor.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="💾 保存修改", command=self.save_question_edits).pack(side="left")
        ttk.Button(btns, text="🧠 生成错题分析", command=lambda: self.generate_report(self.current_question_uid, False)
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="📄 导出本题", command=self.export_current_question).pack(side="left", padx=4)
        ttk.Button(btns, text="打开原图", command=self.open_current_image).pack(side="left", padx=4)

    # ------------------------------------------------------------------ #
    # 线程与消息
    # ------------------------------------------------------------------ #
    def _post(self, msg: tuple) -> None:
        self.q.put(msg)

    def _log_threadsafe(self, text: str) -> None:
        self._post(("log", text))

    def _stage_threadsafe(self, text: str) -> None:
        self._post(("stage", text))

    def _log_listener(self, text: str) -> None:
        """logging 模块的日志也进界面（去掉时间戳前缀，由界面统一格式）。"""
        self._post(("log_raw", text))

    def log(self, text: str) -> None:
        self.log_text.append(time.strftime("[%H:%M:%S] ") + str(text))

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind in ("log", "log_raw"):
                    self.log(msg[1])
                elif kind == "stage":
                    self.status.set(str(msg[1]))
                elif kind == "status":
                    self.status.set(str(msg[1]))
                elif kind == "progress":
                    _, done, total, text = msg
                    self.status.set_progress(done, total, text)
                elif kind == "recognized":
                    self._on_recognized(msg[1])
                elif kind == "report_done":
                    self._on_report_done(msg[1], msg[2])
                elif kind == "done":
                    self._set_busy(False, str(msg[1]) if len(msg) > 1 else "完成")
                elif kind == "error":
                    self._set_busy(False, "出错")
                    messagebox.showerror("出错了", str(msg[1]))
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _run_bg(self, fn, on_done_label: str = "") -> None:
        def wrapper():
            try:
                fn()
            except Exception as exc:
                log.exception("后台任务失败")
                self._post(("error", f"{type(exc).__name__}: {exc}"))
                return
            self._post(("done", on_done_label))

        self._set_busy(True, "处理中…")
        threading.Thread(target=wrapper, daemon=True).start()

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_recognize, self.btn_analyze):
            b.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self.busy_label.configure(text=text)
        if not busy:
            self.status.set("就绪")

    # ------------------------------------------------------------------ #
    # 导入
    # ------------------------------------------------------------------ #
    def import_images(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择习题图片（可多选）",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff"), ("所有文件", "*.*")],
        )
        if files:
            self._do_import(list(files))

    def import_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含习题图片的文件夹")
        if not folder:
            return
        files = [str(p) for p in paths.scan_images(folder)]
        if not files:
            messagebox.showinfo("提示", "该文件夹里没有发现支持的图片（jpg/png/bmp/gif/tif）")
            return
        self._do_import(files)

    def _do_import(self, files: List[str]) -> None:
        batch = time.strftime("导入_%Y%m%d_%H%M%S")
        target_dir = paths.images_dir() / batch
        target_dir.mkdir(parents=True, exist_ok=True)
        added = 0
        skipped = 0
        for src in files:
            try:
                name = os.path.basename(src)
                dst = target_dir / name
                if os.path.exists(dst):
                    stem, ext = os.path.splitext(name)
                    dst = target_dir / f"{stem}_{int(time.time() * 1000) % 100000}{ext}"
                shutil.copy2(src, dst)
                info = imageops.probe(str(dst))
                uid = self.store.add_image(str(dst), original_name=name, batch=batch)
                self.pending.append({"uid": uid, "path": str(dst), "name": name,
                                     "status": "待识别", "count": 0,
                                     "quality": f"{info['width']}×{info['height']}" if info else "?"})
                added += 1
            except Exception as exc:
                skipped += 1
                self.log(f"导入失败 {src}：{exc}")
        self.log(f"已导入 {added} 张图片" + (f"，失败 {skipped} 张" if skipped else ""))
        self.refresh_pending()
        if added:
            self.select_pending_index(len(self.pending) - added)
            self.nb.select(0)

    # ------------------------------------------------------------------ #
    # 待处理列表
    # ------------------------------------------------------------------ #
    def refresh_pending(self) -> None:
        self.pending_tree.delete(*self.pending_tree.get_children())
        for i, item in enumerate(self.pending):
            tag = "wrong" if item.get("status") == "已识别" else ""
            self.pending_tree.insert("", "end", iid=str(i), values=(
                item["name"], item.get("status", ""), item.get("count", 0), item.get("quality", "")),
                tags=(tag,))

    def select_pending_index(self, idx: int) -> None:
        if 0 <= idx < len(self.pending):
            self.pending_tree.selection_set(str(idx))
            self.pending_tree.see(str(idx))
            self.on_pending_select()

    def pending_selection(self) -> List[int]:
        return [int(i) for i in self.pending_tree.selection()]

    def step_pending(self, delta: int) -> None:
        sel = self.pending_selection()
        idx = (sel[0] if sel else -1) + delta
        self.select_pending_index(max(0, min(len(self.pending) - 1, idx)))

    def on_pending_select(self) -> None:
        sel = self.pending_selection()
        if not sel:
            return
        item = self.pending[sel[0]]
        self.current_image_uid = item["uid"]
        try:
            img = imageops.load(item["path"])
            self.preview.set_image(img, max_side=int(self.cfg.get("ui.image_display_max", 1200)))
            self.preview_info.configure(
                text=f"{item['name']}　{img.w}×{img.h}　{paths.human_size(os.path.getsize(item['path']))}")
        except Exception as exc:
            self.preview_info.configure(text=f"无法预览：{exc}")
        # 若这张图已有识别结果，展示出来
        image_id = self.store.image_id(item["uid"])
        questions = self.store.list_questions(image_id=image_id, limit=50)
        self.fill_result_list(questions)
        self.refresh_questions()

    def remove_selected_pending(self) -> None:
        sel = self.pending_selection()
        if not sel:
            return
        if not messagebox.askyesno("移除", f"从待处理列表移除选中的 {len(sel)} 张图片？\n"
                                          "（已识别并归档的题目不会删除）"):
            return
        for i in sorted(sel, reverse=True):
            item = self.pending.pop(i)
            try:
                self.store.delete_images([item["uid"]], delete_files=True)
            except Exception as exc:
                self.log(f"删除失败：{exc}")
        self.refresh_pending()

    # ------------------------------------------------------------------ #
    # 识别
    # ------------------------------------------------------------------ #
    def start_recognition(self, only_selected: bool = False) -> None:
        if self.busy:
            messagebox.showinfo("提示", "正在处理中，请稍候或点「停止」")
            return
        sel = self.pending_selection() if only_selected else list(range(len(self.pending)))
        targets = [self.pending[i] for i in sel if i < len(self.pending)]
        if not targets:
            messagebox.showinfo("提示", "请先导入习题图片")
            return
        self.stop_flag = False
        self._run_bg(lambda: self._recognize_worker(targets), "识别完成")

    def recognize_selected(self, force: Optional[str] = None) -> None:
        if self.busy:
            return
        sel = self.pending_selection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧选中一张图片")
            return
        targets = [self.pending[sel[0]]]
        self.stop_flag = False
        self._run_bg(lambda: self._recognize_worker(targets, force=force), "识别完成")

    def stop_recognition(self) -> None:
        self.stop_flag = True
        self.log("已请求停止，当前图片处理完就停…")

    def _recognize_worker(self, targets: List[Dict], force: Optional[str] = None) -> None:
        total = len(targets)
        for i, item in enumerate(targets, start=1):
            if self.stop_flag:
                self._post(("log", "已停止识别"))
                break
            self._post(("progress", i - 1, total, f"正在识别 {item['name']}"))
            self._post(("log", f"—— 第 {i}/{total} 张：{item['name']}"))
            # 用户在「图片预处理」页手工调过这张图时，识别就用他调好的那张，
            # 而不是拿原图重新自动预处理（否则手工纠正会被覆盖掉）
            src, manual = pick_recognition_source(item)
            if manual:
                self._post(("log", "使用手工预处理后的图片进行识别"))
            outcome = self.pipeline.process_image(src, force=force, skip_preprocess=manual)
            uids = self._save_outcome(item, outcome)
            self._post(("recognized", {"item": item, "outcome": outcome, "uids": uids}))
            self._post(("progress", i, total, f"已完成 {i}/{total}"))
        self._post(("status", "识别完成"))

    def _save_outcome(self, item: Dict, outcome) -> List[str]:
        """把识别结果写入数据库 + 归档 + 自动错题分析（都在后台线程里跑）。"""
        uids: List[str] = []
        image_id = self.store.image_id(item["uid"])
        run = self.cfg.run()
        statuses = [q.get("status") for q in outcome.questions]
        summary_status = "wrong" if "wrong" in statuses else (
            "blank" if statuses and all(s == "blank" for s in statuses) else "correct")
        self.store.update_image(item["uid"], {
            "work_path": outcome.work_path,
            "subject": outcome.questions[0].get("subject", "") if outcome.questions else "",
            "subject_detail": outcome.questions[0].get("subject_detail", "") if outcome.questions else "",
            "grade": outcome.questions[0].get("grade", "") if outcome.questions else "",
            "status": summary_status,
            "quality": outcome.quality.get("score", 0),
            "quality_json": str(outcome.quality),
            "engine": outcome.engine,
            "route": outcome.route,
            "preprocess": "，".join(outcome.preprocess_steps),
            "error": outcome.error,
        })
        for q in outcome.questions:
            q2 = dict(q)
            q2["image_path"] = item["path"]
            q2["work_path"] = outcome.work_path
            uid = self.store.add_question(q2, image_id, item["name"])
            uids.append(uid)
        # 自动归档
        if bool(self.cfg.get("archive.write_text", True)) and outcome.questions:
            for q, uid in zip(outcome.questions, uids):
                rec = dict(q)
                rec["uid"] = uid
                rec["image_path"] = item["path"]
                rec["work_path"] = outcome.work_path
                try:
                    res = self.archiver.archive_question(rec)
                    self.store.update_question(uid, {"archived": 1, "archive_path": res.get("folder", "")})
                except Exception as exc:
                    self._post(("log", f"归档失败（{uid}）：{exc}"))
        # 自动错题分析
        if bool(run.get("auto_analyze", True)) and uids:
            for q, uid in zip(outcome.questions, uids):
                if q.get("status") != "wrong":
                    continue
                try:
                    rec = dict(q)
                    rec["uid"] = uid
                    rec["source_path"] = item["path"]
                    report = self.analyzer.analyze(rec)
                    self.store.save_report(uid, report, depth=report.get("depth", "local"),
                                           engine=report.get("engine", ""))
                    self._post(("log", f"{uid} 已生成错题分析（{report.get('depth')}）"))
                except Exception as exc:
                    self._post(("log", f"{uid} 错题分析失败：{exc}"))
        item["status"] = "已识别" if outcome.questions else "失败"
        item["count"] = outcome.question_count
        item["quality"] = f"{outcome.quality.get('score', 0):.2f}" if outcome.quality else ""
        item["work_path"] = outcome.work_path
        return uids

    def _on_recognized(self, payload: Dict) -> None:
        item, outcome, uids = payload["item"], payload["outcome"], payload["uids"]
        self.refresh_pending()
        self.refresh_questions()
        self.refresh_stats()
        self.refresh_archive_tree()
        image_id = self.store.image_id(item["uid"])
        self.fill_result_list(self.store.list_questions(image_id=image_id, limit=50))
        if not outcome.questions:
            self.result_hint.configure(text=f"未识别出题目：{outcome.error or '请尝试云端重识别或手动录入'}")
        else:
            self.result_hint.configure(text=outcome.summary())

    # ------------------------------------------------------------------ #
    # 结果编辑
    # ------------------------------------------------------------------ #
    def fill_result_list(self, questions: List[Dict]) -> None:
        self.result_list.delete(*self.result_list.get_children())
        self._result_questions = questions
        for i, q in enumerate(questions):
            self.result_list.insert("", "end", iid=str(i), values=(
                q.get("index", i + 1), q.get("qtype", ""), STATUS_CN.get(q.get("status"), ""),
                f"{float(q.get('confidence') or 0):.2f}"))
        if questions:
            self.result_list.selection_set("0")
            self.on_result_select()

    def on_result_select(self) -> None:
        sel = self.result_list.selection()
        if not sel or not getattr(self, "_result_questions", None):
            return
        idx = int(sel[0])
        if idx >= len(self._result_questions):
            return
        q = self._result_questions[idx]
        self.current_question_uid = str(q.get("uid", ""))
        self.load_question_into_form(q)

    def load_question_into_form(self, q: Dict) -> None:
        values = {
            "subject": q.get("subject", ""),
            "subject_detail": q.get("subject_detail", ""),
            "grade": q.get("grade", ""),
            "qtype": q.get("qtype", ""),
            "status": q.get("status", ""),
            "stem": q.get("stem", ""),
            "options": "\n".join(q.get("options") or []),
            "student_answer": q.get("student_answer", ""),
            "student_steps": q.get("student_steps", ""),
            "correct_answer": q.get("correct_answer", ""),
            "knowledge": "、".join(str(k) for k in (q.get("knowledge_points") or [])),
        }
        for key, val in values.items():
            widget = self.field_widgets.get(key)
            if widget is None:
                continue
            if isinstance(widget, tk.Text):
                widget.delete("1.0", "end")
                widget.insert("1.0", val)
            else:
                self.field_vars[key].set(val)
        self.tag_editor.set(q.get("tags") or [])
        self.result_title.configure(text=f"识别结果　{q.get('uid', '')}　"
                                         f"{prompts.status_cn(str(q.get('status', '')))}")
        # 同步到错题分析页
        report = self.store.get_report(str(q.get("uid", ""))) if q.get("uid") else None
        self.report.show(q, report)
        self.preview_question_image(q)

    def _read_form(self) -> Dict:
        def get(key: str) -> str:
            widget = self.field_widgets.get(key)
            if isinstance(widget, tk.Text):
                return widget.get("1.0", "end").strip()
            return str(self.field_vars[key].get()).strip()

        return {
            "subject": get("subject"),
            "subject_detail": get("subject_detail"),
            "grade": get("grade"),
            "qtype": get("qtype"),
            "status": get("status"),
            "stem": get("stem"),
            "options": [line for line in get("options").split("\n") if line.strip()],
            "student_answer": get("student_answer"),
            "student_steps": get("student_steps"),
            "correct_answer": get("correct_answer"),
            "knowledge_points": [k.strip() for k in
                                 get("knowledge").replace("，", "、").replace(",", "、").split("、")
                                 if k.strip()],
        }

    def save_question_edits(self) -> None:
        if not self.current_question_uid:
            messagebox.showinfo("提示", "请先选择一道题")
            return
        patch = self._read_form()
        self.store.update_question(self.current_question_uid, patch)
        for tag in self.tag_editor.tags:
            self.store.add_tag("question", self.store.question_id(self.current_question_uid), tag)
        self.log(f"已保存 {self.current_question_uid} 的修改")
        self.refresh_questions()
        # 归档目录同步更新文字稿
        try:
            q = self.store.get_question(self.current_question_uid)
            if q:
                self.archiver.archive_question(q, self.store.get_report(self.current_question_uid))
        except Exception as exc:
            self.log(f"归档文字稿更新失败：{exc}")
        self.status.set("修改已保存")

    def on_tags_changed(self, tags: List[str]) -> None:
        if not self.current_question_uid:
            return
        qid = self.store.question_id(self.current_question_uid)
        for tag in tags:
            self.store.add_tag("question", qid, tag)

    def preview_question_image(self, q: Dict) -> None:
        src = q.get("work_path") or q.get("image_path") or ""
        if src and os.path.exists(src):
            try:
                self.preview.set_image(imageops.load(src),
                                       max_side=int(self.cfg.get("ui.image_display_max", 1200)))
                self.preview_info.configure(text=f"{os.path.basename(src)}（{q.get('uid', '')}）")
            except Exception as exc:
                self.preview_info.configure(text=f"预览失败：{exc}")

    def open_current_image(self) -> None:
        sel = self.result_list.selection()
        if not sel or not getattr(self, "_result_questions", None):
            return
        q = self._result_questions[int(sel[0])]
        src = q.get("work_path") or q.get("image_path")
        if src and os.path.exists(src):
            self._open(src)
        else:
            messagebox.showinfo("提示", "找不到这张题的图片文件")

    # ------------------------------------------------------------------ #
    # 题库 / 搜索 / 归档树
    # ------------------------------------------------------------------ #
    def refresh_questions(self) -> None:
        from ..mathfmt import format_for_export

        self._refresh_kp_filter()      # 先按当前学科/状态收窄知识点下拉框
        questions = self.store.list_questions(
            limit=800, status=self.filter_status.get(), subject=self.filter_subject.get(),
            knowledge=self._selected_knowledge())
        self.q_tree.delete(*self.q_tree.get_children())
        for q in questions:
            # 列表里显示可读写法（x² 而不是 $x^2$）；公式的图形排版留给报告页
            stem = format_for_export((q.get("stem") or "").replace("\n", " "))[:60]
            kp = "、".join(str(k) for k in (q.get("knowledge_points") or []))
            self.q_tree.insert("", "end", iid=q["uid"], values=(
                q.get("uid"), q.get("subject", ""), kp[:18] or "—", q.get("qtype", ""),
                STATUS_CN.get(q.get("status"), ""), stem),
                tags=(str(q.get("status")),))

    def _selected_knowledge(self) -> str:
        """下拉框里显示的是「复数（3）」，这里换回真正的知识点名「复数」。"""
        return self._kp_map.get(self.filter_knowledge.get(), "")

    def _refresh_kp_filter(self) -> None:
        """把题库里出现过的知识点填进筛选下拉框（跟着学科/状态的筛选一起收窄）。

        知识点来自识别时模型给出的 ``knowledge_points``（如"复数"、"单位根"），
        所以导入并识别完一道题，它的知识点就会出现在这个下拉框里，选一下即可筛出同类题。
        """
        try:
            items = self.store.knowledge_points(subject=self.filter_subject.get(),
                                                status=self.filter_status.get())
        except Exception as exc:      # 下拉框出问题不该拖垮整个题库刷新
            log.warning("读取知识点列表失败：%s", exc)
            items = []
        self._kp_map = {KP_ALL: ""}
        for it in items:
            self._kp_map[f"{it['name']}（{it['count']}）"] = it["name"]
        values = list(self._kp_map)
        self.kp_combo.configure(values=values)
        if self.filter_knowledge.get() not in self._kp_map:
            # 之前选的知识点在当前学科/状态下已经不存在了 → 回到「全部知识点」
            self.filter_knowledge.set(KP_ALL)

    def on_question_select(self) -> None:
        sel = self.q_tree.selection()
        if not sel:
            return
        q = self.store.get_question(sel[0])
        if not q:
            return
        self.current_question_uid = str(q.get("uid"))
        self.load_question_into_form(q)
        if self.nb.index("current") not in (0, 2):
            self.nb.select(2)  # 跳到错题分析页

    def delete_selected_questions(self) -> None:
        sel = list(self.q_tree.selection())
        if not sel:
            return
        if not messagebox.askyesno("删除", f"删除选中的 {len(sel)} 道题？（数据库记录会删除，"
                                          "归档目录里的文件需要手动清理）"):
            return
        n = self.store.delete_questions(sel)
        self.log(f"已删除 {n} 道题")
        self.refresh_questions()
        self.refresh_stats()

    def do_search(self) -> None:
        from ..mathfmt import format_for_export

        kw = self.search_var.get().strip()
        results = self.store.search(keyword=kw, limit=400)
        self.search_tree.delete(*self.search_tree.get_children())
        for q in results:
            self.search_tree.insert("", "end", iid=q["uid"], values=(
                q.get("uid"), q.get("subject", ""), q.get("qtype", ""),
                STATUS_CN.get(q.get("status"), ""),
                "、".join(str(k) for k in (q.get("knowledge_points") or []))[:24],
                format_for_export((q.get("stem") or "").replace("\n", " "))[:60]))
        self.search_status_var.set(f"共 {len(results)} 条结果")
        self.log(f"搜索「{kw or '（全部）'}」：{len(results)} 条")
        if results:
            self.search_tree.selection_set(results[0]["uid"])
            self.on_search_select()

    def on_search_select(self) -> None:
        sel = self.search_tree.selection()
        if not sel:
            return
        q = self.store.get_question(sel[0])
        if not q:
            return
        self.current_question_uid = q["uid"]
        rep = self.store.get_report(q["uid"])
        blocks = [("h1", f"{q['uid']}　{STATUS_CN.get(q.get('status'), '')}")]
        for kind, text in [("kv", f"学科：{q.get('subject')}（{q.get('subject_detail')}）　"
                                  f"学段：{q.get('grade')}　题型：{q.get('qtype')}")]:
            blocks.append((kind, text))
        blocks += [("h2", "题干"), ("para", q.get("stem") or "（无）")]
        if q.get("options"):
            blocks += [("h2", "选项")] + [("bullet", o) for o in q["options"]]
        if q.get("student_answer"):
            blocks += [("h2", "我的作答"), ("para", q["student_answer"])]
        if q.get("correct_answer"):
            blocks += [("h2", "参考答案"), ("para", q["correct_answer"])]
        if rep:
            blocks += [("h2", "错题分析"), ("para", rep.get("restate", ""))]
            blocks += [("bullet", f"[{c.get('type')}] {c.get('detail')}")
                       for c in (rep.get("causes") or []) if isinstance(c, dict)]
        blocks += [("kv", f"归档目录：{q.get('archive_path') or '（未归档）'}")]
        self.search_text.write_blocks(blocks)
        self.load_question_into_form(q)

    def refresh_archive_tree(self) -> None:
        self.archive_tree.delete(*self.archive_tree.get_children())
        root_dir = str(self.archiver.root())
        if not os.path.isdir(root_dir):
            return
        root_node = self.archive_tree.insert("", "end", text=f"归档目录：{root_dir}", open=True)
        self._fill_tree(root_node, root_dir, depth=0)

    def _fill_tree(self, parent: str, folder: str, depth: int) -> None:
        if depth > 3:
            return
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return
        dirs = [e for e in entries if os.path.isdir(os.path.join(folder, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(folder, e))]
        for d in dirs[:80]:
            node = self.archive_tree.insert(parent, "end", text=d, open=(depth < 1))
            self._fill_tree(node, os.path.join(folder, d), depth + 1)
        if depth >= 1:
            for f in files[:60]:
                self.archive_tree.insert(parent, "end", text=f)

    # ------------------------------------------------------------------ #
    # 错题分析
    # ------------------------------------------------------------------ #
    def generate_report(self, uid: str, cloud: bool = False) -> None:
        if self.busy:
            messagebox.showinfo("提示", "正在处理中，请稍候")
            return
        if not uid:
            messagebox.showinfo("提示", "请先选择一道题")
            return
        q = self.store.get_question(uid)
        if not q:
            return
        self.report.set_busy(True, "正在生成分析报告…")

        def work():
            rec = dict(q)
            rec["source_path"] = q.get("work_path") or q.get("image_path")
            report = self.analyzer.analyze(rec, allow_cloud=cloud or None)
            self.store.save_report(uid, report, depth=report.get("depth", "local"),
                                   engine=report.get("engine", ""))
            try:
                self.archiver.archive_question(self.store.get_question(uid) or q, report)
            except Exception as exc:
                self._post(("log", f"归档报告失败：{exc}"))
            self._post(("report_done", uid, report))

        self._run_bg(work, "分析完成")

    def _on_report_done(self, uid: str, report: Dict) -> None:
        self.report.set_busy(False)
        q = self.store.get_question(uid)
        if q:
            self.report.show(q, report)
        self.refresh_stats()
        self.status.set(f"{uid} 分析报告已生成")

    def analyze_batch(self, cloud: bool = False) -> None:
        if self.busy:
            return
        wrong = self.store.wrong_questions(limit=200)
        missing = [q for q in wrong if not q.get("report")]
        if not missing:
            messagebox.showinfo("提示", "所有错题都已有分析报告。\n"
                                        "（如需重新生成，请在「错题分析」页对单题操作）")
            return
        if not messagebox.askyesno("批量分析", f"为 {len(missing)} 道还没有报告的错题生成分析？\n"
                                              f"{'将优先使用云端深度分析。' if cloud else '使用本地模型离线生成。'}"):
            return

        def work():
            total = len(missing)
            for i, q in enumerate(missing, start=1):
                self._post(("progress", i - 1, total, f"分析 {q['uid']}"))
                try:
                    rec = dict(q)
                    rec["source_path"] = q.get("work_path") or q.get("image_path")
                    report = self.analyzer.analyze(rec, allow_cloud=cloud or None)
                    self.store.save_report(q["uid"], report, depth=report.get("depth", "local"),
                                           engine=report.get("engine", ""))
                except Exception as exc:
                    self._post(("log", f"{q['uid']} 分析失败：{exc}"))
            self._post(("progress", total, total, "分析完成"))
            self._post(("log", f"批量分析完成，共 {total} 道"))

        self._run_bg(work, "批量分析完成")

    # ------------------------------------------------------------------ #
    # 导出 / 归档
    # ------------------------------------------------------------------ #
    def export_dialog(self, scope: str) -> None:
        if scope == "selection":
            uids = list(self.q_tree.selection())
            if not uids:
                messagebox.showinfo("提示", "请先在左侧「题库」里选中要导出的题目")
                return
            default = "选中题目"
        elif scope == "wrong":
            uids, default = None, "个人错题本"
        elif scope == "weak":
            path = filedialog.asksaveasfilename(
                title="导出知识点薄弱清单", defaultextension=".md",
                initialfile=f"知识点薄弱清单_{time.strftime('%Y%m%d')}.md",
                filetypes=[("Markdown", "*.md"), ("纯文本", "*.txt"), ("网页", "*.html"),
                           ("Word", "*.docx")])
            if not path:
                return
            self._run_bg(lambda: self._do_export_special("weak", path), "薄弱清单已导出")
            return
        elif scope == "summary":
            path = filedialog.asksaveasfilename(
                title="导出错题汇总", defaultextension=".md",
                initialfile=f"错题汇总_{time.strftime('%Y%m%d')}.md",
                filetypes=[("Markdown", "*.md"), ("纯文本", "*.txt"), ("网页", "*.html"),
                           ("Word", "*.docx")])
            if not path:
                return
            self._run_bg(lambda: self._do_export_special("summary", path), "错题汇总已导出")
            return
        else:
            uids, default = None, "全部题目"

        path = filedialog.asksaveasfilename(
            title="导出", defaultextension=".docx",
            initialfile=f"{default}_{time.strftime('%Y%m%d')}.docx",
            filetypes=[("Word 文档", "*.docx"), ("PDF 文档", "*.pdf"), ("Markdown", "*.md"),
                       ("纯文本", "*.txt"), ("网页", "*.html")])
        if not path:
            return
        status = "wrong" if scope == "wrong" else ""
        self._run_bg(lambda: self._export_worker(path, uids, status), "导出完成")

    def _export_worker(self, path: str, uids, status: str) -> None:
        self._post(("log", f"开始导出到 {path} …"))
        out = self.exporter.export(path, uids=uids, status=status, include_images=True)
        size = paths.human_size(os.path.getsize(out)) if os.path.exists(out) else "?"
        self._post(("log", f"导出完成：{out}（{size}）"))
        self._post(("status", f"已导出：{os.path.basename(out)}（{size}）"))

    def _do_export_special(self, kind: str, path: str) -> None:
        if kind == "weak":
            self.exporter.export_weak_list(path)
        else:
            self.exporter.export_summary(path)
        self._post(("log", f"已导出：{path}"))

    def export_current_question(self) -> None:
        if not self.current_question_uid:
            messagebox.showinfo("提示", "请先选择一道题")
            return
        path = filedialog.asksaveasfilename(
            title="导出本题", defaultextension=".md",
            initialfile=f"{self.current_question_uid}.md",
            filetypes=[("Markdown", "*.md"), ("Word 文档", "*.docx"), ("PDF 文档", "*.pdf"),
                       ("纯文本", "*.txt"), ("网页", "*.html")])
        if not path:
            return
        uid = self.current_question_uid
        self._run_bg(lambda: self._export_worker(path, [uid], ""), "导出完成")

    def archive_selected(self) -> None:
        uids = list(self.q_tree.selection())
        if not uids:
            messagebox.showinfo("提示", "请先在左侧「题库」里选中要归档的题目")
            return

        def work():
            items = []
            for uid in uids:
                q = self.store.get_question(uid)
                if q:
                    items.append({"question": q, "report": q.get("report")})
            res = self.archiver.archive_many(items)
            for uid, r in zip(uids, res):
                self.store.update_question(uid, {"archived": 1, "archive_path": r.get("folder", "")})
            self.archiver.write_index(self.store.stats())
            self._post(("log", f"已归档 {len(res)} 道题到 {self.archiver.root()}"))

        self._run_bg(work, "归档完成")
        self.refresh_archive_tree()

    def rebuild_index(self) -> None:
        path = self.archiver.write_index(self.store.stats())
        self.log(f"归档索引已重建：{path}")
        messagebox.showinfo("完成", f"索引已写入：\n{path}")

    def export_json_all(self) -> None:
        path = self.store.export_json()
        self.log(f"数据已导出：{path}")
        messagebox.showinfo("导出完成", f"已导出全部数据（JSON）：\n{path}")

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def refresh_stats(self) -> None:
        s = self.store.stats()
        self.card_images.set(s.get("images", 0))
        self.card_questions.set(s.get("questions", 0))
        self.card_wrong.set(s.get("wrong", 0))
        self.card_correct.set(s.get("correct", 0))
        self.card_blank.set(s.get("blank", 0))
        self.card_reports.set(s.get("reports", 0))
        self.chart_subject.draw_pie(s.get("by_subject") or {}, "各学科题目占比")
        self.chart_type.draw_bars(s.get("by_type") or {}, "题型分布", max_items=8)
        self.chart_weak.draw_bars(s.get("weak_knowledge") or {}, "薄弱知识点（错题次数）",
                                  max_items=8, color=COLORS["wrong"])
        self.chart_cause.draw_bars(s.get("cause_stats") or {}, "错误原因分布", max_items=8,
                                   color=COLORS["warn"])

    def refresh_all(self) -> None:
        self.refresh_pending()
        self.refresh_questions()
        self.refresh_stats()
        self.refresh_archive_tree()

    # ------------------------------------------------------------------ #
    # 其它
    # ------------------------------------------------------------------ #
    def load_into_editor(self) -> None:
        sel = self.pending_selection()
        path = ""
        if sel:
            item = self.pending[sel[0]]
            path = item.get("work_path") or item["path"]
        elif self.current_question_uid:
            q = self.store.get_question(self.current_question_uid)
            path = (q or {}).get("work_path") or (q or {}).get("image_path") or ""
        if not path or not os.path.exists(path):
            messagebox.showinfo("提示", "请先在左侧选中一张图片")
            return
        self.nb.select(1)
        self.editor.load(path)

    def on_editor_applied(self, path: str, steps: List[str]) -> None:
        """预处理结果回写给当前图片：标记为「手工处理过」，下次识别直接用它。"""
        sel = self.pending_selection()
        applied = False
        if sel:
            item = self.pending[sel[0]]
            item["work_path"] = path
            item["manual_edit"] = True      # 关键：不置这个标记，手工结果会被自动预处理覆盖
            self.store.update_image(item["uid"], {"work_path": path})
            applied = True
            self.refresh_pending()
        if self.current_question_uid:
            # 让「识别与归档」页的预览也切到手工处理后的图
            image_uid = self.store.image_uid_of_question(self.current_question_uid)
            if image_uid:
                self.store.update_image(image_uid, {"work_path": path})
        self.log(f"预处理结果已应用：{os.path.basename(path)}"
                 f"（{'，'.join(steps) if steps else '无步骤'}）"
                 + ("；下次识别将直接使用这张图" if applied else ""))

    def toggle_cloud(self) -> None:
        if not self.cfg.get("cloud.enabled", False):
            if not self.cfg.effective_api_key():
                if not messagebox.askyesno(
                    "开启云端增强",
                    "还没有填云端 API Key。\n\n现在去「设置 → 云端模型」填写？（填好后即可使用云端兜底与深度分析）"
                ):
                    return
                self.open_settings()
                return
            self.cfg.set("cloud.enabled", True)
        else:
            self.cfg.set("cloud.enabled", False)
        self.cfg.save()
        self.pipeline = Pipeline(self.cfg, on_log=self._log_threadsafe, on_stage=self._stage_threadsafe)
        self.analyzer = Analyzer(self.cfg, on_log=self._log_threadsafe)
        self.log(f"云端增强：{'已开启' if self.cfg.cloud_enabled() else '已关闭'}")
        self._refresh_mode_labels()

    def _refresh_mode_labels(self) -> None:
        self.mode_label.configure(text=f"模式：{self.cfg.mode_label()}")
        on = self.cfg.cloud_enabled()
        self.cloud_btn_text.set("☁ 云端增强：开" if on else "☁ 云端增强：关")

    def open_settings(self) -> None:
        def on_saved():
            self.cfg.save()
            self.pipeline = Pipeline(self.cfg, on_log=self._log_threadsafe, on_stage=self._stage_threadsafe)
            self.analyzer = Analyzer(self.cfg, on_log=self._log_threadsafe)
            self.archiver = Archiver(self.cfg, self.store)
            self._refresh_mode_labels()
            self.log("设置已更新")

        SettingsDialog(self, self.cfg, on_saved=on_saved)

    def run_selfcheck(self) -> None:
        """在界面里跑一遍自检，把结果打到日志页（不另开进程，方便直接看）。"""
        try:
            import sys as _sys

            tools_dir = str(paths.tools_dir())
            if tools_dir not in _sys.path:
                _sys.path.insert(0, tools_dir)
            import selfcheck  # type: ignore  # tools/selfcheck.py

            rep = selfcheck.Report()
            selfcheck.check_environment(rep)
            selfcheck.check_storage(rep)
            selfcheck.check_images(rep)
            selfcheck.check_gpu(rep)
            selfcheck.check_local_model(rep, self.cfg, deep=False)
            selfcheck.check_cloud(rep, self.cfg)
            selfcheck.check_export(rep)
            self.nb.select(5)
            self.log("———— 环境自检 ————")
            for entry in rep.items:
                self.log(f"{entry['status']} {entry['name']}：{entry['detail']}")
                if entry["hint"]:
                    self.log(f"      ↳ {entry['hint']}")
            bad = sum(1 for i in rep.items if i["status"] == "❌")
            self.status.set("自检完成" + ("（有失败项，见日志）" if bad else "，一切正常"))
        except Exception as exc:
            messagebox.showerror("自检失败", f"{type(exc).__name__}: {exc}")

    def open_docs(self) -> None:
        docs = paths.docs_dir()
        if docs.is_dir():
            self._open(str(docs))
        else:
            messagebox.showinfo("提示", "文档目录不存在，请查看项目根目录下的 README.md")

    def show_about(self) -> None:
        from ..crypto import self_test

        s = self.store.stats()
        messagebox.showinfo("关于", (
            f"{paths.APP_NAME}　v{paths.APP_VERSION}\n\n"
            f"运行模式：{self.cfg.mode_label()}\n"
            f"本地模型：{self.cfg.get('local.model')}\n"
            f"云端增强：{'开启' if self.cfg.cloud_enabled() else '关闭'}\n"
            f"题库：{s.get('questions', 0)} 道题（错题 {s.get('wrong', 0)}）\n"
            f"加密自检：{'通过' if self_test() else '异常'}\n"
            f"数据目录：{paths.data_dir()}\n\n"
            "本软件完全本地运行，无付费项目、无广告、无会员限制。"
        ))

    def backup_data(self) -> None:
        def work():
            path = self.store.backup_zip()
            self._post(("log", f"备份完成：{path}（{paths.human_size(os.path.getsize(path))}）"))
            self._post(("status", f"备份完成：{os.path.basename(path)}"))

        self._run_bg(work, "备份完成")

    def restore_data(self) -> None:
        zip_path = filedialog.askopenfilename(title="选择备份文件", filetypes=[("备份包", "*.zip")])
        if not zip_path:
            return
        if not messagebox.askyesno("恢复数据", "恢复会用备份包覆盖当前数据库与图片，确定继续？"):
            return
        try:
            import zipfile

            self.store.close()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(str(paths.data_dir()))
            self.store = Store(encrypt=bool(self.cfg.get("privacy.encrypt_db", True)))
            self.archiver = Archiver(self.cfg, self.store)
            self.exporter = Exporter(self.cfg, self.store)
            self.refresh_all()
            messagebox.showinfo("恢复完成", "数据已从备份恢复，界面已刷新。")
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))

    def clear_data(self) -> None:
        if not messagebox.askyesno("清空数据", "将删除所有题目、报告与导入的图片，且不可恢复！\n\n"
                                              "建议先点「备份数据」。确定继续？"):
            return
        if not messagebox.askyesno("再次确认", "真的要清空吗？这是最后一次确认。"):
            return
        self.store.clear_all()
        self.pending = []
        self.refresh_all()
        self.log("已清空全部数据")
        messagebox.showinfo("完成", "数据已清空")

    def _open(self, path: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys_platform_is_mac():
                import subprocess

                subprocess.Popen(["open", path])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showwarning("打开失败", f"{exc}\n路径：{path}")

    def _open_log_file(self) -> None:
        self._open(str(paths.log_file()))

    def _on_close(self) -> None:
        try:
            self.cfg.set("ui.window", self.geometry())
            self.cfg.save()
            self.store.close()
        except Exception:
            pass
        logging_setup.remove_listener(self._log_listener)
        self.destroy()


def sys_platform_is_mac() -> bool:
    import sys

    return sys.platform == "darwin"


def run() -> int:
    """入口：启动主窗口。"""
    logging_setup.setup()
    try:
        app = MainWindow()
    except Exception as exc:  # 启动失败时给出可读提示（尤其是缺 tcl/tk）
        import traceback

        traceback.print_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}\n\n"
                                            "请运行「环境自检.bat」查看详细原因。")
            root.destroy()
        except Exception:
            pass
        return 1
    app.mainloop()
    return 0
