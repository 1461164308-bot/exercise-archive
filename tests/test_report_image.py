"""Original-image selection, stale-preview clearing and responsive report layout."""
import os
import gc
from pathlib import Path
import tkinter as tk
import unittest

from exerarchive import imageops
from exerarchive.ui.reportview import ReportPanel
from exerarchive.ui.widgets import install_style
from tests._util import scratch, cleanup


class ReportImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(scratch('report_image'))
        self.original = self.tmp/'original.png'
        self.processed = self.tmp/'processed.png'
        self.original.write_bytes(imageops.encode_png(120, 180, bytes([210, 100, 110])*120*180))
        self.processed.write_bytes(imageops.encode_png(160, 100, bytes([80, 130, 200])*160*100))
        self.root = tk.Tk()
        self.root.geometry('1040x760+0+0')
        install_style(self.root, 'Microsoft YaHei UI')
        self.opened = []
        self.panel = ReportPanel(self.root, on_open_image=self.opened.append)
        self.panel.pack(fill='both', expand=True)
        self.root.update()

    def tearDown(self):
        for job in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', job)
        self.root.destroy()
        self.panel = None
        self.root = None
        gc.collect()
        cleanup(str(self.tmp))

    def question(self, **kw):
        return dict(uid='Q-original', subject='数学', qtype='选择题', status='wrong',
                    stem='求函数的极小值', image_path=str(self.original), work_path=str(self.processed), **kw)

    def test_original_photo_is_used_and_opened(self):
        self.panel.show(self.question(), None)
        self.root.update()
        self.assertEqual(self.panel.image_path, str(self.original))
        self.assertEqual(self.panel.image_preview._image.pixel(0, 0), (210, 100, 110))
        self.panel.open_image()
        self.assertEqual(self.opened, [str(self.original)])
        self.assertIn('原始照片', self.panel.image_info['text'])

    def test_missing_original_falls_back_to_processed(self):
        self.original.unlink()
        self.panel.show(self.question(), None)
        self.assertEqual(self.panel.image_path, str(self.processed))
        self.assertIn('预处理图片', self.panel.image_info['text'])

    def test_corrupt_original_falls_back_and_missing_both_clears_previous(self):
        self.original.write_bytes(b'not an image')
        self.panel.show(self.question(), None)
        self.assertEqual(self.panel.image_path, str(self.processed))
        self.original.unlink()
        self.processed.unlink()
        q = self.question()
        q['uid'] = 'Q-without-image'
        self.panel.show(q, None)
        self.assertEqual(self.panel.image_path, '')
        self.assertIsNone(self.panel.image_preview._image)
        self.assertEqual(str(self.panel.image_open['state']), 'disabled')
        self.assertIn('未找到', self.panel.image_message['text'])
        self.panel.open_image()
        self.assertEqual(self.opened, [])

    def test_hide_restore_and_small_window_keep_report(self):
        """v1.2.0 布局契约：原图与报告**永远并排**（report 第 0 行第 0 列、原图第 0 行第 1 列），
        窄窗只是把原图**默认收起**并收窄，不再上下叠放挤占正文高度。"""
        self.panel.show(self.question(), {'restate': '分析报告正文保持可见'})
        self.root.update()
        # 宽窗：并排，原图在第 1 列
        self.assertEqual(int(self.panel.image_frame.grid_info()['column']), 1)
        self.assertEqual(int(self.panel.image_frame.grid_info()['row']), 0)
        self.assertEqual(int(self.panel.report_frame.grid_info()['row']), 0)
        self.assertEqual(int(self.panel.report_frame.grid_info()['column']), 0)
        self.panel.toggle_image()
        self.root.update()
        self.assertFalse(self.panel.image_frame.winfo_ismapped())
        self.assertIn('分析报告正文保持可见', self.panel.text.get_text())
        # 窄窗（600px < 650 阈值）：报告独占宽度，展开原图后仍是并排、不叠放
        self.root.geometry('600x720+0+0')
        self.panel.toggle_image()
        self.root.update()
        self.assertEqual(int(self.panel.image_frame.grid_info()['row']), 0)
        self.assertEqual(int(self.panel.image_frame.grid_info()['column']), 1)
        self.assertEqual(int(self.panel.report_frame.grid_info()['row']), 0)
        self.assertEqual(int(self.panel.report_frame.grid_info()['column']), 0)
        self.assertGreater(self.panel.report_frame.winfo_height(), 200)
        self.assertIn('分析报告正文保持可见', self.panel.text.get_text())

    def test_narrow_window_collapses_image_by_default_and_can_expand(self):
        """窄窗默认收起原图（首轮实测正文只剩 22px），点「查看原图」仍可并排展开。"""
        self.panel.pack_forget()
        self.panel.destroy()
        self.root.geometry('600x720+0+0')
        self.root.update()
        self.panel = ReportPanel(self.root, on_open_image=self.opened.append)
        self.panel.pack(fill='both', expand=True)
        self.root.update()
        self.panel.show(self.question(), {'restate': '窄窗也要能读分析'})
        self.root.update()
        self.assertFalse(self.panel.image_frame.winfo_ismapped(), '窄窗应默认收起原图')
        self.assertEqual(self.panel.image_toggle.cget('text'), '查看原图')
        self.assertGreater(self.panel.report_frame.winfo_height(), 200, '正文应拿到高度')
        self.panel.toggle_image()
        self.root.update()
        self.assertTrue(self.panel.image_frame.winfo_ismapped(), '点「查看原图」应能展开')
        self.assertEqual(self.panel.image_toggle.cget('text'), '收起原图')
        self.assertGreater(self.panel.report_frame.winfo_height(), 200, '展开后正文仍要够高')

    def test_image_removed_after_preview_does_not_open_stale_file(self):
        self.panel.show(self.question(), None)
        self.original.unlink()
        self.panel.open_image()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.panel.image_path, str(self.processed))

    def test_recognized_stem_and_options_are_separate_from_analysis(self):
        q = self.question()
        q['stem'] = '题干来自识别结果，请选择正确答案。'
        q['options'] = ['A. 第一个选项', 'B. 第二个选项']
        self.panel.show(q, {'restate': '这是一段分析概述，不是原始题干。'})
        self.root.update()
        recognized = self.panel.question_text.get_text()
        self.assertIn(q['stem'], recognized)
        self.assertIn('A. 第一个选项', recognized)
        self.assertIn('B. 第二个选项', recognized)
        self.assertNotIn('分析概述', recognized)
        self.assertTrue(self.panel.question_text.winfo_ismapped())
        self.panel.show({'uid': 'Q-next', 'stem': '下一道题的新题干。'}, None)
        self.assertIn('下一道题的新题干', self.panel.question_text.get_text())
        self.assertNotIn('第一个选项', self.panel.question_text.get_text())

    def test_empty_stem_is_explained_without_inventing_text(self):
        self.panel.show({'uid': 'Q-no-stem', 'stem': ''}, {'restate': '模型的分析概述'})
        self.assertIn('尚未识别到题干', self.panel.question_text.get_text())
        self.assertNotIn('模型的分析概述', self.panel.question_text.get_text())


if __name__ == '__main__':
    unittest.main()
