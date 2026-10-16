# -*- coding: utf-8 -*-
"""v1.2.0 学生体验回归测试（由复测轮次新增）。

这些断言对应首轮学生体验报告里的具体问题，防止回退：
* 错题分析正文高度（首轮实测只有 22px，学生看不见）
* 题库默认排版预览、源码只在编辑模式、列表不出现 LaTeX 源码
* 预览草稿不写库、保存后回到阅读模式并同步分析页
* 图片优化保存按钮在小窗口也可见
* 导出完成提示带文件名
* 运行日志落盘、日志页**路径与下划线保真**、copy_logs 剪贴板保真、不重复回流
* 识别中的任务提示：第几张/文件名/阶段/已用秒数，阶段变化不丢文件名

只依赖 Tk 与本地数据，不调用模型。

夹具注意（踩过的坑）：
1. 入库后必须 ``refresh_all`` 并选中题目，否则 ``toggle_question_edit`` 会弹
   「请先选择一道题」的模态框把测试挂死；
2. 因此这里把所有 messagebox 换成 AssertionError —— 测试里弹窗即失败，绝不干等；
3. ``logging_setup.setup()`` 在首次调用时就固定了日志文件路径，
   而每个用例都会换 ``EXER_HOME``，所以夹具把文件 handler 重定向到本用例目录，
   用例结束再还原，避免断言到错路径、也避免污染别的用例。
"""
from __future__ import annotations

import gc
import logging
import logging.handlers
import os
import threading
import time
import unittest
from tkinter import messagebox

import tkinter.filedialog as filedialog

from tests._util import scratch, cleanup
from exerarchive import config, imageops
from exerarchive import mathfmt
from exerarchive.ui.app import MainWindow

LOG = logging.getLogger("ui.app")


class ExperienceTest(unittest.TestCase):
    def setUp(self):
        self.previous_home = os.environ.get("EXER_HOME")
        self.tmp = scratch("ui_experience")
        os.environ["EXER_HOME"] = self.tmp
        # load_config() 是**进程级单例**，它的 _path 在第一次加载时就被定住了。
        # 整个套件里前面的测试模块早就加载过配置，所以换过 EXER_HOME 之后必须
        # reload，否则 self.app.cfg 还指着上一个模块的目录（写/读的不是同一个文件）。
        config.load_config(reload=True)
        cfg = config.load_config()
        cfg.set("local.auto_start", False)
        cfg.set("ui.animations", False)
        cfg.save()
        self._redirect_file_log()
        self._forbid_dialogs()
        self.app = MainWindow()
        self.app.geometry("1366x768+0+0")
        self.flush()

    def tearDown(self):
        self.app._on_close()
        self.app = None
        gc.collect()
        self._restore_dialogs()
        self._restore_file_log()
        if self.previous_home is None:
            os.environ.pop("EXER_HOME", None)
        else:
            os.environ["EXER_HOME"] = self.previous_home
        cleanup(self.tmp)

    # -- 夹具：日志与弹窗 ---------------------------------------------- #
    def _redirect_file_log(self) -> None:
        """把根日志的文件 handler 指到本用例目录，返回时可还原。"""
        self._log_restore = None
        target = os.path.join(self.tmp, "data", "logs", "app.log")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                previous = handler.baseFilename
                handler.close()
                handler.baseFilename = target
                handler.stream = handler._open()
                self._log_restore = (handler, previous)
                break
        self.log_path = target

    def _restore_file_log(self) -> None:
        if not self._log_restore:
            return
        handler, previous = self._log_restore
        try:
            handler.close()
            handler.baseFilename = previous
            handler.stream = handler._open()
        except Exception:
            pass
        self._log_restore = None

    def _forbid_dialogs(self) -> None:
        """测试里出现模态框 = 逻辑走到了不该走的分支，直接失败而不是挂死。"""
        self._dialogs = {name: getattr(messagebox, name) for name in
                         ("showinfo", "showerror", "showwarning", "askyesno", "askokcancel")}

        def boom(title="", message="", **kw):
            raise AssertionError("测试中弹出了模态框：%s / %s" % (title, message))

        for name in self._dialogs:
            setattr(messagebox, name, boom)

    def _restore_dialogs(self) -> None:
        for name, func in getattr(self, "_dialogs", {}).items():
            setattr(messagebox, name, func)

    # -- 工具 ---------------------------------------------------------- #
    def flush(self, seconds: float = 0.25):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(.01)

    def seed(self, stem: str, **kw) -> str:
        """入库一道题并刷新界面（**必须刷新**，否则题库是空的、选不中）。"""
        img = os.path.join(self.tmp, "page.png")
        with open(img, "wb") as f:
            f.write(imageops.encode_png(240, 320, bytes([200, 205, 215]) * 240 * 320))
        image_uid = self.app.store.add_image(img, original_name="page.png")
        data = {"subject": "数学", "grade": "大学", "qtype": "填空题", "stem": stem,
                "status": "wrong", "student_answer": "我的作答", "correct_answer": "参考答案",
                "knowledge_points": ["不定积分"]}
        data.update(kw)
        uid = self.app.store.add_question(data, self.app.store.image_id(image_uid))
        self.app.refresh_all()
        self.flush(0.3)
        return uid

    def select(self, uid: str) -> None:
        """打开题库并选中该题（等价于学生点列表里那一行）。"""
        self.app.navigate("library")
        self.flush(0.4)
        self.assertIn(uid, self.app.q_tree.get_children(), "题目应出现在题库列表里")
        self.app.q_tree.selection_set(uid)
        self.app.on_question_select()
        self.flush(0.4)
        self.assertEqual(self.app.current_question_uid, uid)

    def seed_with_report(self, stem: str) -> str:
        uid = self.seed(stem)
        self.app.store.save_report(uid, {"restate": "错因概述" * 40, "steps": ["第一步", "第二步"],
                                         "pitfalls": ["易错点"], "variants": ["变式一"]})
        self.app.refresh_all()
        self.flush(0.3)
        return uid

    # -- 1. 分析页正文高度 --------------------------------------------- #
    def test_report_body_has_usable_height_at_both_sizes(self):
        """首轮 P0：1366x768 下正文只有 22px。要求两种尺寸都 ≥180px。"""
        uid = self.seed_with_report(r"$\int 1/(1 + x^2) dx = $")
        self.select(uid)
        for geom in ("1366x768+0+0", "1120x720+0+0"):
            self.app.geometry(geom)
            self.flush(0.8)
            self.app.navigate("report")
            self.flush(0.8)
            self.assertEqual(self.app.report.uid, uid)
            height = self.app.report.text.winfo_height()
            self.assertGreaterEqual(
                height, 180,
                "分析正文高度 %dpx 过小（%s）—— 首轮就是被上方内容挤成 22px" % (height, geom))

    def test_report_image_collapses_by_default_on_narrow_and_keeps_height(self):
        """窄窗（内容区 <650px）默认收起原图，展开后正文仍要够高。"""
        uid = self.seed_with_report("求 AB。")
        self.select(uid)
        self.app.navigate("report")
        self.app.geometry("1120x720+0+0")
        self.flush(0.9)
        report = self.app.report
        if report.content.winfo_width() < 650:
            self.assertFalse(report.image_frame.winfo_ismapped(), "窄窗应默认收起原图")
            self.assertEqual(report.image_toggle.cget("text"), "查看原图")
            self.assertGreaterEqual(report.text.winfo_height(), 180)
            report.toggle_image()
            self.flush(0.6)
            self.assertTrue(report.image_frame.winfo_ismapped(), "点「查看原图」应能展开")
            self.assertGreaterEqual(report.text.winfo_height(), 180, "展开后正文仍要够高")

    # -- 2. 题库默认排版预览 ------------------------------------------- #
    def test_library_preview_is_readable_and_source_hidden(self):
        """默认不得出现源码表单；预览与列表都不应出现 $ 或 \\begin{pmatrix}。"""
        stem = r"求 AB。$A=\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$"
        uid = self.seed(stem)
        self.select(uid)
        self.assertFalse(self.app._editing_question)
        self.assertTrue(self.app.question_preview.winfo_ismapped())
        self.assertFalse(self.app.result_scroll.winfo_ismapped())
        preview = self.app.question_preview.get_text()
        self.assertNotIn("$", preview)
        self.assertNotIn("\\begin", preview)
        self.assertIn("求 AB", preview)
        # 列表列：调用 format_for_export，不应残留源码
        shown = str(self.app.q_tree.item(uid, "values")[-1])
        self.assertEqual(shown, mathfmt.format_for_export(stem)[:60])
        self.assertNotIn("$", shown)
        self.assertNotIn("pmatrix", shown)

    def test_preview_draft_does_not_write_database_then_save_syncs_report(self):
        """点「查看排版预览」只渲染草稿；保存后才落库并同步分析页。"""
        uid = self.seed("原始题干")
        self.select(uid)
        before = self.app.store.get_question(uid)["stem"]
        self.app.toggle_question_edit()
        self.flush()
        self.assertTrue(self.app.result_scroll.winfo_ismapped())
        widget = self.app.field_widgets["stem"]
        widget.delete("1.0", "end")
        widget.insert("1.0", "草稿题干（未保存）")
        self.app.toggle_question_edit()          # 回阅读模式，只渲染草稿
        self.flush()
        self.assertIn("草稿题干（未保存）", self.app.question_preview.get_text())
        self.assertEqual(self.app.store.get_question(uid)["stem"], before,
                         "预览草稿不得写进数据库")
        # 真正保存
        self.app.toggle_question_edit()
        self.flush()
        widget = self.app.field_widgets["stem"]
        widget.delete("1.0", "end")
        widget.insert("1.0", "核对后的题干")
        self.app.save_question_edits()
        self.flush(0.8)
        self.assertEqual(self.app.store.get_question(uid)["stem"], "核对后的题干")
        self.assertFalse(self.app._editing_question, "保存后应回到阅读模式")
        self.assertIn("已保存", self.app.page_hint.cget("text"))
        self.app.navigate("report")
        self.flush(0.6)
        self.assertIn("核对后的题干", self.app.report.question_text.get_text())

    # -- 3. 图片优化保存按钮 ------------------------------------------- #
    def test_editor_save_button_visible_at_both_sizes(self):
        """保存按钮固定在工具区底部，两种窗口尺寸都要完整可见。"""
        self.app.navigate("editor")
        self.flush(0.6)
        for geom in ("1366x768+0+0", "1120x720+0+0"):
            self.app.geometry(geom)
            self.flush(0.8)
            button = self.app.editor.save_button
            self.assertTrue(button.winfo_ismapped())
            bottom = button.winfo_rooty() + button.winfo_height()
            win_bottom = self.app.winfo_rooty() + self.app.winfo_height()
            self.assertLessEqual(bottom, win_bottom,
                                 "保存按钮底部 %d 超出窗口 %d（%s）" % (bottom, win_bottom, geom))

    def test_settings_save_button_is_always_visible(self):
        """用户反馈「设置中改换设置无保存选项」：按钮栏被挤没了。

        根因：Notebook 先 pack 且 ``expand=True``，而它的**请求高度 771px** 大于窗口
        （700px）；Tk 的 pack 按调用顺序分配空间，于是按钮栏被压成 **1 像素**、
        「保存」的 ``winfo_ismapped()`` 为 False（实测）。现在按钮栏先以
        ``side="bottom"`` 占位，标签页内容放进 ``ScrollableFrame``（小窗口可滚动）。
        """
        from exerarchive.ui.settingsdlg import SettingsDialog

        dialog = SettingsDialog(self.app, self.app.cfg)
        try:
            for geom in ("860x700+40+40", "700x560+40+40", "760x600+40+40"):
                dialog.geometry(geom)
                self.flush(0.8)
                bar = dialog.save_button.master
                self.assertGreaterEqual(
                    bar.winfo_height(), 30,
                    "设置对话框的按钮栏被压扁（曾出现高度 1px，见 %s）" % geom)
                self.assertTrue(dialog.save_button.winfo_ismapped(),
                                "「保存」按钮没有显示（%s）" % geom)
                bottom = dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height()
                self.assertLessEqual(
                    bottom, dialog.winfo_rooty() + dialog.winfo_height(),
                    "「保存」按钮超出对话框底边（%s）" % geom)
            # 每个标签页都要有可滚动容器，小窗口下底部选项才够得着
            notebook = [child for child in dialog.winfo_children()
                        if child.winfo_class() == "TNotebook"][0]
            tabs = notebook.tabs()
            self.assertEqual(len(tabs), 7, "设置标签页数量变了：%s" % (tabs,))
            for tab_id in tabs:
                page = dialog.nametowidget(tab_id)
                self.assertTrue(hasattr(page, "body"),
                                "标签页 %s 的内容没放进可滚动容器" % tab_id)
        finally:
            dialog.destroy()

    def test_settings_dialog_save_actually_takes_effect(self):
        """用户反馈「无法正常改设置」：在对话框里改完点保存，配置必须真的生效。

        根因：``_collect()`` 交出来的是**扁平点号键**补丁，而 ``Config.update()``
        以前把它们当普通键塞进顶层（影子键），``get()`` 读的嵌套值从未改变 ——
        于是日志还写着"设置已保存"，实际一点没改。这里从**磁盘**读回来验证。

        另外把 Tk 的回调异常收下来：``Button.invoke()`` 会把回调里的异常交给
        ``report_callback_exception`` **吞掉**，不检查的话测试会误判成"没保存"。
        """
        import json
        from pathlib import Path

        from exerarchive import config as config_mod
        from exerarchive.ui.settingsdlg import SettingsDialog

        callback_errors = []
        self.app.report_callback_exception = lambda *args: callback_errors.append(args)

        dialog = SettingsDialog(self.app, self.app.cfg)
        try:
            dialog._var("run.confidence_threshold").set("0.55")
            dialog.save_button.invoke()
            self.flush(0.6)
        finally:
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except Exception:
                pass
        self.assertEqual(callback_errors, [], "保存设置时回调里抛了异常（被 Tk 吞掉了）")
        # 直接看保存对话框写出去的那个文件（不依赖进程级单例）
        cfg_path = Path(self.app.cfg.path)
        self.assertTrue(cfg_path.exists(), "配置文件没有写出来：%s" % cfg_path)
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(float(saved["run"]["confidence_threshold"]), 0.55,
                         "嵌套段没被更新（多半又写成顶层影子键了）：%s" % cfg_path)
        self.assertNotIn("run.confidence_threshold", saved, "又写出了影子键")
        # 重新加载后读到的值也必须是 0.55
        on_disk = config_mod.load_config(reload=True).get("run.confidence_threshold")
        self.assertEqual(float(on_disk), 0.55, "设置保存后没有生效")

    # -- 4. 导出提示 --------------------------------------------------- #
    def test_export_notice_contains_filename(self):
        uid = self.seed("导出的题目")
        self.app.store.save_report(uid, {"restate": "概述"})
        self.app.refresh_all()
        self.flush(0.3)
        out_dir = os.path.join(self.tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        real = filedialog.asksaveasfilename
        filedialog.asksaveasfilename = lambda **kw: os.path.join(
            out_dir, kw.get("initialfile", "导出.docx"))
        try:
            self.app.export_dialog("wrong")
            waited = 0.0
            while waited < 60 and getattr(self.app, "busy", False):
                self.app.update()
                time.sleep(.1)
                waited += .1
            self.flush(0.8)
        finally:
            filedialog.asksaveasfilename = real
        files = sorted(os.listdir(out_dir))
        self.assertTrue(files, "导出应产生文件")
        name = files[-1]
        self.assertIn(name, self.app.page_hint.cget("text"), "完成提示要带文件名")
        self.assertIn(name, self.app.status.msg.get())

    # -- 5. 日志保真 --------------------------------------------------- #
    def test_log_page_preserves_windows_path_and_underscore_filename(self):
        """首轮复测踩过的坑：不能用 RichText.get_text() 判定日志显示。

        日志里的 ``\\Users`` 会被数学转义吃掉、``_2026`` 会变下标，
        因此这里读 **Text 原始内容**，并用 copy_logs 的剪贴板内容复核。
        """
        path = r"C:\Users\14611\Documents\导出结果\个人错题本_20261016.docx"
        LOG.info("导出完成：%s", path)
        self.flush(0.6)
        raw = self.app.log_text.text.get("1.0", "end-1c")
        self.assertIn(path, raw, "日志页原始文本必须保留完整路径")
        self.assertIn("个人错题本_20261016.docx", raw, "文件名下划线不得被转成下标")
        self.app.copy_logs()
        self.flush(0.3)
        clip = self.app.clipboard_get()
        self.assertIn(path, clip, "复制日志也要保留完整路径")
        self.assertIn("个人错题本_20261016.docx", clip)
        self.assertNotIn("₂", clip, "剪贴板内容不应出现下标字符")

    def test_log_written_to_disk_without_duplicate_reflux(self):
        """GUI 操作要落盘（写进**当前实际生效**的日志文件），且不得重复回流到界面。"""
        uid = self.seed("日志用题目")
        self.select(uid)
        size_before = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        self.app.toggle_question_edit()
        self.flush(0.4)
        widget = self.app.field_widgets["stem"]
        widget.delete("1.0", "end")
        widget.insert("1.0", "改了题干用于写日志")
        self.app.save_question_edits()
        self.flush(0.8)
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        self.assertTrue(os.path.exists(self.log_path), "夹具应已把日志文件指到用例目录")
        size_after = os.path.getsize(self.log_path)
        self.assertGreater(size_after, size_before, "GUI 操作必须写进磁盘日志")
        with open(self.log_path, encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertIn("已保存 %s" % uid, on_disk)
        self.app.navigate("logs")
        self.flush(0.6)
        raw = self.app.log_text.text.get("1.0", "end-1c")
        self.assertEqual(raw.count("已保存 %s" % uid), 1, "同一条日志不应重复回流到界面")

    # -- 6. 识别中的任务提示 ------------------------------------------- #
    def test_task_hint_shows_index_name_stage_and_elapsed(self):
        """首轮 P1：等待时只看到笼统「本地识别 0/3」。要求带第几张/文件名/阶段/秒数。"""
        self.app._set_busy(True, "处理中…")
        self.app._on_image_started("Q-task", 2, 5, "u3_math_integral_fill.jpg")
        self.app._task_current["started"] = time.monotonic() - 7
        self.app._refresh_task_hint()
        hint = self.app.page_hint.cget("text")
        self.assertIn("第 2/5 张", hint)
        self.assertIn("u3_math_integral_fill.jpg", hint)
        self.assertIn("已用", hint)
        self.assertEqual(self.app.status.msg.get(), hint, "底栏应与页头显示同一串信息")
        # 阶段切换（识别 → 生成错题分析）不得把文件名冲掉
        self.app._task_current["stage"] = "生成错题分析 1/1"
        self.app._refresh_task_hint()
        hint2 = self.app.page_hint.cget("text")
        self.assertIn("生成错题分析 1/1", hint2)
        self.assertIn("u3_math_integral_fill.jpg", hint2, "阶段变化不能丢掉文件名")
        self.app._set_busy(False, "识别完成")
        self.flush(0.3)

    # -- 7. 「选了云端优先，界面却到处显示小模型在工作」 ------------------ #
    def _cloud_first_cfg(self, api_key: str = "sk-test") -> None:
        self.app.cfg.update({
            "run.mode": "cloud_first",
            "cloud.enabled": True,
            "cloud.profiles": [{"name": "模拟云端", "base_url": "https://example.invalid/v1",
                                "model": "qwen-vl-max", "api_key": api_key,
                                "supports_vision": True}],
            "cloud.active": 0,
        })

    def test_engine_hint_follows_the_mode_instead_of_always_local(self):
        """状态栏右侧必须说清"实际会用哪个模型"，而不是永远写本地模型。

        用户反馈：选了「云端优先」，界面上却到处显示小模型在工作。
        """
        self._cloud_first_cfg()
        self.app._refresh_mode_labels()
        hint = self.app._engine_hint()
        self.assertIn("云端", hint)
        self.assertIn("qwen-vl-max", hint)
        self.assertNotIn("本地模型", hint, "云端优先时不该还写着本地模型：%s" % hint)
        self.assertEqual(self.app.status.detail.get(), hint, "状态栏右侧要跟模式一致")
        # 云端优先但没填 Key：要说清"云端未配置"，别让人以为是本地的问题
        self._cloud_first_cfg(api_key="")
        hint2 = self.app._engine_hint()
        self.assertIn("未配置", hint2)
        self.assertIn("设置", hint2, "要给出下一步动作：%s" % hint2)
        # 纯本地 / 混合
        self.app.cfg.update({"run.mode": "local_only"})
        self.assertIn("本地模型", self.app._engine_hint())
        self.app.cfg.update({"cloud.profiles": [{"name": "c", "base_url": "https://x/v1",
                                                 "model": "m", "api_key": "sk"}],
                             "run.mode": "hybrid"})
        both = self.app._engine_hint()
        self.assertIn("本地", both)
        self.assertIn("云端", both)

    def test_cloud_first_startup_does_not_claim_local_model_is_needed(self):
        """云端优先 + 云端可用时：不拉起本地服务，启动日志要说明原因。"""
        self._cloud_first_cfg()
        self.app.cfg.set("local.auto_start", True)
        started = []
        real_thread = threading.Thread

        class FakeThread:
            def __init__(self, *a, **kw):
                started.append(kw.get("target"))

            def start(self):
                pass

        threading.Thread = FakeThread
        try:
            self.app._startup_message()
        finally:
            threading.Thread = real_thread
        self.assertEqual([t for t in started if t], [], "云端优先时不该再去拉本地模型服务")
        self.assertNotIn("本地模型服务未就绪", self.app.status.msg.get())

    def test_finished_batch_reports_which_engine_actually_ran(self):
        """整批跑完后状态栏右侧要写"本次识别：<引擎>"，且不被进度计数覆盖。"""
        self._cloud_first_cfg()
        self.app._engines_used = ["cloud:qwen-vl-max", "cloud:qwen-vl-max"]
        self.app._set_busy(False, "识别完成")
        self.flush(0.3)
        self.assertEqual(self.app.status.detail.get(), "本次识别：cloud:qwen-vl-max")
        self.assertEqual(self.app._engines_used, [], "用过的引擎列表应清空")
        # 混合来源时要去重并都列出来
        self.app._engines_used = ["local:qwen3-vl:4b", "cloud:qwen-vl-max", "local:qwen3-vl:4b"]
        self.app._set_busy(False, "识别完成")
        detail = self.app.status.detail.get()
        self.assertIn("local:qwen3-vl:4b", detail)
        self.assertIn("cloud:qwen-vl-max", detail)
        self.assertEqual(detail.count("local:qwen3-vl:4b"), 1, "同一引擎只列一次")

    def test_report_source_hint_is_readable_chinese_with_engine(self):
        """分析页"报告来源"不能把内部代码(rule/local/cloud)直接甩给学生。"""
        from exerarchive.ui.reportview import ReportPanel

        panel = ReportPanel(self.app, on_open_image=lambda *_: None)
        try:
            panel.pack(fill="both", expand=True)
            self.flush(0.3)
            panel.show({"uid": "Q-x", "stem": "题干", "status": "wrong"},
                       {"depth": "cloud", "engine": "cloud:qwen-vl-max", "restate": "概述"})
            self.flush(0.3)
            text = panel.hint.cget("text")
            self.assertIn("云端模型生成", text)
            self.assertIn("cloud:qwen-vl-max", text)
            self.assertNotIn("报告来源：cloud", text, "不该直接显示内部代码：%s" % text)
            panel.show({"uid": "Q-y", "stem": "题干", "status": "wrong"},
                       {"depth": "rule", "restate": "概述"})
            self.flush(0.3)
            self.assertIn("本地规则生成", panel.hint.cget("text"))
        finally:
            panel.destroy()


if __name__ == "__main__":
    unittest.main()
