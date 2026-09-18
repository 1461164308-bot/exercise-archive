# -*- coding: utf-8 -*-
"""生成程序图标（.ico）与桌面快捷方式。

图标完全由代码绘制（不依赖任何素材文件、不需要联网、不需要第三方库）：
深蓝圆角底板 + 白色「题」字，多尺寸打包成一个 .ico，任务栏/桌面/超大图标都清晰。

用法::

    python tools/make_shortcut.py                    # 生成图标 + 在桌面建快捷方式
    python tools/make_shortcut.py --icon-only        # 只生成 assets\\app.ico
    python tools/make_shortcut.py --name "AI错题本"   # 自定义快捷方式名字

快捷方式指向 ``启动.bat``（无控制台窗口启动），工作目录设为项目根目录，
这样双击即可打开软件，且不会因为工作目录不对而找不到 data/。
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import gdiplus, imageops, paths  # noqa: E402

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
BRAND = (47, 111, 237)        # 主色（与界面按钮同色 #2f6fed）
BRAND_DARK = (31, 79, 191)    # 边框色


# --------------------------------------------------------------------------- #
# 绘制
# --------------------------------------------------------------------------- #
def render_glyph(char: str = "题", canvas: int = 0, font_size: int = 260) -> imageops.Image:
    """把单个汉字渲染成黑字白底，并按墨迹裁掉四周空白（便于精确居中缩放）。

    坑：``draw_text_bitmap`` 的行高约等于 ``font_size × 1.45``，画布比行高还小时
    它会直接跳过绘制（历史版本在这里得到一张纯白图，然后报"没找到字形像素"）。
    所以这里按字体大小自动算画布，并在万一没墨迹时自动放大重试一次。
    """
    for attempt, size in enumerate((canvas or int(font_size * 1.7),
                                    int(font_size * 2.4))):
        w, h, px = gdiplus.draw_text_bitmap(
            size, size, bg_rgb=(255, 255, 255), lines=[char],
            font_size=font_size, font_name="SimHei", color=(0, 0, 0), margin=10,
        )
        img = imageops.Image(w, h, px)
        left, top, right, bottom = img.w, img.h, 0, 0
        d = img.data
        for y in range(img.h):
            row = y * img.w * 3
            for x in range(img.w):
                if d[row + x * 3] < 128:
                    left = min(left, x)
                    right = max(right, x)
                    top = min(top, y)
                    bottom = max(bottom, y)
        if right > left and bottom > top:
            return img.crop((left, top, right + 1, bottom + 1))
    raise RuntimeError("字体渲染失败：没找到字形像素（检查系统是否安装了 SimHei/黑体）")


def rounded_alpha(size: int, radius_ratio: float = 0.22) -> list:
    """圆角矩形的覆盖率（0~1，用于抗锯齿边缘）。返回 size×size 的列表。"""
    r = max(2.0, size * radius_ratio)
    out = []
    for y in range(size):
        for x in range(size):
            # 到圆角矩形边界的有符号距离
            cx = min(max(x + 0.5, r), size - r)
            cy = min(max(y + 0.5, r), size - r)
            dx, dy = (x + 0.5) - cx, (y + 0.5) - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= r - 1:
                out.append(1.0)
            elif dist >= r:
                out.append(0.0)
            else:
                out.append(max(0.0, min(1.0, r - dist)))
    return out


def make_icon_rgba(size: int, glyph: imageops.Image) -> tuple:
    """生成某一尺寸的图标 RGBA 像素（圆角蓝底 + 白色字形）。"""
    alpha = rounded_alpha(size)
    # 字形缩放到约 66% 并居中
    target = max(6, int(size * 0.66))
    scale = target / max(glyph.w, glyph.h)
    gw, gh = max(1, int(glyph.w * scale)), max(1, int(glyph.h * scale))
    g = glyph.resize(gw, gh)
    ox, oy = (size - gw) // 2, (size - gh) // 2

    rgba = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            i = (y * size + x)
            a = alpha[i]
            if a <= 0:
                continue
            # 底色：上浅下深，做出一点立体感；边缘 1px 用深色描边
            t = y / max(1, size - 1)
            base = tuple(int(BRAND[c] * (1 - t * 0.25) + BRAND_DARK[c] * (t * 0.25)) for c in range(3))
            r, gg, b = base
            if a < 1.0:
                r, gg, b = BRAND_DARK            # 抗锯齿边缘描边
            # 叠字形
            gx, gy = x - ox, y - oy
            if 0 <= gx < gw and 0 <= gy < gh:
                gi = (gy * gw + gx) * 3
                ink = 1.0 - glyph_pixel(g, gi) / 255.0   # 越黑越不透明
                if ink > 0:
                    r = int(r * (1 - ink) + 255 * ink)
                    gg = int(gg * (1 - ink) + 255 * ink)
                    b = int(b * (1 - ink) + 255 * ink)
            o = i * 4
            rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3] = r, gg, b, int(round(a * 255))
    return size, size, rgba


def glyph_pixel(g: imageops.Image, i: int) -> int:
    return g.data[i]


# --------------------------------------------------------------------------- #
# ICO 封装
# --------------------------------------------------------------------------- #
def write_ico(entries, path: str) -> str:
    """把若干 (尺寸, RGBA 缓冲) 打包成 .ico（BMP 条目 + AND 掩码，兼容 Windows 全尺寸）。"""
    images = []
    for size, _w, rgba in entries:
        # ICO 里的 BMP 条目：BITMAPINFOHEADER(40) + BGRA 自底向上 + AND 掩码
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             size * size * 4, 0, 0, 0, 0)
        body = bytearray()
        for y in range(size - 1, -1, -1):        # 自底向上
            for x in range(size):
                o = (y * size + x) * 4
                r, g, b, a = rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]
                body += bytes((b, g, r, a))
        # AND 掩码：透明像素置 1（每行按 4 字节对齐）
        row_bytes = ((size + 31) // 32) * 4
        mask = bytearray(row_bytes * size)
        for y in range(size):
            for x in range(size):
                if rgba[(y * size + x) * 4 + 3] < 128:
                    mask[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
        images.append(header + bytes(body) + bytes(mask))

    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for (size, _w, rgba), blob in zip(entries, images):
        out += struct.pack("<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
                           0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in images:
        out += blob
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


def build_icon(path: str = "") -> str:
    if not gdiplus.available():
        raise RuntimeError("生成图标需要 Windows GDI（本机不可用）")
    glyph = render_glyph()
    entries = [make_icon_rgba(s, glyph) for s in ICON_SIZES]
    path = path or str(paths.code_root() / "assets" / "app.ico")
    write_ico(entries, path)
    return path


# --------------------------------------------------------------------------- #
# 快捷方式
# --------------------------------------------------------------------------- #
def find_gui_launcher() -> tuple:
    """找一个**不带控制台**的 Python 启动方式，返回 (可执行文件, 参数列表)。

    为什么必须绕开 .bat：Windows 11 默认用 **Windows Terminal** 承载批处理窗口，
    而它常常不会随脚本结束而关闭 —— 用户每双击一次快捷方式就多留一个黑色终端窗口
    （实测：快捷方式指向 ``启动.bat`` 时，启动瞬间会新建一个
    ``CASCADIA_HOSTING_WINDOW_CLASS`` 的 Terminal 窗口，且可能残留）。

    所以优先直接调用 GUI 子系统的解释器：
    - ``pyw.exe``（Python 官方启动器的窗口版）→ ``pyw -3 -m exerarchive``
    - ``pythonw.exe``（窗口版解释器）→ ``pythonw -m exerarchive``
    两者都不会创建控制台窗口；都找不到时才退回 ``启动.bat``。
    """
    import shutil
    import sys

    pyw = shutil.which("pyw")
    if pyw:
        return pyw, ["-3", "-m", "exerarchive"]
    pythonw = shutil.which("pythonw")
    if pythonw:
        return pythonw, ["-m", "exerarchive"]
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(cand):
        return cand, ["-m", "exerarchive"]
    return "", []


def desktop_dir() -> str:
    """取**真实桌面**目录。

    坑：中文 Windows 常见 OneDrive 重定向，桌面实际在 ``%USERPROFILE%\\OneDrive\\桌面``，
    而 ``%USERPROFILE%\\Desktop`` 可能只是残留的空目录 —— 早期版本优先选了后者，
    结果快捷方式建到了一个"看不见的桌面"上。这里改成问系统（Known Folder）。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        path = (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout else ""
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    # 退路：注册表里登记的桌面路径
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\"
             "User Shell Folders').Desktop"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        path = (proc.stdout or "").strip().splitlines()[-1].strip() if proc.stdout else ""
        path = os.path.expandvars(path)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    candidates = [
        os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "Desktop"),
        os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "桌面"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return candidates[-1]


def create_shortcut(name: str = "AI 错题本", icon: str = "", target: str = "",
                    working: str = "", desktop: str = "") -> str:
    """用 PowerShell 的 WScript.Shell 建 .lnk（Windows 下最稳，无需 pywin32）。

    默认目标**不是** ``启动.bat``，而是 GUI 版解释器（``pyw -3 -m exerarchive``）：
    这样双击快捷方式**完全不会出现命令行窗口**。只有找不到 pyw/pythonw 时才退回 bat。
    """
    icon = icon or str(paths.code_root() / "assets" / "app.ico")
    working = working or str(paths.code_root())
    desktop = desktop or desktop_dir()
    lnk = os.path.join(desktop, f"{name}.lnk")

    if target:
        exe, arguments = target, ""
    else:
        exe, argv = find_gui_launcher()
        if not exe:
            exe, argv = str(paths.code_root() / "启动.bat"), []
            print("  ⚠️ 没找到 pyw/pythonw，退回用 启动.bat（双击时会闪一个命令行窗口）")
        arguments = " ".join(argv)

    ps = f"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '{name}.lnk'
try {{
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = '{exe}'
    $s.Arguments = '{arguments}'
    $s.WorkingDirectory = '{working}'
    $s.IconLocation = '{icon},0'
    $s.Description = 'AI 本地错题习题归档整理工具（本地模型识别 + 错题分析）'
    $s.WindowStyle = 1
    $s.Save()
}} catch {{
    Write-Output ('FAILED 保存失败：' + $_.Exception.Message)
    exit 1
}}
# 写完必须回读校验：早期版本只判断"文件是否存在"，结果保存被拒（权限/沙箱）
# 时也会误报成功，用户看到的还是旧快捷方式
$check = $ws.CreateShortcut($lnk)
Write-Output ('TARGET=' + $check.TargetPath)
Write-Output ('ARGS=' + $check.Arguments)
if ($check.TargetPath -ne '{exe}') {{ Write-Output 'FAILED 目标未生效'; exit 1 }}
Write-Output ('CREATED ' + $lnk)
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        creationflags=0x08000000,        # 建快捷方式时也别闪控制台窗口
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if "CREATED" not in out:
        raise RuntimeError(
            "创建快捷方式失败（桌面写入可能被权限/安全软件拦截）：\n" + out.strip()[:400]
        )
    real = [l for l in out.splitlines() if l.startswith("CREATED")][0][len("CREATED "):].strip()
    got_target = next((l[7:] for l in out.splitlines() if l.startswith("TARGET=")), "")
    got_args = next((l[5:] for l in out.splitlines() if l.startswith("ARGS=")), "")
    print(f"  回读校验：目标={got_target} 参数={got_args!r}")
    return real or lnk


def main() -> int:
    ap = argparse.ArgumentParser(description="生成图标与桌面快捷方式")
    ap.add_argument("--icon-only", action="store_true", help="只生成 .ico")
    ap.add_argument("--name", default="AI 错题本", help="快捷方式名称（默认 AI 错题本）")
    ap.add_argument("--icon", default="", help="自定义图标路径")
    ap.add_argument("--target", default="", help="自定义快捷方式目标（默认用 pyw/pythonw，无控制台窗口）")
    args = ap.parse_args()

    from exerarchive import logging_setup

    logging_setup.enable_utf8_console()   # 报告里有 ⚠️，重定向时别被 GBK 打断

    path = build_icon(args.icon)
    print(f"图标已生成：{path}（{os.path.getsize(path) / 1024:.1f} KB，"
          f"含 {len(ICON_SIZES)} 种尺寸：{', '.join(str(s) for s in ICON_SIZES)}）")
    if args.icon_only:
        return 0

    lnk = create_shortcut(args.name, icon=path, target=args.target)
    print(f"桌面快捷方式已创建：{lnk}")
    print(f"  工作目录：{paths.code_root()}")
    print(f"  图标：{path}")
    print("双击桌面上的「AI 错题本」即可启动（不会出现任何命令行窗口）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
