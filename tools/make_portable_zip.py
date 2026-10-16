# -*- coding: utf-8 -*-
"""打包「便携版」：连 Python 运行时一起打，目标电脑**不用装 Python** 就能双击运行。

和 ``make_release_zip.py``（只打源码，要求目标机已装 Python）的区别就在这里。

做法：把当前解释器（``sys.base_prefix``）整份拷进去，再按下面的规则精简 ——
本软件是**纯标准库、零第三方依赖**，所以 ``Lib\\site-packages`` 可以整块删掉，
``Lib\\test``、``Lib\\idlelib``、``Lib\\ensurepip``、``Lib\\venv`` 这些也用不上。

用法::

    python tools\\make_portable_zip.py                     # 输出到项目上一级目录
    python tools\\make_portable_zip.py --out D:\\交付       # 指定输出目录
    python tools\\make_portable_zip.py --name 我的错题本    # 自定义文件夹名
    python tools\\make_portable_zip.py --no-zip            # 只要文件夹，不压 zip

产物::

    <out>\\AI错题本-便携版-v1.2.0\\          解压即用的文件夹
    <out>\\AI错题本-便携版-v1.2.0.zip        同样内容的 zip（便于传输）

注意：便携运行时跟着**当前解释器**走（本机是 64 位 → 目标机也要 64 位 Windows）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import logging_setup, paths  # noqa: E402

# ---------------------------------------------------------------- 精简规则
# 顶层不拷的目录/文件（运行时用不到，删掉能省 60+ MB）
RUNTIME_SKIP_TOP = {
    "Doc", "include", "libs", "Scripts", "share", "Tools",
    "NEWS.txt",          # 发行说明，运行时不需要（LICENSE.txt 保留：分发要求）
}
# Lib 下不拷的目录：test 27MB、idlelib 2.5MB、ensurepip 1.8MB…
# site-packages 可整块删 —— 本软件零第三方依赖
RUNTIME_SKIP_LIB = {
    "test", "idlelib", "ensurepip", "venv", "turtledemo", "site-packages",
    "lib2to3", "distutils", "tkinter\\test", "unittest\\test",
    "sqlite3\\test", "ctypes\\test", "email\\test", "json\\tests",
    "encodings\\__pycache__", "asyncio\\__pycache__",
}
# 应用侧不打包的东西：用户数据、缓存、开发证据
APP_SKIP_DIRS = {"data", "__pycache__", ".git", ".idea", ".vscode", "_tmp", "_workflow"}
# verify/ 里只带文档引用到的界面截图，其余验收证据不带
VERIFY_KEEP_PREFIX = "gui-"

PORTABLE_BAT = r"""@echo off
chcp 65001 >nul
rem ============================================================
rem  AI错题本 便携版 —— 双击即用，不需要安装 Python
rem  优先用自带的 python\pythonw.exe；找不到才退回系统 Python。
rem ============================================================
cd /d "%~dp0"

if exist "%~dp0python\pythonw.exe" (
    start "" "%~dp0python\pythonw.exe" -m exerarchive
    goto :eof
)

echo 自带的 python 运行时缺失，尝试使用系统 Python…
where py >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 -m exerarchive
    goto :eof
)
where pythonw >nul 2>nul
if errorlevel 1 (
    echo.
    echo 没有找到可用的 Python 运行时，便携包可能解压不完整。
    echo 请重新完整解压本文件夹后再试。
    echo.
    pause
    exit /b 1
)
start "" pythonw -m exerarchive
"""

PORTABLE_BAT_CONSOLE = r"""@echo off
chcp 65001 >nul
rem AI错题本 便携版 —— 控制台方式启动（出错时看这里的报错信息）
cd /d "%~dp0"

if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" -m exerarchive
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m exerarchive
    goto :eof
)
python -m exerarchive
"""

SELFCHECK_BAT = r"""@echo off
chcp 65001 >nul
rem 环境自检：确认自带的 Python 与依赖都正常
cd /d "%~dp0"
echo === 便携运行时自检 ===
"%~dp0python\python.exe" -c "import sys, tkinter, sqlite3, ctypes, json, urllib.request, hashlib, hmac, zipfile; print('Python', sys.version.split()[0]); print('tkinter/sqlite3/ctypes 均可导入'); print('运行时目录', sys.prefix)"
echo.
echo === 软件自检 ===
"%~dp0python\python.exe" tools\selfcheck.py
echo.
pause
"""

README_TXT = """AI错题本 · 便携版 v{version}
================================================

这个包**不需要安装 Python**，也不需要命令行：解压后双击「启动.bat」即可。

【怎么用】
1. 把整个文件夹解压到一个**有写权限**的位置，例如 D:\\AI错题本
   （不要放在 C:\\Program Files 下面，那里没有写权限，题库会存不下来）
2. 双击「启动.bat」→ 出现主窗口
3. 第一次用请先配好"云端模型"：主窗口右上角「设置」→「云端模型」
   填 4 项：名称 / Base URL / 模型名 / API Key，
   **并且勾上「该模型支持图片」**（不勾的话云端收不到图片，会识别出 0 道题）
   → 点「测试连接」确认通了 → 切到「运行模式」选「云端优先」→ 点右下角「保存」
4. 回到主窗口：「导入图片」选你的错题照片 → 「开始识别 →」→ 在「我的题库」核对题干
5. 详细图文步骤见 docs\\07-用户全流程使用说明.md

【不用管本地模型】
启动时若左下角提示「本地模型服务未就绪」，**选了云端优先就可以完全忽略**：
这句只是说本机没装小模型，而你的识别走云端。想让它别再提示：
设置 → 本地模型 → 取消勾选「启动软件时自动拉起本地模型服务」。

【你的数据在哪】
都在本文件夹的 data\\ 目录里（题库、原图、归档、日志）。
拷贝这个文件夹 = 搬家；删掉 data\\ = 清空所有记录。包里出厂**不含任何个人数据**。

【出问题怎么办】
- 双击「启动-控制台.bat」可以看到报错文字
- 双击「环境自检.bat」检查自带的 Python 与依赖是否正常
- 常见问题对照表见 docs\\07-用户全流程使用说明.md 第 5 章

【系统要求】
- 64 位 Windows 10 / 11
- 需要联网（识别走云端模型，要一个 API Key；申请方式见上面第 3 步所指文档）

【想先试试效果，不配 API Key 也行】
双击「生成示例习题图.bat」会在 samples\\ 下生成模拟试卷照片，可以用来熟悉界面。

【版权】
本软件为纯 Python 实现，随包附带的 Python 运行时版权归 Python Software Foundation
（见 python\\LICENSE.txt）。
"""


def human(mb: float) -> str:
    return "%.0f MB" % mb if mb >= 1 else "%.0f KB" % (mb * 1024)


def dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def copy_runtime(src: str, dst: str, log) -> None:
    """把 Python 运行时拷进 dst/python，并按规则精简。"""
    os.makedirs(dst, exist_ok=True)
    copied = skipped = 0
    for base, dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        parts = [] if rel == "." else rel.split(os.sep)
        keep = []
        for d in dirs:
            if len(parts) == 0 and d in RUNTIME_SKIP_TOP:
                skipped += 1
                continue
            if d == "__pycache__":
                skipped += 1
                continue
            if len(parts) == 1 and parts[0] == "Lib":
                sub = os.path.join(rel, d).replace("\\", "\\")
                rel_sub = os.path.relpath(os.path.join(base, d), os.path.join(src, "Lib"))
                if rel_sub in RUNTIME_SKIP_LIB or rel_sub.split(os.sep)[0] in RUNTIME_SKIP_LIB:
                    skipped += 1
                    continue
            keep.append(d)
        dirs[:] = keep

        target_dir = os.path.join(dst, rel) if parts else dst
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if len(parts) == 0 and name in RUNTIME_SKIP_TOP:
                skipped += 1
                continue
            if name.endswith((".pyc", ".pyo")):
                skipped += 1
                continue
            try:
                shutil.copy2(os.path.join(base, name), os.path.join(target_dir, name))
                copied += 1
            except OSError as exc:
                log("  ！跳过 %s（%s）" % (name, exc))
    log("  运行时：拷入 %d 个文件，跳过 %d 项（省掉 site-packages / test / Doc 等）"
        % (copied, skipped))


def copy_app(dst: str, log) -> None:
    """把软件本体拷进 dst（保持能 `python -m exerarchive` 的目录结构）。"""
    copied = 0
    for base, dirs, files in os.walk(ROOT):
        rel = os.path.relpath(base, ROOT)
        parts = [] if rel == "." else rel.split(os.sep)
        dirs[:] = [d for d in dirs if d not in APP_SKIP_DIRS]
        if parts and parts[0] == "verify":
            # 只保留文档引用到的界面截图
            dirs[:] = []
        target_dir = os.path.join(dst, rel) if parts else dst
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if len(parts) == 0 and name.endswith(".bat"):
                continue                    # 便携版用自己写的启动脚本
            if parts and parts[0] == "verify" and not name.startswith(VERIFY_KEEP_PREFIX):
                continue
            if os.path.splitext(name)[1].lower() in (".pyc", ".pyo", ".log"):
                continue
            try:
                shutil.copy2(os.path.join(base, name), os.path.join(target_dir, name))
                copied += 1
            except OSError:
                pass
    log("  软件本体：拷入 %d 个文件（已排除 data/、缓存与验收证据）" % copied)


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包便携版（内含 Python 运行时）")
    ap.add_argument("--out", default=None, help="输出目录（默认：项目上一级目录）")
    ap.add_argument("--name", default=None, help="文件夹名（默认 AI错题本-便携版-v<版本>）")
    ap.add_argument("--python", default=sys.base_prefix, help="要打包的 Python 安装目录")
    ap.add_argument("--no-zip", action="store_true", help="只生成文件夹，不压 zip")
    args = ap.parse_args()

    logging_setup.enable_utf8_console()
    version = paths.APP_VERSION
    name = args.name or f"AI错题本-便携版-v{version}"
    out_root = args.out or os.path.dirname(ROOT)
    stage = os.path.join(out_root, name)
    zip_path = stage + ".zip"

    py_src = os.path.abspath(args.python)
    if not os.path.isfile(os.path.join(py_src, "python.exe")):
        print("找不到 Python 运行时：%s" % py_src)
        return 2

    def log(msg: str) -> None:
        print(msg, flush=True)

    log("打包便携版：%s" % name)
    log("  版本号：v%s" % version)
    log("  运行时来源：%s（%s）" % (py_src, sys.version.split()[0]))
    if os.path.exists(stage):
        log("  清理旧的同名文件夹…")
        shutil.rmtree(stage, ignore_errors=True)

    t0 = time.time()
    log("① 拷 Python 运行时 → python\\")
    copy_runtime(py_src, os.path.join(stage, "python"), log)
    log("② 拷软件本体 → 根目录")
    copy_app(stage, log)
    log("③ 写便携启动脚本")
    write_text(os.path.join(stage, "启动.bat"), PORTABLE_BAT)
    write_text(os.path.join(stage, "启动-控制台.bat"), PORTABLE_BAT_CONSOLE)
    write_text(os.path.join(stage, "环境自检.bat"), SELFCHECK_BAT)
    write_text(os.path.join(stage, "使用说明（先看我）.txt"), README_TXT.format(version=version))
    log("  启动.bat / 启动-控制台.bat / 环境自检.bat / 使用说明（先看我）.txt")

    size = dir_size(stage)
    files = sum(len(f) for _b, _d, f in os.walk(stage))
    log("④ 完成：%s" % stage)
    log("   %d 个文件，%.1f MB（耗时 %.0f 秒）" % (files, size / 1024 / 1024, time.time() - t0))

    if not args.no_zip:
        log("⑤ 压 zip → %s" % zip_path)
        z0 = time.time()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for base, _dirs, fnames in os.walk(stage):
                for fn in fnames:
                    full = os.path.join(base, fn)
                    zf.write(full, os.path.join(name, os.path.relpath(full, stage)))
        zsize = os.path.getsize(zip_path)
        log("   zip：%.1f MB（压缩率 %.0f%%，耗时 %.0f 秒）"
            % (zsize / 1024 / 1024, zsize / max(1, size) * 100, time.time() - z0))
        log("\n交付：%s" % zip_path)
    else:
        log("\n交付：%s" % stage)
    log("目标电脑要求：64 位 Windows 10/11，**不需要装 Python**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
