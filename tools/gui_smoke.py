# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真的把窗口开起来、跑完整流程、逐页截图留证。

用法::

    python tools/gui_smoke.py                # 建窗口 → 导入样张 → 识别 → 逐页截图
    python tools/gui_smoke.py --no-recognize # 只验证界面能构建与切换各页
    python tools/gui_smoke.py --keep         # 跑完不关窗口（人工看一眼）

验证内容：
1. 主窗口、全部页签、各面板能正常构建（tkinter 控件写错在这里就会炸）；
2. 导入图片 → 识别 → 入库 → 归档 → 错题分析 全链路跑通（跨线程 + 消息队列）；
3. 统计看板 / 搜索 / 题库 / 归档目录树确实渲染出了内容（不是空壳）；
4. 每页截图保存到 ``verify/gui-*.png``，可直接打开查看排版。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_WINAPI_READY = False
_user32 = None
_gdi32 = None


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def _declare_winapi() -> None:
    """声明 Win32 句柄相关函数的参数/返回类型。

    坑：不声明 argtypes 时 ctypes 会把 Python int 当成 32 位 c_int 传，
    而 Windows 句柄是 64 位（实测 CreateCompatibleDC 返回 0xFFFFFFFFFFxxxxxx 这种值），
    于是间歇抛 ``OverflowError: int too long to convert`` —— 必须显式声明。
    """
    global _WINAPI_READY, _user32, _gdi32
    if _WINAPI_READY:
        return
    _user32 = ctypes.WinDLL("user32")
    _gdi32 = ctypes.WinDLL("gdi32")
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.GetWindowRect.restype = ctypes.c_int
    _user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    _user32.PrintWindow.restype = ctypes.c_int
    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.c_uint32]
    _gdi32.CreateDIBSection.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteObject.restype = ctypes.c_int
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.restype = ctypes.c_int
    _gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                              ctypes.c_uint32]
    _gdi32.BitBlt.restype = ctypes.c_int
    _WINAPI_READY = True


def capture_hwnd(hwnd: int, path: str, use_screen: bool = True) -> bool:
    """把窗口内容截成 PNG。

    默认用**屏幕抓取**（BitBlt 屏幕 DC）：先把窗口抬到最前，抓出来的就是它真实的显示效果；
    实测 ``PrintWindow`` 在 Tk 窗口上会在切换页签后间歇抓到空白，所以只作为兜底。
    """
    if sys.platform != "win32":
        return False
    try:
        _declare_winapi()
        rect = _RECT()
        if not _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return False
        x, y = rect.left, rect.top
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return False

        hdc_screen = _user32.GetDC(None)
        hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        bmi = _BMIH()
        bmi.biSize = ctypes.sizeof(_BMIH)
        bmi.biWidth = w
        bmi.biHeight = -h          # 负值 = 自顶向下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        bits = ctypes.c_void_p(0)
        hbmp = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        _gdi32.SelectObject(hdc, hbmp)
        try:
            if use_screen:
                _gdi32.BitBlt(hdc, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)
                raw = ctypes.string_at(bits, w * h * 4)
                if _is_blank(raw):
                    # 屏幕上没抓到内容（窗口被遮挡/最小化），退回 PrintWindow
                    if not _user32.PrintWindow(ctypes.c_void_p(hwnd), hdc, 2):
                        return False
                    raw = ctypes.string_at(bits, w * h * 4)
            else:
                if not _user32.PrintWindow(ctypes.c_void_p(hwnd), hdc, 2):
                    _gdi32.BitBlt(hdc, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)
                raw = ctypes.string_at(bits, w * h * 4)
            from exerarchive import gdiplus

            rgb = bytearray(w * h * 3)
            for yy in range(h):
                row = raw[yy * w * 4 : (yy + 1) * w * 4]
                o = yy * w * 3
                rgb[o : o + w * 3 : 3] = row[2::4]
                rgb[o + 1 : o + w * 3 : 3] = row[1::4]
                rgb[o + 2 : o + w * 3 : 3] = row[0::4]
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            gdiplus.save_image(w, h, bytes(rgb), path, "png")
            return True
        finally:
            _gdi32.DeleteObject(hbmp)
            _gdi32.DeleteDC(hdc)
            _user32.ReleaseDC(None, hdc_screen)
    except Exception as exc:
        print(f"  截图失败：{type(exc).__name__}: {exc}")
        return False


def _is_blank(raw: bytes, sample_step: int = 997) -> bool:
    """粗判截图是否全白/全黑（被遮挡时常见）。"""
    seen = set()
    for i in range(0, len(raw), sample_step * 4):
        seen.add(raw[i : i + 3])
        if len(seen) > 3:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="GUI 冒烟测试")
    ap.add_argument("--no-recognize", action="store_true", help="不跑识别，只验证界面")
    ap.add_argument("--import", dest="import_n", type=int, default=0,
                    help="只导入前 N 张样张、不做识别（用来验证预览/编辑器能显示图片，无需显卡）")
    ap.add_argument("--keep", action="store_true", help="跑完保留窗口")
    ap.add_argument("--samples", default=os.path.join(ROOT, "samples"), help="测试用样张目录")
    ap.add_argument("--use-real-data", action="store_true",
                    help="用真实 data/ 目录跑（默认用临时目录，绝不动你的题库）")
    args = ap.parse_args()

    # 报告里有 ✅/❌，输出被重定向或接进管道时中文 Windows 是 GBK，先切 UTF-8 再打印
    from exerarchive import logging_setup

    logging_setup.enable_utf8_console()

    # 默认把数据目录切到临时位置：这个脚本会导入图片、跑识别、写题库，
    # 早期版本直接用真实 data/，结果"验证一下"就把用户自己的题库一起写进去了。
    if not args.use_real_data:
        os.environ["EXER_HOME"] = os.path.join(ROOT, "verify", "_guismoke")
    print(f"数据目录：{os.environ.get('EXER_HOME') or '（真实 data/，已显式指定 --use-real-data）'}")

    from exerarchive.ui.app import MainWindow

    print("1) 构建主窗口…")
    app = MainWindow()
    # 截图要求整窗都在屏幕内：窗口被屏幕边缘裁掉时抓到的会是空白
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry(f"{min(1400, sw - 40)}x{min(880, sh - 80)}+10+10")
    app.update()
    tabs = [app.nb.tab(i, "text") for i in range(len(app.nb.tabs()))]
    print(f"   窗口尺寸 {app.winfo_width()}x{app.winfo_height()}（屏幕 {sw}x{sh}），页签 {len(tabs)} 个："
          f"{', '.join(tabs)}")

    out_dir = os.path.join(ROOT, "verify")
    hwnd = app.winfo_id()
    shots = []

    def shot(name: str) -> None:
        """切页后立刻截图（必须紧跟 update，不然截到的还是上一页）。"""
        app.lift()
        try:
            app.attributes("-topmost", True)
        except Exception:
            pass
        app.update_idletasks()
        app.update()
        time.sleep(0.45)
        app.update()
        path = os.path.join(out_dir, f"gui-{name}.png")
        ok = capture_hwnd(hwnd, path)
        if not ok:
            ok = capture_hwnd(hwnd, path, use_screen=False)
        if ok:
            print(f"   已截图：{path}（{os.path.getsize(path)} 字节）")
            shots.append(path)

    def tour() -> None:
        """逐页切换并截图，顺带断言各页确实有内容。"""
        for idx, name in enumerate(tabs):
            app.nb.select(idx)
            if idx == 2 and app.q_tree.get_children():          # 错题分析页：选一道题
                app.q_tree.selection_set(app.q_tree.get_children()[0])
                app.on_question_select()
            if idx == 3:                                        # 搜索页：真的搜一次
                app.search_var.set(app.search_var.get() or "积分")
                app.do_search()
            if idx == 1 and app.pending:                        # 预处理页：载入一张图
                app.load_into_editor()
                app.editor.auto_deskew()
            shot(name)
        # 左栏「题库」页单独截一张：知识点筛选下拉框在这里，
        # 而上面 6 张截的都是中间页签（左栏一直停在"待识别图片"），这项功能就没进过证据。
        try:
            app.left_nb.select(1)
            app.refresh_questions()
            app.update()
            shot("题库-知识点筛选")
            app.left_nb.select(0)
        except Exception as exc:
            print(f"   （题库页截图跳过：{exc}）")
        checks = {
            "题库行数": len(app.q_tree.get_children()),
            "知识点筛选项": len([v for v in app.kp_combo.cget("values")
                                 if v != "全部知识点"]),
            "搜索结果数": len(app.search_tree.get_children()),
            "统计卡片-题目总数": app.card_questions.value_var.get(),
            "学科占比图元数": len(app.chart_subject.find_all()),
            "薄弱知识点图元数": len(app.chart_weak.find_all()),
            "归档树节点数": len(app.archive_tree.get_children()),
            "报告页字数": len(app.report.text.get_text()),
            "日志行数": len(app.log_text.get_text().splitlines()),
            # 预览画布必须真的显示出图片：早期这里恒为空白（PhotoImage 建不出来），
            # 但"界面能开、列表有数据"，光看列表根本发现不了 —— 所以必须断言。
            # 注意：没有待识别图片时（--no-recognize 且未导入）本来就没东西可显示，不算失败。
            "预览画布已显示图片": ("是" if app.preview._photo is not None
                                  else ("❌ 否" if app.pending else "（无待识别图片，跳过）")),
            "预处理画布已显示图片": "是" if app.editor.canvas._photo is not None else "（未载入）",
        }
        print("   界面内容自检：")
        for key, val in checks.items():
            bad = (isinstance(val, int) and val == 0) or (isinstance(val, str) and val.startswith("❌"))
            flag = "⚠️ 空" if bad else "✓"
            print(f"     {flag} {key}：{val}")
        return checks

    if args.no_recognize:
        print("2) 逐页切换并截图（不跑识别）…")
        tour()
    elif args.import_n:
        # 只导入、不识别：用来验证「导入后预览区/编辑器能不能正常显示图片」
        # （这条路径曾经因为 PhotoImage 建不出来而永远是空白，且不需要显卡就能验）
        files = [str(p) for p in
                 __import__("exerarchive.paths", fromlist=["x"]).scan_images(args.samples)]
        files = files[: args.import_n]
        print(f"2) 导入 {len(files)} 张样张（不识别，只验证显示）…")
        app._do_import(files)
        app.update()
        print(f"   待识别列表：{len(app.pending)} 张；预览画布："
              f"{'已显示图片' if app.preview._photo else '❌ 空白'}")
        app.load_into_editor()
        app.update()
        print(f"   预处理页：{app.editor.image.w if app.editor.image else '未载入'}"
              f"；画布：{'已显示图片' if app.editor.canvas._photo else '❌ 空白'}")
        print("3) 逐页切换并截图…")
        tour()
    else:
        files = [str(p) for p in
                 __import__("exerarchive.paths", fromlist=["x"]).scan_images(args.samples)]
        files = files[:2]
        if not files:
            print("   样张目录为空，跳过识别（可先运行 python tools/make_samples.py）")
            tour()
        else:
            print(f"2) 导入 {len(files)} 张样张并识别（会真的调用本地模型，首次含模型加载）…")
            app._do_import(files)
            app.update()
            t0 = time.time()
            app._recognize_worker(app.pending[: len(files)])
            print(f"   识别 + 入库 + 归档 + 错题分析完成，用时 {time.time() - t0:.1f}s")
            app.update()
            app.refresh_all()
            app.update()
            stats = app.store.stats()
            print(f"   题库统计：{stats['questions']} 题，错题 {stats['wrong']}，"
                  f"报告 {stats['reports']}，图片 {stats['images']}")
            print("3) 逐页切换并截图…")
            tour()

    # 设置对话框也要能打开：它是唯一改配置的入口，控件写错在这里就会炸。
    # （本地模型页新增了「重新加载模型」按钮 —— 识别变慢的修复入口，必须守住）
    print("4) 打开设置对话框，检查本地模型页的控件…")
    settings_ok = True
    try:
        from exerarchive.ui.settingsdlg import SettingsDialog

        dlg = SettingsDialog(app, app.cfg)
        app.update()
        labels: list = []

        def walk(w):
            for child in w.winfo_children():
                try:
                    txt = child.cget("text")
                except Exception:
                    txt = ""
                if txt:
                    labels.append(str(txt))
                walk(child)

        walk(dlg)
        need = ["重新加载模型", "刷新模型列表", "测试连接"]
        missing = [t for t in need if t not in labels]
        if missing:
            print(f"   ❌ 设置对话框缺少控件：{missing}")
            settings_ok = False
        else:
            print(f"   ✓ 设置对话框构建正常（{len(labels)} 个文本控件，含「重新加载模型」）")
        dlg.destroy()
        app.update()
    except Exception as exc:
        print(f"   ❌ 设置对话框打开失败：{type(exc).__name__}: {exc}")
        settings_ok = False

    if args.keep:
        print("窗口保留中，关闭窗口即可退出（Ctrl+C 也可）")
        app.mainloop()
    else:
        app._on_close()
    if settings_ok:
        print(f"GUI 冒烟测试通过 ✅（截图 {len(shots)} 张，位于 {out_dir}）")
    # 给批处理用的纯 ASCII 结果行（findstr 对 emoji 不友好）
    print(f"RESULT: {'OK' if settings_ok else 'FAILED'} "
          f"({len(tabs)} tabs, {len(shots)} screenshots)")
    return 0 if settings_ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
