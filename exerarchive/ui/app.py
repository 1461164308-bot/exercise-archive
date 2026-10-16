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
from .softui import AmbientBackground, GlassCard, SoftButton, ScrollableFrame, WelcomePanel
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

        self.title(f"AI 错题本  v{paths.APP_VERSION}")
        self.geometry(self._initial_geometry())
        self.minsize(1120, 720)
        self.animations_enabled = bool(self.cfg.get("ui.animations", True))

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
        self._task_current = None
        self._task_tick_job = None
        self._completion_notice = ""
        self._engines_used: List[str] = []      # 本次任务里每张图实际用的引擎（云端优先时用来看"到底谁干的"）
        self._editing_question = False
        self._loaded_form_uid = ""

        self._build()
        logging_setup.add_listener(self._log_listener)
        self._queue_job = self.after(120, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_all()
        self._startup_message()

    # ------------------------------------------------------------------ #
    def _initial_geometry(self) -> str:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        saved = str(self.cfg.get("ui.window", "") or "")
        if saved and "x" in saved:
            try:
                import re
                match = re.fullmatch(r"(\d+)x(\d+)(?:[+-]\d+[+-]\d+)?", saved)
                w, h = int(match[1]), int(match[2])
            except (ValueError, TypeError):
                w, h = 1480, 920
        else:
            w, h = 1480, 920
        w, h = min(w, sw-40), min(h, sh-90)
        return f"{w}x{h}+{max(0, (sw-w)//2)}+{max(0, (sh-h)//3)}"

    def _engine_hint(self) -> str:
        """底部状态栏右侧那行字：说清**当前实际会用哪个模型**。

        用户反馈过：明明选了「云端优先」，界面上却到处写着「本地模型 qwen3-vl:4b」，
        看着像小模型在干活。所以这里按 模式 + 云端是否真的可用 给出真实答案
        （``cloud_enabled()`` 已经包含"有没有填 API Key"，所以它能代表"云端可用"）。
        """
        mode = self.cfg.mode()
        local_model = str(self.cfg.get("local.model", "") or "本地模型")
        cloud_model = str((self.cfg.active_cloud() or {}).get("model", "") or "云端模型")
        cloud_ok = self.cfg.cloud_enabled()
        if mode == "cloud_first":
            return f"云端模型 {cloud_model}" if cloud_ok else "云端优先（云端未配置：去「设置 → 云端模型」填 API Key）"
        if mode == "local_only":
            return f"本地模型 {local_model}（纯本地，不联网）"
        if not cloud_ok:
            return f"本地模型 {local_model}（云端未配置，不会联网）"
        return f"本地 {local_model} ＋ 云端 {cloud_model}"

    def _startup_message(self) -> None:
        mode = self.cfg.mode_label()
        self.status.set(f"就绪　模式：{mode}", self._engine_hint())
        self.log(f"数据目录：{paths.data_dir()}")
        self.log(f"运行模式：{mode}　云端增强：{'开启' if self.cfg.cloud_enabled() else '关闭'}")
        cloud_only = self.cfg.mode() == "cloud_first" and self.cfg.cloud_enabled()
        if cloud_only:
            # 云端优先且云端已配好：没必要再去拉本地模型（省内存，也免得弹出"本地模型未就绪"让人误会）
            self.log("当前为「云端优先」且云端已配置，本次启动不拉起本地模型服务")
        elif self.cfg.get("local.auto_start", True):
            threading.Thread(target=self._ensure_model_async, daemon=True).start()

    def _ensure_model_async(self) -> None:
        try:
            ok = ModelClient("local", self.cfg).manager.ensure_running(on_log=self._log_threadsafe)
            if ok:
                self._post(("status", "本地模型服务已就绪"))
            elif self.cfg.mode() == "cloud_first":
                self._post(("status", "云端优先模式：本地模型服务未就绪（不影响识别，识别走云端）"))
            else:
                self._post(("status", "本地模型服务未就绪（可在设置里检查）"))
        except Exception as exc:
            self._log_threadsafe(f"启动本地模型服务失败：{exc}")

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self._page = "import"
        self._compare_image = False
        self._switching = False
        self._nav_buttons = {}
        self._ambient = AmbientBackground(self)
        self._ambient.place(relwidth=1, relheight=1)
        shell = tk.Frame(self, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True, padx=18, pady=(18, 0))
        rail = GlassCard(shell, width=190, padding=16)
        rail.pack(side="left", fill="y", pady=(0, 12))
        self._build_navigation(rail.body)
        self.main = tk.Frame(shell, bg=COLORS["bg"])
        self.main.pack(side="left", fill="both", expand=True, padx=(18, 0))
        self._build_header()
        self._build_toolbar()
        self.workspace_card = GlassCard(self.main, padding=16)
        self.workspace_card.pack(fill="both", expand=True, pady=(12, 12))
        self.paned = ttk.Panedwindow(self.workspace_card.body, orient="horizontal")
        self.paned.pack(fill="both", expand=True)
        self._build_left(self.paned)
        self._build_center(self.paned)
        self.status = StatusBar(self)
        self.status.pack(fill="x", side="bottom", padx=20, pady=(0, 8))
        self.left_nb.bind("<<NotebookTabChanged>>", self._on_left_page)
        self.nb.bind("<<NotebookTabChanged>>", self._on_center_page)
        self.bind("<Control-o>", lambda _: self.import_images())
        self.bind("<Control-s>", lambda _: self.save_question_edits())
        self.bind("<Control-f>", lambda _: self._focus_search())
        self.bind("<F5>", lambda _: self.refresh_all())
        self.navigate("import", animate=False)

    def _build_navigation(self, parent):
        brand = tk.Frame(parent, bg=COLORS["panel"])
        brand.pack(fill="x", pady=(8, 25))
        logo = tk.Canvas(brand, width=40, height=40, bg=COLORS["panel"], highlightthickness=0)
        logo.pack(side="left")
        from .softui import rounded
        rounded(logo, 1, 1, 39, 39, 12, fill=COLORS["primary"], outline="")
        logo.create_text(20, 20, text="题", fill="white", font=(self.family, 17, "bold"))
        title = tk.Frame(brand, bg=COLORS["panel"])
        title.pack(side="left", padx=9)
        tk.Label(title, text="AI 错题本", bg=COLORS["panel"], fg=COLORS["text"],
                 font=(self.family, 13, "bold")).pack(anchor="w")
        tk.Label(title, text="让每次练习都有收获", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=(self.family, 8)).pack(anchor="w")
        tk.Label(parent, text="学习空间", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=(self.family, 9)).pack(anchor="w", padx=12, pady=(0, 10))
        for key, text in (("import", "＋    导入整理"), ("library", "▤    我的题库"),
                          ("report", "✧    错题分析"), ("stats", "◷    学习概览"),
                          ("search", "⌕    搜索题目")):
            b = SoftButton(parent, text=text, width=158, height=44, anchor="w",
                           command=lambda k=key: self.navigate(k), quiet=True)
            b.pack(fill="x", pady=4)
            self._nav_buttons[key] = b
        self.library_count = tk.StringVar(value="0 道题目 · 慢慢积累")
        tk.Label(parent, textvariable=self.library_count, bg=COLORS["panel"], fg=COLORS["muted"],
                 font=(self.family, 9)).pack(anchor="w", padx=12, pady=(18, 0))
        bottom = tk.Frame(parent, bg=COLORS["panel"])
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(bottom).pack(fill="x", pady=(0, 10))
        for key, text in (("editor", "▧    图片优化"), ("archive", "▱    归档目录"),
                          ("logs", "≡    运行日志")):
            b = SoftButton(bottom, text=text, width=158, height=38, anchor="w",
                           command=lambda k=key: self.navigate(k), quiet=True)
            b.pack(fill="x", pady=2)
            self._nav_buttons[key] = b
        tk.Label(bottom, text="一题一整理，一步一进步。", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=(self.family, 8)).pack(anchor="w", padx=10, pady=(16, 8))

    def _build_header(self) -> None:
        head = tk.Frame(self.main, bg=COLORS["bg"])
        head.pack(fill="x", pady=(4, 0))
        actions = tk.Frame(head, bg=COLORS["bg"])
        actions.pack(side="right", anchor="n", pady=8)
        SoftButton(actions, text="设置", width=68, command=self.open_settings).pack(side="right", padx=(6, 0))
        more = ttk.Menubutton(actions, text="更多  ⋯")
        menu = tk.Menu(more, tearoff=False, bg=COLORS["panel"], fg=COLORS["text"],
                       activebackground="#e3e9f8", activeforeground=COLORS["primary_dark"],
                       font=(self.family, 10))
        for label, command in (("环境自检", self.run_selfcheck),
            ("打开数据目录", lambda: self._open(str(paths.data_dir()))),
            ("打开归档目录", lambda: self._open(str(self.archiver.root()))),
            ("查看日志文件", self._open_log_file), ("重建归档索引", self.rebuild_index),
            ("导出全部数据 (JSON)", self.export_json_all)):
            menu.add_command(label=label, command=command)
        menu.add_separator()
        menu.add_command(label="备份数据…", command=self.backup_data)
        menu.add_command(label="从备份恢复…", command=self.restore_data)
        menu.add_separator()
        menu.add_command(label="使用说明", command=self.open_docs)
        menu.add_command(label="关于", command=self.show_about)
        menu.add_separator()
        menu.add_command(label="清空全部数据…", command=self.clear_data)
        more["menu"] = menu
        more.pack(side="right")
        self.page_title = tk.Label(head, text="", bg=COLORS["bg"], fg=COLORS["text"],
                                   font=(self.family, 24, "bold"), anchor="w")
        self.page_title.pack(anchor="w")
        self.page_hint = tk.Label(head, text="", bg=COLORS["bg"], fg=COLORS["muted"],
                                  font=(self.family, 10), anchor="w")
        self.page_hint.pack(anchor="w", pady=(8, 14))

    def _build_toolbar(self) -> None:
        card = GlassCard(self.main, height=78, padding=14)
        self.toolbar_card = card
        card.pack(fill="x")
        bar = card.body
        self.import_actions = tk.Frame(bar, bg=COLORS["panel"])
        self.import_actions.pack(side="left")
        SoftButton(self.import_actions, text="＋ 导入图片", command=self.import_images, width=112).pack(side="left")
        SoftButton(self.import_actions, text="导入文件夹", command=self.import_folder, width=105).pack(side="left", padx=6)
        self.btn_recognize = SoftButton(bar, text="开始识别  →", command=self.start_recognition, primary=True, width=128)
        self.btn_recognize.pack(side="left", padx=(6, 0))
        self.btn_stop = SoftButton(bar, text="停止", command=self.stop_recognition, state="disabled", width=62)
        self.btn_analyze = SoftButton(bar, text="生成错题分析", command=lambda: self.analyze_batch(False), primary=True, width=138)
        self.cloud_btn_text = tk.StringVar()
        self.mode_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.cloud_button = SoftButton(bar, textvariable=self.cloud_btn_text, width=128, command=self.toggle_cloud)
        self.cloud_button.pack(side="right", padx=(6, 0))
        export = ttk.Menubutton(bar, text="导出  ↓")
        emenu = tk.Menu(export, tearoff=False, font=(self.family, 10))
        for label, key in (("选中题目…", "selection"), ("个人错题本…", "wrong"),
                           ("知识点薄弱清单…", "weak"), ("错题汇总…", "summary"), ("全部题目…", "all")):
            emenu.add_command(label=label, command=lambda k=key: self.export_dialog(k))
        export["menu"] = emenu
        export.pack(side="right", padx=4)
        self.busy_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self._refresh_mode_labels()

    def _tree(self, parent, columns, definitions, **kw):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=columns, show="headings", **kw)
        for key, title, width in definitions:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=35, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=sb.set, xscrollcommand=xsb.set)
        xsb.pack(side="bottom", fill="x")
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        return tree

    def _build_left(self, parent) -> None:
        self.left_frame = ttk.Frame(parent, width=310)
        parent.add(self.left_frame, weight=1)
        self.left_nb = ttk.Notebook(self.left_frame, style="Workspace.TNotebook")
        self.left_nb.pack(fill="both", expand=True, padx=(0, 10))
        tab = ttk.Frame(self.left_nb, padding=(0, 6))
        self.left_nb.add(tab, text="待识别图片")
        self.queue_title = ttk.Label(tab, text="图片队列", font=(self.family, 12, "bold"))
        self.queue_title.pack(anchor="w", pady=(0, 12))
        self.pending_tree = self._tree(tab, ("file", "status", "count", "quality"),
            (("file", "图片", 148), ("status", "状态", 66), ("count", "题数", 42), ("quality", "质量", 70)),
            selectmode="extended", displaycolumns=("file", "status"))
        self.pending_tree.bind("<<TreeviewSelect>>", lambda e: self.on_pending_select())
        self.pending_tree.bind("<Double-1>", lambda e: self.start_recognition(only_selected=True))
        row = ttk.Frame(tab)
        row.pack(fill="x", side="bottom", pady=(10, 0), before=self.pending_tree.master)
        ttk.Button(row, text="优化图片", command=self.load_into_editor).pack(side="left")
        more = ttk.Menubutton(row, text="图片操作 ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="本地重新识别", command=lambda: self.recognize_selected(force="local"))
        menu.add_command(label="云端重新识别", command=lambda: self.recognize_selected(force="cloud"))
        menu.add_separator()
        menu.add_command(label="移除选中图片…", command=self.remove_selected_pending)
        more["menu"] = menu
        more.pack(side="right")

        tab = ttk.Frame(self.left_nb, padding=(0, 6))
        self.left_nb.add(tab, text="题库")
        self.question_count = ttk.Label(tab, text="全部题目", font=(self.family, 12, "bold"))
        self.question_count.pack(anchor="w", pady=(0, 10))
        filt = ttk.Frame(tab)
        filt.pack(fill="x", pady=(0, 6))
        self.filter_status = tk.StringVar(value="")
        self.filter_subject = tk.StringVar(value="")
        self.filter_knowledge = tk.StringVar(value=KP_ALL)
        self._kp_map = {KP_ALL: ""}
        self.filter_status_label = tk.StringVar(value="全部状态")
        self._filter_names = {"全部状态": "", "错题": "wrong", "正确": "correct", "未做": "blank"}
        cb = ttk.Combobox(filt, textvariable=self.filter_status_label, width=9, state="readonly",
                          values=list(self._filter_names))
        cb.pack(side="left", fill="x", expand=True, padx=(0, 6))
        cb.bind("<<ComboboxSelected>>", lambda _: self._apply_status_filter())
        self.filter_subject_label = tk.StringVar(value="全部学科")
        self.subject_combo = ttk.Combobox(filt, textvariable=self.filter_subject_label, width=9, state="readonly",
                                          values=["全部学科"] + prompts.SUBJECTS)
        self.subject_combo.pack(side="left", fill="x", expand=True)
        self.subject_combo.bind("<<ComboboxSelected>>", lambda _: self._apply_subject_filter())
        ttk.Label(tab, text="知识点", style="Muted.TLabel").pack(anchor="w", pady=(4, 4))
        self.kp_combo = ttk.Combobox(tab, textvariable=self.filter_knowledge, state="readonly", values=[KP_ALL])
        self.kp_combo.pack(fill="x", pady=(0, 12))
        self.kp_combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_questions())
        self.q_tree = self._tree(tab, ("uid", "subject", "kp", "qtype", "status", "stem"),
            (("uid", "题号", 100), ("subject", "学科", 48), ("kp", "知识点", 85),
             ("qtype", "题型", 65), ("status", "状态", 50), ("stem", "题目", 160)),
             displaycolumns=("subject", "kp", "stem"), selectmode="extended")
        self.q_tree.bind("<<TreeviewSelect>>", lambda _: self.on_question_select())
        self.q_tree.column("subject", width=44, stretch=False)
        self.q_tree.column("kp", width=80, stretch=False)
        self.q_tree.column("stem", width=110, minwidth=40)
        for name in ("wrong", "correct", "blank"):
            self.q_tree.tag_configure(name, foreground=COLORS[name])
        actions = ttk.Menubutton(tab, text="题目操作 ▾")
        menu = tk.Menu(actions, tearoff=False)
        menu.add_command(label="查看 / 分析", command=self.on_question_select)
        menu.add_command(label="归档选中题目", command=self.archive_selected)
        menu.add_command(label="删除选中题目…", command=self.delete_selected_questions)
        actions["menu"] = menu
        actions.pack(anchor="w", side="bottom", pady=(10, 0), before=self.q_tree.master)

        tab = ttk.Frame(self.left_nb, padding=(0, 6))
        self.left_nb.add(tab, text="归档目录")
        ttk.Label(tab, text="归档文件", font=(self.family, 12, "bold")).pack(anchor="w", pady=(0, 12))
        self.archive_tree = ttk.Treeview(tab, show="tree")
        self.archive_tree.pack(fill="both", expand=True)
        ttk.Button(tab, text="刷新目录", command=self.refresh_archive_tree).pack(anchor="w", pady=(10, 0))

    def _build_center(self, parent) -> None:
        self.nb = ttk.Notebook(parent, style="Workspace.TNotebook")
        parent.add(self.nb, weight=4)
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="识别与归档")
        self.result_paned = ttk.Panedwindow(tab, orient="horizontal")
        self.result_paned.pack(fill="both", expand=True)
        self.preview_frame = ttk.Frame(self.result_paned)
        self.result_paned.add(self.preview_frame, weight=3)
        self.welcome = WelcomePanel(self.preview_frame, self.import_images)
        self.welcome.pack(fill="both", expand=True)
        self.preview = ImageCanvas(self.preview_frame, bg="#eef2f8")
        self.preview_info = ttk.Label(self.preview_frame, text="选择一张图片，查看预览", style="Muted.TLabel")
        self.preview_nav = ttk.Frame(self.preview_frame)
        for text, cmd in (("← 上一张", lambda: self.step_pending(-1)), ("下一张 →", lambda: self.step_pending(1)),
                          ("优化图片", self.load_into_editor)):
            ttk.Button(self.preview_nav, text=text, command=cmd).pack(side="left", padx=(0, 6))
        self.result_box = ttk.Frame(self.result_paned, width=380, padding=(14, 0, 0, 0))
        self.result_paned.add(self.result_box, weight=3)
        self._build_result_editor(self.result_box)
        self.result_paned.forget(self.result_box)
        self.editor = EditorPanel(self.nb, on_apply=self.on_editor_applied)
        self.nb.add(self.editor, text="图片预处理")
        self.report = ReportPanel(self.nb, on_generate=self.generate_report, on_open_image=self._open)
        self.nb.add(self.report, text="错题分析")

        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="全局搜索")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 14))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=40)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _: self.do_search())
        ttk.Button(bar, text="搜索题目", style="Primary.TButton", command=self.do_search).pack(side="left", padx=(8, 0))
        ttk.Label(tab, text="输入题干、答案或知识点，也能搜索分析报告中的内容。", style="Muted.TLabel").pack(anchor="w")
        self.search_status_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.search_status_var, style="Muted.TLabel").pack(anchor="w", pady=6)
        self.search_tree = self._tree(tab, ("uid", "subject", "qtype", "status", "kp", "stem"),
            (("uid", "题号", 120), ("subject", "学科", 65), ("qtype", "题型", 75),
             ("status", "状态", 65), ("kp", "知识点", 160), ("stem", "题目", 400)), selectmode="browse")
        self.search_tree.bind("<<TreeviewSelect>>", lambda _: self.on_search_select())
        self.search_text = RichText(tab, height=9)
        self.search_text.pack(fill="x", pady=(12, 0))

        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="统计看板")
        cards = ttk.Frame(tab)
        cards.pack(fill="x", pady=(0, 14))
        self.card_images = StatCard(cards, "图片总数")
        self.card_questions = StatCard(cards, "题目总数")
        self.card_wrong = StatCard(cards, "错题数", color=COLORS["wrong"])
        self.card_correct = StatCard(cards, "正确题", color=COLORS["correct"])
        self.card_blank = StatCard(cards, "未做题", color=COLORS["blank"])
        self.card_reports = StatCard(cards, "分析报告")
        for i, card in enumerate((self.card_images, self.card_questions, self.card_wrong,
                                  self.card_correct, self.card_blank, self.card_reports)):
            cards.columnconfigure(i, weight=1, uniform="stat")
            card.grid(row=0, column=i, sticky="nsew", padx=4)
        charts = ttk.Frame(tab)
        charts.pack(fill="both", expand=True)
        charts.columnconfigure((0, 1), weight=1, uniform="charts")
        charts.rowconfigure((0, 1), weight=1)
        self.chart_subject = ChartCanvas(charts, width=380, height=230)
        self.chart_type = ChartCanvas(charts, width=380, height=230)
        self.chart_weak = ChartCanvas(charts, width=380, height=230)
        self.chart_cause = ChartCanvas(charts, width=380, height=230)
        for i, chart in enumerate((self.chart_subject, self.chart_type, self.chart_weak, self.chart_cause)):
            chart.grid(row=i//2, column=i%2, sticky="nsew", padx=8, pady=8)
        self.stats_empty = ttk.Label(tab, text="还没有学习记录。导入并识别习题后，这里会显示你的薄弱知识点。",
                                      style="Muted.TLabel", anchor="center")
        self.stats_empty.pack(fill="x", pady=6)

        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="运行日志")
        self.log_text = RichText(tab, height=24, font_family="Consolas")
        self.log_text.pack(fill="both", expand=True)
        history = logging_setup.read_log_file(max_chars=24000)
        if history.strip() and history != "（暂无日志文件）":
            self.log_text.append(history.rstrip())
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="清空显示", command=self.log_text.clear).pack(side="left")
        ttk.Button(row, text="打开日志文件", command=self._open_log_file).pack(side="left", padx=6)
        ttk.Button(row, text="复制全部日志", command=self.copy_logs).pack(side="left", padx=6)

    def _apply_status_filter(self):
        self.filter_status.set(self._filter_names.get(self.filter_status_label.get(), ""))
        self.refresh_questions()

    def _apply_subject_filter(self):
        value = self.filter_subject_label.get()
        self.filter_subject.set("" if value == "全部学科" else value)
        self.refresh_questions()

    def _focus_search(self):
        self.navigate("search")
        self.search_entry.focus_set()

    def navigate(self, page, animate=True):
        if self._switching:
            return
        self._switching = True
        try:
            self._page = page
            if page == "report":
                self.toolbar_card.pack_forget()
            else:
                self.toolbar_card.pack(fill="x", before=self.workspace_card)
            left, center = {"import": (0, 0), "library": (1, 0), "report": (1, 2), "stats": (1, 4),
                            "search": (1, 3), "editor": (0, 1), "archive": (2, 0), "logs": (0, 5)}[page]
            self.left_nb.select(left)
            self.nb.select(center)
            titles = {
                "import": ("把错题，变成下一次的进步。", "导入 → 识别 → 整理，三步收好每一道题。"),
                "library": ("我的题库", "按学科和知识点查找，选中题目即可查看与修改。"),
                "report": ("读懂错因，才算真正学会。", "先选一道题，再查看解题步骤、避错技巧和变式练习。"),
                "stats": ("看见自己的每一点进步。", "从练习记录中，找到下一次复习的重点。"),
                "search": ("想找的那道题，就在这里。", "题干、答案、知识点与报告，一次搜索。"),
                "editor": ("让习题照片更清楚。", "先选择图片，再裁剪、纠偏或提亮，保存后即可用于识别。"),
                "archive": ("每一道题，都有自己的位置。", "查看按学科、知识点和状态整理的归档文件。"),
                "logs": ("运行记录", "查看任务进度和诊断信息。")}
            self.page_title.configure(text=titles[page][0])
            self.page_hint.configure(text=titles[page][1], fg=COLORS["muted"])
            self._page_hint_default = titles[page][1]
            if self.busy and self._task_current:
                self._refresh_task_hint()
            for key, button in self._nav_buttons.items():
                button.set_active(key == page)
            self.import_actions.pack_forget()
            self.btn_recognize.pack_forget()
            self.btn_analyze.pack_forget()
            self.mode_label.pack_forget()
            if page in ("import", "editor"):
                self.import_actions.pack(side="left")
                self.btn_recognize.pack(side="left", padx=6)
            elif page == "library":
                self.btn_analyze.pack(side="left")
            else:
                self.mode_label.pack(side="left", padx=6)
            self._sync_layout()
            if page in ("library", "report") and not self.current_question_uid:
                items = self.q_tree.get_children()
                if items:
                    if page == "report":
                        items = tuple(i for i in items if "wrong" in self.q_tree.item(i, "tags")) or items
                    self.q_tree.selection_set(items[0])
                    self.on_question_select()
            if animate and self.animations_enabled:
                self._animate_heading()
        finally:
            self._switching = False

    def _animate_heading(self):
        # A short foreground fade keeps the page stable and keyboard focus intact.
        from .softui import blend
        if getattr(self, "_heading_job", None):
            self.after_cancel(self._heading_job)
        def tick(frame=0):
            self._heading_job = None
            self.page_title.configure(fg=blend("#a6b1c8", COLORS["text"], min(1, frame/9)))
            if frame < 9:
                self._heading_job = self.after(16, tick, frame+1)
        tick()

    def _on_center_page(self, _event=None):
        if self._switching:
            return
        idx = self.nb.index("current")
        desired = {1: "editor", 2: "report", 3: "search", 4: "stats", 5: "logs"}.get(idx)
        if idx == 0 and self._page not in ("import", "library", "archive"):
            desired = "library" if self.current_question_uid else "import"
        if desired and desired != self._page:
            self.navigate(desired)

    def _on_left_page(self, _event=None):
        if not self._switching and self.nb.index("current") == 0:
            desired = {0: "import", 1: "library", 2: "archive"}[self.left_nb.index("current")]
            if desired != self._page:
                self.navigate(desired)

    def _sync_layout(self):
        page = self._page
        self.btn_recognize.configure(state="normal" if self.pending and not self.busy else "disabled")
        self.btn_analyze.configure(state="normal" if self.q_tree.get_children() and not self.busy else "disabled")
        show_left = page in ("library", "report", "archive") or (page in ("import", "editor") and bool(self.pending))
        panes = tuple(str(x) for x in self.paned.panes())
        if show_left and str(self.left_frame) not in panes:
            self.paned.insert(0, self.left_frame, weight=1)
            self.after(50, self._size_left_pane)
        elif not show_left and str(self.left_frame) in panes:
            self.paned.forget(self.left_frame)
        if show_left:
            self.after(50, self._size_left_pane)
        show_result = bool(getattr(self, "_result_questions", [])) or (
            page in ("library", "archive") and bool(self.current_question_uid))
        if page == "import" and not self.pending:
            show_result = False
        panes = tuple(str(x) for x in self.result_paned.panes())
        if show_result and str(self.result_box) not in panes:
            self.result_paned.add(self.result_box, weight=3)
            self.after(100, self._size_result_pane)
        elif not show_result and str(self.result_box) in panes:
            self.result_paned.forget(self.result_box)
        show_preview = not show_result or page != "library" or self._compare_image
        panes = tuple(str(x) for x in self.result_paned.panes())
        if show_preview and str(self.preview_frame) not in panes:
            if self.result_paned.panes():
                self.result_paned.insert(0, self.preview_frame, weight=3)
            else:
                self.result_paned.add(self.preview_frame, weight=3)
            self.after(100, self._size_result_pane)
        elif not show_preview and str(self.preview_frame) in panes:
            self.result_paned.forget(self.preview_frame)
        if page == "import" and len(getattr(self, "_result_questions", [])) > 1:
            self.result_list.master.pack(fill="x", after=self.result_hint)
        else:
            self.result_list.master.pack_forget()
        has_image = self.preview._image is not None and not (page == "import" and not self.pending)
        self.welcome.pack_forget()
        self.preview.pack_forget()
        self.preview_info.pack_forget()
        self.preview_nav.pack_forget()
        if has_image:
            self.preview.pack(fill="both", expand=True)
            self.preview_info.pack(anchor="w", pady=(10, 4))
            self.preview_nav.pack(fill="x", pady=(4, 0))
        elif page == "import":
            self.welcome.pack(fill="both", expand=True)
        elif page == "library":
            # An empty library still offers an immediate way to add its first question.
            self.welcome.pack(fill="both", expand=True)

    def _size_left_pane(self):
        if str(self.left_frame) in tuple(str(x) for x in self.paned.panes()):
            self.paned.sashpos(0, min(340 if self._page in ("library", "report") else 275,
                                     max(250, int(self.paned.winfo_width()*.30))))
            if len(self.result_paned.panes()) > 1:
                self.after(60, self._size_result_pane)

    def _size_result_pane(self):
        if len(self.result_paned.panes()) > 1:
            self.result_paned.sashpos(0, max(240, int(self.result_paned.winfo_width()*.46)))

    def _toggle_comparison(self):
        self._compare_image = not self._compare_image
        self._sync_layout()

    def _build_result_editor(self, parent) -> None:
        """右侧「识别结果」区：可直接修改并保存回数据库。"""
        wrap = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        wrap.pack(fill="both", expand=True)
        self.result_title = ttk.Label(wrap, text="题目详情", style="Panel.TLabel", font=(self.family, 12, "bold"))
        self.result_title.pack(anchor="w")
        self.result_hint = ttk.Label(wrap, text="核对识别内容，修改后记得保存。",
                                     style="Panel.TLabel", foreground=COLORS["muted"],
                                     wraplength=380)
        self.result_hint.pack(anchor="w", pady=(0, 6))

        listwrap = ttk.Frame(wrap)
        listwrap.pack(fill="x")
        self.result_list = ttk.Treeview(listwrap, columns=("idx", "type", "status", "conf"),
                                       show="headings", height=3, selectmode="browse")
        for c, t, w in (("idx", "题号", 50), ("type", "题型", 80), ("status", "状态", 70),
                        ("conf", "置信度", 60)):
            self.result_list.heading(c, text=t)
            self.result_list.column(c, width=w, anchor="center")
        self.result_list.pack(fill="x")
        self.result_list.bind("<<TreeviewSelect>>", lambda e: self.on_result_select())

        self.question_preview = RichText(wrap, height=14, size=11)
        self.question_preview.text.configure(width=30)
        self.question_preview.pack(fill="both", expand=True, pady=8)
        self.result_scroll = ScrollableFrame(wrap)
        form = self.result_scroll.body
        self.field_vars: Dict[str, tk.Variable] = {}
        self.field_widgets: Dict[str, tk.Widget] = {}

        def row(label: str, key: str, values=None, height=None, parent=form):
            f = ttk.Frame(parent)
            f.pack(fill="x", pady=5)
            ttk.Label(f, text=label, width=8).pack(side="left", anchor="n", pady=6)
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

        row("状态", "status", values=list(STATUS_CN.values()))
        row("题干", "stem", height=5)
        row("我的答案", "student_answer")
        row("参考答案", "correct_answer", height=3)
        row("知识点", "knowledge")
        self.details_open = tk.BooleanVar(value=False)
        self.details_panel = ttk.Frame(form)
        ttk.Checkbutton(form, text="展开更多信息（学科、选项、解题步骤与标签）",
                        variable=self.details_open, command=self._toggle_details).pack(anchor="w", pady=(10, 4))
        details = self.details_panel
        row("学科", "subject", values=prompts.SUBJECTS, parent=details)
        row("细分学科", "subject_detail", parent=details)
        row("学段", "grade", values=prompts.GRADES, parent=details)
        row("题型", "qtype", values=prompts.QTYPES, parent=details)
        row("选项", "options", height=3, parent=details)
        row("我的步骤", "student_steps", height=3, parent=details)
        f = ttk.Frame(details)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="标签", width=8).pack(side="left", anchor="n")
        self.tag_editor = TagEditor(f, on_change=self.on_tags_changed)
        self.tag_editor.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(8, 0))
        self.btn_edit_question = ttk.Button(btns, text="修改识别内容", style="Primary.TButton", command=self.toggle_question_edit)
        self.btn_edit_question.pack(side="left")
        self.btn_save_question = ttk.Button(btns, text="保存修改", style="Primary.TButton", command=self.save_question_edits)
        self.btn_view_report = ttk.Button(btns, text="查看分析", width=8, command=lambda: self.navigate("report"))
        self.btn_view_report.pack(side="left", padx=6)
        more = ttk.Menubutton(btns, text="更多 ▾", width=5)
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="生成 / 更新分析", command=lambda: self.generate_report(self.current_question_uid, False))
        menu.add_command(label="导出本题…", command=self.export_current_question)
        menu.add_command(label="打开原图", command=self.open_current_image)
        menu.add_command(label="显示 / 收起原图对照", command=self._toggle_comparison)
        more["menu"] = menu
        more.pack(side="right")

    def _toggle_details(self):
        if self.details_open.get():
            self.details_panel.pack(fill="x", pady=4)
        else:
            self.details_panel.pack_forget()

    def _render_question_preview(self, q):
        blocks = [("h2", "题干"), ("para", q.get("stem") or "尚未识别到题干")]
        if q.get("options"):
            blocks += [("h2", "选项"), ("para", "\n".join(q["options"]))]
        blocks += [("h2", "我的答案"), ("para", q.get("student_answer") or "未作答"),
                   ("h2", "参考答案"), ("para", q.get("correct_answer") or "暂无参考答案")]
        if q.get("knowledge_points"):
            blocks += [("h2", "知识点"), ("para", "、".join(str(k) for k in q["knowledge_points"]))]
        self.question_preview.write_blocks(blocks)

    def _show_question_mode(self):
        self.question_preview.pack_forget()
        self.result_scroll.pack_forget()
        self.btn_save_question.pack_forget()
        self.btn_view_report.pack_forget()
        if self._editing_question:
            self.result_scroll.pack(fill="both", expand=True, pady=8, before=self.btn_edit_question.master)
            self.btn_edit_question.configure(text="查看排版预览", style="TButton")
            self.btn_save_question.pack(side="left", after=self.btn_edit_question, padx=(6, 0))
            self.result_hint.configure(text="修改后点「保存修改」。公式源码保留 LaTeX，点「查看排版预览」核对效果。")
        else:
            self.question_preview.pack(fill="both", expand=True, pady=8, before=self.btn_edit_question.master)
            self.btn_edit_question.configure(text="修改识别内容", style="Primary.TButton")
            self.btn_view_report.pack(side="left", after=self.btn_edit_question, padx=6)
            self.result_hint.configure(text="先核对排版后的题目；需要改字时，点「修改识别内容」。")

    def toggle_question_edit(self):
        if not self.current_question_uid:
            messagebox.showinfo("提示", "请先选择一道题")
            return
        if self._editing_question:
            self._render_question_preview(self._read_form())
        self._editing_question = not self._editing_question
        self._show_question_mode()

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
        # Persist once; the registered logging listener delivers the same line to the UI.
        log.info("%s", text)

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log(msg[1])
                elif kind == "log_raw":
                    self.log_text.append(str(msg[1]))
                elif kind == "stage":
                    if self._task_current:
                        self._task_current["stage"] = str(msg[1])
                        self._refresh_task_hint()
                    else:
                        self.status.set(str(msg[1]))
                elif kind == "status":
                    self.status.set(str(msg[1]))
                    if str(msg[1]).startswith("已导出"):
                        self._completion_notice = str(msg[1])
                elif kind == "progress":
                    _, done, total, text = msg
                    self.status.set_progress(done, total, text)
                elif kind == "recognized":
                    self._on_recognized(msg[1])
                elif kind == "image_started":
                    self._on_image_started(*msg[1:])
                elif kind == "report_done":
                    self._on_report_done(msg[1], msg[2])
                elif kind == "done":
                    self._set_busy(False, self._completion_notice or (str(msg[1]) if len(msg) > 1 else "完成"))
                elif kind == "error":
                    self._set_busy(False, "出错")
                    messagebox.showerror("出错了", str(msg[1]))
        except queue.Empty:
            pass
        self._queue_job = self.after(120, self._drain_queue)

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
        if busy:
            self._completion_notice = ""
            self.btn_stop.pack(side="left", padx=6)
            self.status.start_activity(text)
        else:
            self.btn_stop.pack_forget()
            self.status.stop_activity()
            self.report.set_busy(False)
            self._task_current = None
            if self._task_tick_job:
                self.after_cancel(self._task_tick_job)
                self._task_tick_job = None
            self.page_hint.configure(text=getattr(self, "_page_hint_default", ""))
        if not busy:
            self.status.set(text or "就绪")
            if text and text != "出错":
                self.status.progress.configure(value=100)
                self.page_hint.configure(text=text, fg=COLORS["primary_dark"])
            # 整批跑完后，状态栏右侧明确写出"这次实际用了哪个引擎"
            engines = []
            for e in self._engines_used:
                if e not in engines:
                    engines.append(e)
            if engines:
                self.status.detail.set("本次识别：" + "、".join(engines))
            else:
                self.status.detail.set(self._engine_hint())
            self._engines_used = []
        self._sync_layout()

    def _on_image_started(self, uid, index, total, name):
        self._task_current = {"uid": uid, "index": index, "total": total, "name": name,
                              "stage": "准备识别", "started": time.monotonic()}
        for item in self.pending:
            if item["uid"] == uid:
                item["status"] = "识别中…"
                break
        self.refresh_pending()
        self._refresh_task_hint()

    def _refresh_task_hint(self):
        if self._task_tick_job:
            self.after_cancel(self._task_tick_job)
            self._task_tick_job = None
        task = self._task_current
        if not task or not self.busy:
            return
        seconds = max(0, int(time.monotonic()-task["started"]))
        name = task["name"]
        if len(name) > 30:
            name = name[:27]+"…"
        context = f"第 {task['index']}/{task['total']} 张 · {name} · {task['stage']} · 已用 {seconds} 秒"
        self.page_hint.configure(text=context)
        self.status.set(context)
        self._task_tick_job = self.after(1000, self._refresh_task_hint)

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
            self.navigate("import")

    # ------------------------------------------------------------------ #
    # 待处理列表
    # ------------------------------------------------------------------ #
    def refresh_pending(self) -> None:
        self.queue_title.configure(text=f"图片队列 · {len(self.pending)} 张")
        self._sync_layout()
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
        self._sync_layout()
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
            self._post(("image_started", item["uid"], i, total, item["name"]))
            started = time.monotonic()
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
            self._post(("log", f"{item['name']} 识别结束：{len(uids)} 道题，用时 {time.monotonic()-started:.1f} 秒"
                        + (f"；{outcome.error}" if outcome.error else "")))
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
            analysis_total = sum(q.get("status") == "wrong" for q in outcome.questions)
            analysis_index = 0
            for q, uid in zip(outcome.questions, uids):
                if q.get("status") != "wrong":
                    continue
                analysis_index += 1
                self._post(("stage", f"生成错题分析 {analysis_index}/{analysis_total}"))
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
        self._task_current = None
        # 记住"这张图是谁识别的"，等整批跑完再在状态栏右侧统一显示
        # （不能在这里直接 set detail：随后的进度事件 "已完成 i/N" 会把它覆盖掉）
        if outcome.engine:
            self._engines_used.append(str(outcome.engine))
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
        self._result_questions = questions
        self._sync_layout()
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
        if self._loaded_form_uid != str(q.get("uid", "")):
            self._editing_question = False
        self._loaded_form_uid = str(q.get("uid", ""))
        values = {
            "subject": q.get("subject", ""),
            "subject_detail": q.get("subject_detail", ""),
            "grade": q.get("grade", ""),
            "qtype": q.get("qtype", ""),
            "status": STATUS_CN.get(q.get("status"), ""),
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
        self._render_question_preview(q)
        self._show_question_mode()
        self.result_title.configure(text=f"题目详情　{q.get('uid', '')}　"
                                         f"{prompts.status_cn(str(q.get('status', '')))}")
        # 同步到错题分析页
        report = self.store.get_report(str(q.get("uid", ""))) if q.get("uid") else None
        self.report.show(q, report)
        self.preview_question_image(q)
        self._sync_layout()

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
            "status": prompts.normalize_status(get("status")),
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
                self.report.show(q, self.store.get_report(self.current_question_uid))
                self._render_question_preview(q)
                self._editing_question = False
                self._show_question_mode()
                self.archiver.archive_question(q, self.store.get_report(self.current_question_uid))
        except Exception as exc:
            self.log(f"归档文字稿更新失败：{exc}")
        self.status.set("修改已保存")
        self.page_hint.configure(text="修改已保存，可继续核对下一道题。", fg=COLORS["primary_dark"])

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
        self.question_count.configure(text=f"题目 · {len(questions)} 道")
        self.filter_subject_label.set(self.filter_subject.get() or "全部学科")
        reverse_status = {v: k for k, v in self._filter_names.items()}
        self.filter_status_label.set(reverse_status.get(self.filter_status.get(), "全部状态"))
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
        use_cloud = bool(cloud) and self.cfg.cloud_enabled()
        if use_cloud:
            engine_text = "将使用云端深度分析。"
        elif cloud:
            engine_text = "云端当前不可用（没填 API Key 或没开「云端增强」），只能用本地模型生成。"
        elif self.cfg.cloud_enabled() and self.cfg.get("run.cloud_for_analysis", True):
            engine_text = "将优先使用云端深度分析（可在「设置 → 运行模式」里关闭）。"
        else:
            engine_text = f"使用本地模型（{self.cfg.get('local.model')}）离线生成。"
        if not messagebox.askyesno("批量分析", f"为 {len(missing)} 道还没有报告的错题生成分析？\n{engine_text}"):
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
        self._post(("status", f"已导出：{os.path.basename(path)}"))

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
        self.library_count.set(f"{s.get('questions', 0)} 道题目 · {s.get('wrong', 0)} 道待巩固")
        self.stats_empty.configure(text="还没有学习记录。导入并识别习题后，这里会显示你的薄弱知识点。" if not s.get("questions") else "")
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
        # 状态栏右侧也要跟着模式走，否则切到「云端优先」后它仍写着本地模型。
        # 注意：工具栏先于状态栏创建，构造过程中这里可能还没有 self.status。
        status = getattr(self, "status", None)
        if status is not None:
            status.detail.set(self._engine_hint())

    def open_settings(self) -> None:
        def on_saved():
            self.cfg.save()
            self.pipeline = Pipeline(self.cfg, on_log=self._log_threadsafe, on_stage=self._stage_threadsafe)
            self.analyzer = Analyzer(self.cfg, on_log=self._log_threadsafe)
            self.archiver = Archiver(self.cfg, self.store)
            self._refresh_mode_labels()
            self.animations_enabled = bool(self.cfg.get("ui.animations", True))
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
        self.library_count.set(f"{s.get('questions', 0)} 道题目 · {s.get('wrong', 0)} 道待巩固")
        self.stats_empty.configure(text="还没有学习记录。导入并识别习题后，这里会显示你的薄弱知识点。" if not s.get("questions") else "")
        cloud_ok = self.cfg.cloud_enabled()
        cloud_model = str((self.cfg.active_cloud() or {}).get("model", "") or "—")
        messagebox.showinfo("关于", (
            f"{paths.APP_NAME}　v{paths.APP_VERSION}\n\n"
            f"运行模式：{self.cfg.mode_label()}\n"
            f"实际使用：{self._engine_hint()}\n"
            + ("本地模型：本次不使用（云端优先模式）\n" if self.cfg.mode() == "cloud_first"
               else f"本地模型：{self.cfg.get('local.model') or '—'}\n")
            + f"云端模型：{cloud_model if cloud_ok else '未启用（缺 API Key 或没打开云端增强）'}\n"
            f"题库：{s.get('questions', 0)} 道题（错题 {s.get('wrong', 0)}）\n"
            f"加密自检：{'通过' if self_test() else '异常'}\n"
            f"数据目录：{paths.data_dir()}\n\n"
            + ("题库与报告只存在本机；识别时会把（压缩后的）图片发给上面配置的云端服务商。"
               if cloud_ok else
               "本软件完全本地运行，无付费项目、无广告、无会员限制。")
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

    def copy_logs(self):
        self.clipboard_clear()
        # Log paths and filenames are literal text, not mathematical expressions.
        self.clipboard_append(self.log_text.text.get("1.0", "end-1c"))
        self.status.set("日志已复制")

    def _on_close(self) -> None:
        # Own this Tcl interpreter: cancel child redraws and layout timers as well.
        for job in self.tk.call("after", "info"):
            # Tcl cancels the timer; each owning widget removes its own command on destroy.
            self.tk.call("after", "cancel", job)
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
