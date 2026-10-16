"""Task navigation, compact layouts and Chinese status round trips with a real Tk window."""
import os
import gc
import time
import unittest

from tests._util import scratch, cleanup
from exerarchive import config, imageops
from exerarchive.ui.app import MainWindow


class SoftLayoutTest(unittest.TestCase):
    def setUp(self):
        self.previous_home = os.environ.get("EXER_HOME")
        self.tmp = scratch("soft_layout")
        os.environ["EXER_HOME"] = self.tmp
        # load_config() 是进程级单例，_path 在第一次加载时就定住了；套件里前面的
        # 模块早已加载过配置，所以换过 EXER_HOME 必须 reload，否则 app 的配置
        # 还指向上一个模块的目录（读写不是同一个文件）。
        config.load_config(reload=True)
        cfg = config.load_config()
        cfg.set("local.auto_start", False)
        cfg.set("ui.animations", False)
        cfg.save()
        self.app = MainWindow()
        self.app.geometry("1120x720+0+0")
        self.flush()

    def tearDown(self):
        self.app._on_close()
        self.app = None
        gc.collect()
        if self.previous_home is None:
            os.environ.pop("EXER_HOME", None)
        else:
            os.environ["EXER_HOME"] = self.previous_home
        cleanup(self.tmp)

    def flush(self):
        end = time.monotonic() + .24
        while time.monotonic() < end:
            self.app.update()
            time.sleep(.01)

    def seed(self):
        image_uid = self.app.store.add_image(os.path.join(self.tmp, 'sample.png'), original_name='sample.png')
        image_id = self.app.store.image_id(image_uid)
        uid = self.app.store.add_question({"subject": "数学", "grade": "高中", "qtype": "选择题",
            "stem": "求函数的极小值", "status": "wrong", "options": ["A. 0", "B. -2"],
            "student_answer": "A", "correct_answer": "B", "knowledge_points": ["导数"]}, image_id)
        self.app.refresh_all()
        return uid

    def test_empty_import_has_one_clear_start(self):
        self.assertEqual(self.app._page, "import")
        self.assertTrue(self.app.welcome.winfo_ismapped())
        self.assertFalse(self.app.left_frame.winfo_ismapped())
        self.assertFalse(self.app.result_box.winfo_ismapped())

    def test_library_opens_editable_question_at_small_size(self):
        """v1.2.0：题库默认是**排版预览**（阅读模式），点「修改识别内容」才出现源码表单。

        注意：`field_widgets["stem"].winfo_ismapped()` 在这种 ScrollableFrame 结构下不可靠
        （预览模式下它同样返回 1），所以这里用 result_scroll / question_preview 的
        mapped 状态与按钮文字来判断当前模式。
        """
        uid = self.seed()
        self.app.navigate("library")
        self.flush()
        self.assertEqual(self.app.current_question_uid, uid)
        self.assertTrue(self.app.result_box.winfo_ismapped())
        self.assertGreater(self.app.result_box.winfo_width(), 400)
        # 默认：阅读模式（排版预览可见，源码表单未挂载）
        self.assertFalse(self.app._editing_question)
        self.assertTrue(self.app.question_preview.winfo_ismapped())
        self.assertFalse(self.app.result_scroll.winfo_ismapped())
        self.assertEqual(self.app.btn_edit_question.cget("text"), "修改识别内容")
        self.assertIn("求函数的极小值", self.app.question_preview.get_text())
        self.assertFalse(self.app.preview_frame.winfo_ismapped())
        self.assertFalse(self.app.details_panel.winfo_ismapped())
        self.assertEqual(self.app.filter_subject_label.get(), "全部学科")
        self.app.filter_subject_label.set("英语")
        self.app._apply_subject_filter()
        self.assertEqual(self.app.q_tree.get_children(), ())
        self.app.filter_subject_label.set("全部学科")
        self.app._apply_subject_filter()
        self.assertEqual(self.app.q_tree.get_children(), (uid,))
        # 点「修改识别内容」→ 源码表单出现，可以改字
        self.app.toggle_question_edit()
        self.flush()
        self.assertTrue(self.app._editing_question)
        self.assertTrue(self.app.result_scroll.winfo_ismapped())
        self.assertFalse(self.app.question_preview.winfo_ismapped())
        self.assertEqual(self.app.btn_edit_question.cget("text"), "查看排版预览")
        self.app.details_open.set(True)
        self.app._toggle_details()
        self.flush()
        self.assertTrue(self.app.details_panel.winfo_ismapped())

    def test_chinese_status_saves_database_enum(self):
        uid = self.seed()
        self.app.navigate("library")
        self.flush()
        self.assertEqual(self.app.field_vars["status"].get(), "错题")
        self.app.field_vars["status"].set("正确")
        patch = self.app._read_form()
        self.assertEqual(patch["status"], "correct")
        self.app.store.update_question(uid, patch)
        self.assertEqual(self.app.store.get_question(uid)["status"], "correct")
        self.app.filter_status_label.set("正确")
        self.app._apply_status_filter()
        self.assertEqual(self.app.q_tree.get_children(), (uid,))

    def test_navigation_preserves_page_and_hides_unneeded_list(self):
        self.seed()
        for page, index in (("library", 0), ("report", 2), ("stats", 4), ("search", 3),
                            ("logs", 5), ("editor", 1), ("import", 0)):
            self.app.navigate(page)
            self.flush()
            self.assertEqual(self.app._page, page)
            self.assertEqual(self.app.nb.index("current"), index)
            if page in ("stats", "search", "logs"):
                self.assertFalse(self.app.left_frame.winfo_ismapped())

    def test_centered_preview_crop_maps_to_original_pixels(self):
        canvas = self.app.preview
        img = imageops.Image(400, 300, bytearray([240] * (400*300*3)))
        canvas.set_image(img, max_side=400)
        self.app._sync_layout()
        self.flush()
        canvas._fit_canvas()
        ox, oy = canvas._offset
        self.assertEqual(canvas._to_image_coords(ox, oy), (0, 0))
        x, y = canvas._to_image_coords(ox+200*canvas._scale, oy+150*canvas._scale)
        self.assertLessEqual(abs(x-200), 1)
        self.assertLessEqual(abs(y-150), 1)
        self.assertEqual(canvas._to_image_coords(-1000, -1000), (0, 0))

    def test_reduced_motion_has_no_running_animation(self):
        self.app.navigate("stats")
        self.app.status.start_activity("测试任务")
        self.assertFalse(self.app.animations_enabled)
        self.assertIsNone(getattr(self.app, "_heading_job", None))
        self.assertEqual(self.app.status.progress["value"], 0)
        self.app.status.set_progress(1, 2)
        self.assertEqual(self.app.status.progress["value"], 50)

    def test_close_cancels_pending_child_animations(self):
        self.app.animations_enabled = True
        button = self.app._nav_buttons["library"]
        button._target(True)
        self.app.chart_subject._resize_chart(None)
        self.app.status.start_activity("测试关闭时的进度条")
        self.app.status.set_progress(1, 3)
        self.assertIsNotNone(button._job)
        # tearDown immediately closes the window while child-owned timers are pending.

    def test_report_selection_switches_original_image(self):
        """切题时原图必须跟着换。

        v1.2.0 起窄窗（报告内容区 <650px）**默认收起原图**把高度让给分析正文，
        所以这里不能再用「原图控件是否可见」当判据，要看**实际载入的图片内容**。
        """
        first = self.seed()
        original = os.path.join(self.tmp, 'sample.png')
        with open(original, 'wb') as f:
            f.write(imageops.encode_png(120, 180, bytes([210, 100, 110])*120*180))
        second_path = os.path.join(self.tmp, 'second.png')
        with open(second_path, 'wb') as f:
            f.write(imageops.encode_png(160, 100, bytes([80, 130, 200])*160*100))
        image_uid = self.app.store.add_image(second_path)
        second = self.app.store.add_question({'stem': '另一道错题', 'status': 'wrong', 'subject': '数学'},
                                              self.app.store.image_id(image_uid))
        self.app.refresh_questions()
        self.app.navigate('report')
        for uid, path, color in ((first, original, (210, 100, 110)),
                                 (second, second_path, (80, 130, 200))):
            self.app.q_tree.selection_set(uid)
            self.app.on_question_select()
            self.flush()
            self.assertEqual(self.app.report.uid, uid)
            self.assertEqual(self.app.report.image_path, path)
            loaded = self.app.report.image_preview._image
            self.assertIsNotNone(loaded, '原图应已载入（即使当前被收起）')
            self.assertEqual(loaded.pixel(0, 0), color, '切题后原图内容必须是新题目的图')
            # 收起/展开都必须能拿到同一张图：收起是布局行为，不是没加载
            if not self.app.report.image_frame.winfo_ismapped():
                self.app.report.toggle_image()
                self.flush()
                self.assertTrue(self.app.report.image_frame.winfo_ismapped(),
                                '点「查看原图」应能展开')
                self.app.report.toggle_image()
                self.flush()

    def test_saved_stem_is_refreshed_in_report(self):
        self.seed()
        self.app.navigate('library')
        self.flush()
        stem = self.app.field_widgets['stem']
        stem.delete('1.0', 'end')
        stem.insert('1.0', '人工核对后的题干。')
        self.app.save_question_edits()
        self.assertIn('人工核对后的题干', self.app.report.question_text.get_text())


if __name__ == "__main__":
    unittest.main()
