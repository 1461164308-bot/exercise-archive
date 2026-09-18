# -*- coding: utf-8 -*-
"""一键环境自检：装没装好、模型通不通、导出能不能用，一条命令全查清。

用法::

    python tools/selfcheck.py            # 人类可读报告
    python tools/selfcheck.py --json     # 机器可读（供 GUI 的「自检」按钮复用）
    python tools/selfcheck.py --no-model # 跳过模型连通性测试（更快）

检查项：
1. Python 版本、运行平台、编码
2. 数据目录可写、数据库可打开、加密自检（RFC 8439 向量）
3. 图像后端（Pillow / Windows GDI+ / 纯 Python）、中文字体
4. 本地模型服务（Ollama / LM Studio）：是否在跑、有哪些模型、当前模型是否支持视觉
5. 云端配置：是否填了 API Key、连通性
6. 导出能力：Word（内置，恒可用）、PDF（浏览器或内置渲染）、中文字体
7. 磁盘剩余空间、归档目录
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import config, crypto, gdiplus, imageops, logging_setup, paths  # noqa: E402
from exerarchive.store import Store  # noqa: E402
from exerarchive.vlm import ModelClient  # noqa: E402

OK, WARN, BAD = "✅", "⚠️", "❌"


class Report:
    """检查结果集合。

    注意：结果列表叫 ``entries``；``items`` 是可读属性（等价于 entries）。
    早期版本把列表直接叫 ``items``，结果 ``rep.items()`` 会被当成"调用列表"而抛
    ``TypeError: 'list' object is not callable``，导致 ``--json`` 模式整段失效。
    """

    def __init__(self):
        self.entries = []

    @property
    def items(self):
        return self.entries

    def add(self, name: str, status: str, detail: str = "", hint: str = ""):
        self.entries.append({"name": name, "status": status, "detail": detail, "hint": hint})

    def print(self):
        width = max(len(i["name"]) for i in self.entries) + 2
        print("=" * 76)
        print(f"　{paths.APP_NAME}　环境自检　v{paths.APP_VERSION}")
        print("=" * 76)
        for i in self.entries:
            print(f"{i['status']} {i['name']:<{width}} {i['detail']}")
            if i["hint"]:
                print(f"   {'':<{width}} ↳ 建议：{i['hint']}")
        bad = sum(1 for i in self.entries if i["status"] == BAD)
        warn = sum(1 for i in self.entries if i["status"] == WARN)
        print("-" * 76)
        print(f"结论：{len(self.entries)} 项检查，失败 {bad} 项，警告 {warn} 项")
        if bad == 0 and warn == 0:
            print("一切正常，可以开始使用了 🎉")
        elif bad == 0:
            print("核心功能可用（有警告项，按建议处理体验更好）")
        else:
            print("存在失败项，请按上面的建议处理后重试（也可以直接看 docs/03-常见问题排查.md）")
        return 0 if bad == 0 else 1

    def ok(self) -> bool:
        return all(i["status"] != BAD for i in self.entries)


def check_environment(rep: Report):
    v = sys.version_info
    rep.add("Python 版本", OK if v >= (3, 8) else BAD,
            f"{v.major}.{v.minor}.{v.micro}（{sys.platform}）",
            "" if v >= (3, 8) else "请安装 Python 3.8 及以上版本")
    try:
        import tkinter  # noqa: F401
        rep.add("图形界面库 tkinter", OK, "可用（自带，无需安装）")
    except Exception as exc:
        rep.add("图形界面库 tkinter", BAD, f"不可用：{exc}",
                "Windows 下重装 Python 时勾选 tcl/tk；Linux 需装 python3-tk")
    try:
        free = shutil.disk_usage(str(paths.data_dir())).free
        rep.add("磁盘剩余空间", OK if free > 2 * 1024 ** 3 else WARN,
                f"{paths.human_size(free)}（数据目录：{paths.data_dir()}）",
                "" if free > 2 * 1024 ** 3 else "建议清理磁盘，模型与图片都比较占空间")
    except Exception as exc:
        rep.add("磁盘剩余空间", WARN, f"读取失败：{exc}")


def check_storage(rep: Report):
    try:
        p = paths.data_dir() / ".write-test"
        p.write_text("ok", encoding="utf-8")
        p.unlink()
        rep.add("数据目录可写", OK, str(paths.data_dir()))
    except Exception as exc:
        rep.add("数据目录可写", BAD, f"{exc}", "把软件放到有写权限的目录（不要放 C:\\Program Files）")

    try:
        ok = crypto.self_test()
        rep.add("加密模块自检", OK if ok else BAD,
                "ChaCha20/HMAC 通过 RFC 8439 官方测试向量" if ok else "测试向量不通过")
    except Exception as exc:
        rep.add("加密模块自检", BAD, f"{exc}")

    try:
        st = Store(encrypt=True)
        st.stats()
        st.close()
        rep.add("数据库", OK, f"可读写：{paths.db_path()}")
    except Exception as exc:
        rep.add("数据库", BAD, f"{exc}", "检查 data 目录权限，或删除 data/archive.db 重建")


def check_images(rep: Report):
    backend = imageops.backend_name()
    rep.add("图像解码后端", OK if backend != "纯Python（PNG/BMP）" else WARN, backend,
            "" if backend != "纯Python（PNG/BMP）" else
            "当前只能读 PNG/BMP；建议装 Ollama 环境以外的 Pillow（pip install pillow）或使用 Windows")
    rep.add("GDI+ 绘图", OK if gdiplus.available() else WARN,
            "可用（导出 PDF/示例图需要）" if gdiplus.available() else gdiplus.init_error())
    # 造一张小图跑一遍完整链路（含中文字体渲染）
    try:
        if gdiplus.available():
            w, h, px = gdiplus.draw_text_bitmap(420, 120, lines=["中文渲染自检：错题本 123"],
                                                font_size=28, font_name="SimHei")
            dark = sum(1 for i in range(0, len(px), 3) if px[i] < 120)
            ok = dark > 300
            rep.add("中文字体渲染", OK if ok else WARN,
                    f"SimHei 渲染出 {dark} 个笔画像素" if ok else "中文可能显示为方块",
                    "" if ok else "安装黑体/雅黑字体，或在设置里换字体")
        else:
            rep.add("中文字体渲染", WARN, "非 Windows 系统，跳过 GDI 渲染自检")
    except Exception as exc:
        rep.add("中文字体渲染", WARN, f"{exc}")

    # 公式排版：错题解析里的数学要靠它显示成教科书样式（分数、根号、上下标）
    try:
        from exerarchive import mathdraw

        if not mathdraw.available():
            rep.add("公式排版（数学渲染）", WARN, "当前平台不支持 GDI 渲染，公式将用纯文本写法显示",
                    "Windows 下应可用；其它系统请看 docs/04 的可优化方向")
        else:
            probes = [r"e^{2\pi i k/6}", r"\frac{-b\pm\sqrt{b^2-4ac}}{2a}",
                      r"\int_0^1 x^2\,dx", r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}"]
            bad = []
            for latex in probes:
                img, _base = mathdraw.render_image(latex, size_px=20)
                dark = sum(1 for i in range(0, len(img.data), 3) if img.data[i] < 200)
                if img.w < 4 or img.h < 4 or dark < 8:
                    bad.append(latex)
            if bad:
                rep.add("公式排版（数学渲染）", WARN,
                        f"{len(bad)}/{len(probes)} 个公式渲染异常（可能缺数学字体）",
                        "安装 Cambria Math / Segoe UI Symbol 字体，或见 docs/03 附四")
            else:
                rep.add("公式排版（数学渲染）", OK,
                        f"分式/根号/上下标/矩阵 4 类样例均正常（字号 {20}px）")
    except Exception as exc:
        rep.add("公式排版（数学渲染）", WARN, f"{exc}")


def check_gpu(rep: Report):
    """检查显卡状态。

    实测教训：识别突然变得极慢/超时，往往是**别的程序把显存和 GPU 占满了**
    （游戏、渲染、视频会议都常见）。这里直接把占用情况报出来，省得用户瞎猜。
    """
    import shutil as _shutil
    import subprocess as _sp

    exe = _shutil.which("nvidia-smi")
    if not exe:
        rep.add("显卡（NVIDIA）", OK, "未检测到 nvidia-smi（非 NVIDIA 显卡或未装驱动，纯 CPU 也能跑）")
        return
    try:
        out = _sp.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=15, text=True,
        ).stdout.strip().splitlines()
        if not out:
            rep.add("显卡（NVIDIA）", WARN, "nvidia-smi 没有返回数据")
            return
        name, util, used, total, temp = [p.strip() for p in out[0].split(",")]
        used_mb, total_mb = float(used), float(total)
        free_gb = (total_mb - used_mb) / 1024.0
        busy = float(util) > 60 or free_gb < 2.5
        detail = (f"{name}：占用 {util}%，显存 {used_mb:.0f}/{total_mb:.0f} MB"
                  f"（空闲 {free_gb:.1f} GB），温度 {temp}°C")
        if busy:
            rep.add("显卡（NVIDIA）", WARN, detail,
                    "显存/GPU 现在被别的程序占着（游戏、渲染、浏览器视频都算），"
                    "识别会明显变慢甚至超时；关掉它们再试，或把「送模型最长边」调小")
        else:
            rep.add("显卡（NVIDIA）", OK, detail)
    except Exception as exc:
        rep.add("显卡（NVIDIA）", WARN, f"查询失败：{exc}")


def check_local_model(rep: Report, cfg, deep: bool = True):
    kind = str(cfg.get("local.kind", "ollama"))
    client = ModelClient("local", cfg)
    models = client.manager.list_models()
    alive = client.manager.alive()
    rep.add("本地模型服务", OK if alive else BAD,
            f"{'运行中' if alive else '未运行'}（{kind} @ {cfg.get('local.base_url')}）",
            "" if alive else "双击「启动.bat」后重试；或在设置里开启自动启动本地服务")
    if models:
        cur = str(cfg.get("local.model", ""))
        rep.add("已安装模型", OK if cur in models else WARN,
                "、".join(models[:8]) + ("…" if len(models) > 8 else ""),
                "" if cur in models else f"当前配置的模型 {cur} 不在列表里，请在设置里改选")
    elif alive:
        rep.add("已安装模型", BAD, "本地服务在跑，但没有任何模型",
                "执行：ollama pull qwen3-vl:4b（或 lms load qwen3-vl-4b-instruct）")
    if not deep or not alive:
        return
    # 视觉能力实测：造一张写着固定字符串的图，看模型能不能读出来
    try:
        from exerarchive import imageops as _io

        if not gdiplus.available():
            return
        w, h, px = gdiplus.draw_text_bitmap(700, 200, lines=["识别测试码：QINGHUA-7391"],
                                            font_size=40, font_name="SimHei")
        img = _io.Image(w, h, px)
        t0 = time.time()
        res = client.chat("只输出图片里的那串识别测试码，不要任何其它文字。", [img],
                          json_mode=False, timeout=180)
        dt = time.time() - t0
        text = (res.text or "").upper()
        hit = "7391" in text or "QINGHUA" in text
        rep.add("本地模型视觉实测", OK if hit else WARN,
                f"{dt:.1f}s，{res.output_tokens} tokens（{res.tokens_per_sec:.0f} tok/s）"
                f"，读到：{text.strip()[:40] or res.error}",
                "" if hit else "模型没读对测试图：确认用的是视觉模型（如 qwen3-vl），"
                               "或在设置里调大 max_image_side")
    except Exception as exc:
        rep.add("本地模型视觉实测", WARN, f"{exc}")


def check_cloud(rep: Report, cfg):
    prof = cfg.active_cloud() or {}
    name = prof.get("name", "云端")
    if not cfg.get("cloud.enabled", False):
        rep.add("云端增强", OK, f"未启用（当前：{cfg.mode_label()}）",
                "需要更深度的错题分析时，在设置里填 API Key 并勾选「启用云端增强」")
        return
    if not cfg.effective_api_key():
        rep.add("云端增强", WARN, f"{name} 已启用但没填 API Key", "在设置 → 云端 里填 Key（或设环境变量）")
        return
    try:
        res = ModelClient("cloud", cfg).ping(timeout=30)
        rep.add("云端增强连通性", OK if res.ok else WARN,
                f"{name} / {prof.get('model')}：{'正常' if res.ok else res.error}",
                "" if res.ok else "检查网络、Base URL、模型名与 Key")
    except Exception as exc:
        rep.add("云端增强连通性", WARN, f"{exc}")


def check_export(rep: Report):
    from exerarchive.exporter import _find_browser

    b = _find_browser()
    rep.add("PDF 导出（浏览器打印）", OK if b else WARN,
            f"找到：{b}" if b else "未找到 Edge/Chrome",
            "" if b else "不影响使用：会自动改用内置渲染器生成图片版 PDF（体积略大）")
    rep.add("Word 导出", OK, "内置 .docx 生成器（无需 Office）")
    try:
        t = paths.data_dir() / "归档"
        t.mkdir(parents=True, exist_ok=True)
        rep.add("归档目录", OK, str(t))
    except Exception as exc:
        rep.add("归档目录", BAD, f"{exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="环境自检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default=None,
                    help="把 JSON 直接写入指定文件（推荐，避免 PowerShell 重定向写成 UTF-16）")
    ap.add_argument("--no-model", action="store_true", help="跳过模型实测（更快）")
    args = ap.parse_args(argv)

    # 自检报告里有 ✅/⚠️/❌，中文 Windows 上只要输出被重定向或接进管道就是 GBK，
    # 不先切 UTF-8 就会在 rep.print() 里抛 UnicodeEncodeError（报告为空 + traceback）。
    logging_setup.enable_utf8_console()
    logging_setup.setup()
    cfg = config.load_config()
    config.bootstrap_local_model(cfg)

    rep = Report()
    check_environment(rep)
    check_storage(rep)
    check_images(rep)
    check_gpu(rep)
    check_local_model(rep, cfg, deep=not args.no_model)
    check_cloud(rep, cfg)
    check_export(rep)

    if args.json or args.out:
        payload = {
            "app": paths.APP_NAME, "version": paths.APP_VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": rep.entries,
            "failed": sum(1 for i in rep.entries if i["status"] == BAD),
            "warned": sum(1 for i in rep.entries if i["status"] == WARN),
            "ok": rep.ok(),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if args.out:
            # 自己写文件（UTF-8），避免 PowerShell 的 > 把输出写成 UTF-16 让下游读不了
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"诊断信息已写入：{args.out}")
            if not args.json:
                return 0 if rep.ok() else 1
        # 强制 UTF-8 输出：中文 Windows 控制台默认 GBK，重定向到文件时会因
        # 无法编码 ✅/❌ 而抛 UnicodeEncodeError，导致诊断文件是空的。
        # （main() 开头已统一调过 enable_utf8_console()，这里保留一次幂等兜底，
        #   因为 Report.print() 也可能被别处单独调用。）
        logging_setup.enable_utf8_console()
        print(text)
        return 0 if rep.ok() else 1
    return rep.print()


if __name__ == "__main__":
    raise SystemExit(main())
