# -*- coding: utf-8 -*-
"""设置对话框：运行模式 / 本地模型 / 云端模型 / 隐私 / 归档。

小白用户只需要关心两件事：
1. 想不想用云端增强（填个 API Key 就行）；
2. 图片存哪儿、要不要加密。
其余参数都给了合理默认值，并且每个输入框下面都有一句话说明。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, Optional

from ..config import Config, default_ollama_paths
from ..logging_setup import get_logger
from ..vlm import ModelClient
from .widgets import COLORS

log = get_logger("ui.settings")


class SettingsDialog(tk.Toplevel):
    """配置窗口（模态）。保存后通过 ``on_saved`` 通知主窗口刷新。"""

    def __init__(self, master, cfg: Config, on_saved: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.title("设置")
        self.cfg = cfg
        self.on_saved = on_saved
        self.transient(master)
        self.resizable(True, True)
        self.geometry("760x620")
        self.minsize(700, 560)
        self._vars: Dict[str, tk.Variable] = {}
        self._build()
        self.grab_set()

    # ------------------------------------------------------------------ #
    def _var(self, key: str, value=None, kind=tk.StringVar):
        if key not in self._vars:
            self._vars[key] = kind(value=value if value is not None else self.cfg.get(key, ""))
        return self._vars[key]

    def _row(self, parent, label: str, key: str, hint: str = "", width: int = 40,
             kind=tk.StringVar) -> ttk.Entry:
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        ttk.Label(frame, text=label, width=18).pack(side="left", anchor="n")
        entry = ttk.Entry(frame, textvariable=self._var(key, kind=kind), width=width)
        entry.pack(side="left", fill="x", expand=True)
        if hint:
            ttk.Label(parent, text="　　" + hint, style="Muted.TLabel", wraplength=640).pack(
                anchor="w", pady=(0, 4))
        return entry

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # ---------------- 运行模式 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="运行模式")
        ttk.Label(tab, text="本地优先，云端兜底（推荐）：常规习题用本机模型离线识别，"
                            "遇到模糊、字迹潦草、置信度低的题自动交给云端复核。",
                  wraplength=660, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        mode = self._var("run.mode", self.cfg.mode())
        for text, value, desc in (
            ("纯本地（完全离线，隐私最强）", "local_only", "不联网，全部在本机完成；速度取决于你的电脑。"),
            ("本地优先 + 云端兜底（推荐）", "hybrid", "本地识别不确定时自动用云端复核（需要 API Key）。"),
            ("云端优先", "cloud_first", "直接交云端处理，最准但依赖网络，且会发送图片（可在隐私页关闭）。"),
        ):
            ttk.Radiobutton(tab, text=text, value=value, variable=mode).pack(anchor="w", pady=(4, 0))
            ttk.Label(tab, text="　　" + desc, style="Muted.TLabel").pack(anchor="w")
        ttk.Separator(tab).pack(fill="x", pady=12)
        self._row(tab, "置信度阈值", "run.confidence_threshold",
                  "低于该值就转云端复核（0~1，越小越少用云端；默认 0.70）")
        self._row(tab, "图片质量阈值", "run.quality_threshold",
                  "清晰度/亮度综合分低于该值直接转云端（默认 0.35）")
        self._row(tab, "单图最多题目数", "run.max_questions_per_image",
                  "防止模型在一张图上啰嗦太多（默认 8）")
        for key, text in (("run.auto_preprocess", "导入后自动纠偏/提亮/锐化"),
                          ("run.auto_analyze", "识别出错题后自动生成分析报告"),
                          ("run.cloud_on_error", "本地失败自动转云端"),
                          ("run.cloud_for_analysis", "错题分析使用云端深度版"),
                          ("run.cloud_for_variants", "变式练习题由云端生成")):
            ttk.Checkbutton(tab, text=text, variable=self._var(key, bool(self.cfg.get(key, True)),
                                                              tk.BooleanVar)).pack(anchor="w", pady=2)

        # ---------------- 本地模型 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="本地模型")
        ttk.Label(tab, text="本机轻量视觉模型：负责离线识别。推荐 Ollama + qwen3-vl:4b"
                            "（4.4B，4G 左右显存即可流畅运行）。",
                  wraplength=660, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="服务类型", width=18).pack(side="left")
        ttk.Combobox(r, textvariable=self._var("local.kind", self.cfg.get("local.kind", "ollama")),
                     values=["ollama", "lmstudio", "openai_compatible"], width=20,
                     state="readonly").pack(side="left")
        ttk.Label(r, text="　ollama=本机 Ollama；lmstudio=LM Studio；其它 OpenAI 兼容服务选第三项",
                  style="Muted.TLabel").pack(side="left")
        self._row(tab, "服务地址", "local.base_url", "例：http://127.0.0.1:11434（Ollama）")
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="模型名称", width=18).pack(side="left")
        self.model_box = ttk.Combobox(r, textvariable=self._var("local.model", self.cfg.get("local.model", "")),
                                      width=32)
        self.model_box.pack(side="left")
        ttk.Button(r, text="刷新模型列表", command=self.refresh_models).pack(side="left", padx=6)
        ttk.Button(r, text="测试连接", command=self.test_local).pack(side="left")
        self.model_hint = ttk.Label(tab, text="　　（点「刷新模型列表」可从本机服务读取已安装模型）",
                                    style="Muted.TLabel")
        self.model_hint.pack(anchor="w", pady=(0, 4))
        # 识别"突然变慢"的头号原因是模型没装进显存：Ollama 只在加载那一刻分层，之后不再规划。
        # 所以给一个按钮，让它按当前空闲显存重新加载一次（实测 6.6 tok/s → 74~80 tok/s）。
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=(0, 6))
        ttk.Button(r, text="重新加载模型", command=self.reload_local_model).pack(side="left")
        ttk.Label(r, text="　识别明显变慢时点这个：让 Ollama 按当前空闲显存重新规划（几十秒）",
                  style="Muted.TLabel").pack(side="left")
        self._row(tab, "识别超时(秒)", "local.timeout", "CPU 跑视觉模型可能较慢，默认 300 秒")
        self._row(tab, "送模型最长边", "local.max_image_side",
                  "越小越快、越省显存（默认 1600；显存紧张可调到 1100）")
        self._row(tab, "压缩画质", "local.jpeg_quality", "送给模型前 JPEG 压缩质量（默认 88）")
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="服务程序路径", width=18).pack(side="left")
        ttk.Entry(r, textvariable=self._var("local.server_exe", self.cfg.get("local.server_exe", ""))
                  ).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="浏览", width=6, command=lambda: self._pick_file("local.server_exe")).pack(side="left")
        self._row(tab, "模型目录", "local.models_dir",
                  "Ollama 模型存放目录（多盘部署时填，例如 D:\\ollama-models）")
        self._row(tab, "数据目录", "local.home_dir", "Ollama 的 .ollama 目录（受限环境时填）")
        ttk.Checkbutton(tab, text="启动软件时自动拉起本地模型服务（推荐）",
                        variable=self._var("local.auto_start", bool(self.cfg.get("local.auto_start", True)),
                                           tk.BooleanVar)).pack(anchor="w", pady=2)
        ttk.Checkbutton(tab, text="关闭思考链（识别更快，qwen3-vl 建议开启）",
                        variable=self._var("local.disable_thinking",
                                           bool(self.cfg.get("local.disable_thinking", True)),
                                           tk.BooleanVar)).pack(anchor="w", pady=2)

        # ---------------- 云端模型 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="云端模型")
        ttk.Checkbutton(tab, text="启用云端增强（填好 API Key 后勾选）",
                        variable=self._var("cloud.enabled", bool(self.cfg.get("cloud.enabled", False)),
                                           tk.BooleanVar)).pack(anchor="w", pady=(0, 8))
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="配置档案", width=18).pack(side="left")
        self.profile_box = ttk.Combobox(r, width=34, state="readonly",
                                        values=[p.get("name", "") for p in self.cfg.get("cloud.profiles", [])])
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.load_profile())
        ttk.Button(r, text="+ 新增", width=8, command=self.add_profile).pack(side="left", padx=4)
        ttk.Button(r, text="删除", width=6, command=self.del_profile).pack(side="left")
        ttk.Label(tab, text="　　任何一个 OpenAI 兼容接口都能用：DeepSeek / 通义千问 / 智谱 / Kimi /"
                            "自建 OneAPI 等，只要填对 Base URL 与模型名。",
                  wraplength=660, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self._row(tab, "名称", "cloud.cur.name", "给这套配置起个名字，例如「DeepSeek 官方」")
        self._row(tab, "Base URL", "cloud.cur.base_url", "例：https://api.deepseek.com/v1")
        self._row(tab, "模型名", "cloud.cur.model", "例：deepseek-chat / qwen-vl-max / glm-4v")
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="API Key", width=18).pack(side="left")
        ttk.Entry(r, textvariable=self._var("cloud.cur.api_key", ""), show="*").pack(
            side="left", fill="x", expand=True)
        ttk.Checkbutton(r, text="该模型支持图片", variable=self._var(
            "cloud.cur.supports_vision", True, tk.BooleanVar)).pack(side="left", padx=6)
        ttk.Label(tab, text="　　不填 Key 时也会读取环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=6)
        ttk.Button(r, text="测试连接", command=self.test_cloud).pack(side="left")
        ttk.Button(r, text="获取模型列表", command=self.fetch_cloud_models).pack(side="left", padx=6)
        self.cloud_hint = ttk.Label(tab, text="", style="Muted.TLabel", wraplength=660)
        self.cloud_hint.pack(anchor="w")
        self.load_profile()

        # ---------------- 隐私与存储 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="隐私与存储")
        for key, text, hint in (
            ("privacy.encrypt_db", "题库正文加密存储（推荐）",
             "题干、作答、错题报告在数据库里加密保存，别人拷走 archive.db 也读不出内容。"),
            ("privacy.send_image_to_cloud", "允许把图片传给云端模型",
             "关掉后云端只收到本地识别出的文字；开启云端深度分析时更准。"),
            ("privacy.delete_local_after_cloud", "云端处理完删除本地原图（不推荐）",
             "开启后本地不再保留原图，无法回溯。"),
        ):
            ttk.Checkbutton(tab, text=text, variable=self._var(key, bool(self.cfg.get(key, True)),
                                                              tk.BooleanVar)).pack(anchor="w", pady=(6, 0))
            ttk.Label(tab, text="　　" + hint, style="Muted.TLabel", wraplength=660).pack(anchor="w")
        ttk.Separator(tab).pack(fill="x", pady=12)
        ttk.Label(tab, text="数据都在本机：数据库、原图、归档目录、日志全部在 data 文件夹里，"
                            "拷贝这个文件夹即可迁移，删除即彻底清除。",
                  wraplength=660, style="Muted.TLabel").pack(anchor="w")

        # ---------------- 归档 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="归档")
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="归档根目录", width=18).pack(side="left")
        ttk.Entry(r, textvariable=self._var("archive.root", self.cfg.get("archive.root", ""))
                  ).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="浏览", width=6, command=lambda: self._pick_dir("archive.root")).pack(side="left")
        ttk.Label(tab, text="　　留空 = data/归档", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self._row(tab, "目录模板", "archive.pattern",
                  "可用变量：{subject} {subject_detail} {grade} {qtype} {status} {knowledge} "
                  "{year} {month} {source}（{knowledge} 取该题第一个知识点，如「复数」）")
        self._row(tab, "文件名模板", "archive.naming", "可用变量：{uid} {status} {subject} {qtype}")
        for key, text in (("archive.write_text", "归档时写 Markdown 文字稿"),
                          ("archive.write_report", "归档时写错题分析报告"),
                          ("archive.copy_original", "归档时保留原图（推荐）")):
            ttk.Checkbutton(tab, text=text, variable=self._var(key, bool(self.cfg.get(key, True)),
                                                              tk.BooleanVar)).pack(anchor="w", pady=2)

        # ---------------- 界面与公式 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="界面与公式")
        ttk.Label(tab, text="公式显示", style="Title.TLabel").pack(anchor="w")
        r = ttk.Frame(tab)
        r.pack(fill="x", pady=3)
        ttk.Label(r, text="公式大小倍率", width=18).pack(side="left")
        ttk.Spinbox(r, from_=0.8, to=1.8, increment=0.05, width=8,
                    textvariable=self._var("ui.math_scale",
                                           self.cfg.get("ui.math_scale", 1.15),
                                           tk.DoubleVar)).pack(side="left")
        ttk.Label(r, text="　（1.0 = 与正文行高相同；嫌小就调到 1.3~1.5，改完点保存并重新选一下题目）",
                  style="Muted.TLabel").pack(side="left")
        r2 = ttk.Frame(tab)
        r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="渲染精细度", width=18).pack(side="left")
        ttk.Combobox(r2, width=10, state="readonly",
                     values=["1（最快）", "2", "3（推荐）", "4（最清晰）"],
                     textvariable=self._var("ui.math_supersample_text",
                                            f"{int(self.cfg.get('ui.math_supersample', 3) or 3)}"
                                            + ("（推荐）" if int(self.cfg.get('ui.math_supersample', 3) or 3) == 3 else "")
                                            )).pack(side="left")
        ttk.Label(r2, text="　超采样倍数：越大越清晰、渲染稍慢（公式很多时才有感知差别）",
                  style="Muted.TLabel").pack(side="left")
        ttk.Label(tab, text="　　数学公式由本机排版（分数、根号、上下标、矩阵），离线可用、无需联网。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(6, 0))
        ttk.Separator(tab).pack(fill="x", pady=12)
        ttk.Label(tab, text="窗口与预览", style="Title.TLabel").pack(anchor="w")
        self._row(tab, "界面字体", "ui.font_family", "换字体后需要重启软件")
        self._row(tab, "字号", "ui.font_size", "正文基准字号（公式大小按它换算）")
        self._row(tab, "预览区最长边", "ui.image_display_max", "图片预览的显示尺寸上限（像素）")

        # ---------------- 关于 ----------------
        tab = ttk.Frame(nb, padding=14)
        nb.add(tab, text="关于")
        from .. import paths

        ttk.Label(tab, text=f"{paths.APP_NAME}　v{paths.APP_VERSION}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(tab, text="本地 + 云端混合：本地轻量模型离线识别，云端强力模型兜底增强。\n"
                            "所有代码开源、无付费依赖、无广告、无会员限制。\n"
                            f"\n数据目录：{paths.data_dir()}\n配置：{paths.config_path()}\n日志：{paths.log_file()}",
                  wraplength=660, justify="left").pack(anchor="w", pady=10)
        ttk.Button(tab, text="打开数据目录", command=lambda: self._open_path(str(paths.data_dir()))).pack(anchor="w")

        # ---------------- 底部按钮 ----------------
        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="恢复默认", command=self.restore_defaults).pack(side="left")
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="保存", command=self.save).pack(side="right", padx=8)

    # ------------------------------------------------------------------ #
    def _pick_file(self, key: str) -> None:
        path = filedialog.askopenfilename(title="选择程序", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self._vars[key].set(path)

    def _pick_dir(self, key: str) -> None:
        path = filedialog.askdirectory(title="选择目录")
        if path:
            self._vars[key].set(path)

    @staticmethod
    def _open_path(path: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["open" if os.uname().sysname == "Darwin" else "xdg-open", path])
        except Exception as exc:
            messagebox.showwarning("打开失败", str(exc))

    # ------------------------------------------------------------------ #
    def refresh_models(self) -> None:
        """从本地服务读取已安装模型，填进下拉框（顺便报一下模型有多少装在显存里）。"""
        self.apply_profile_fields()
        self.cfg.update(self._collect())
        client = ModelClient("local", self.cfg)

        def work():
            try:
                models = client.manager.list_models()
            except Exception as exc:
                models = []
                log.warning("读取模型列表失败：%s", exc)
            status = ""
            try:
                st = client.manager.offload_status()
                if st.get("found"):
                    status = ("⚠️ " if st.get("level") in ("bad", "partial") else "") + st.get("text", "")
            except Exception:
                status = ""
            self.after(0, lambda: self._set_models(models, status))

        self.model_hint.configure(text="　　正在读取模型列表…")
        threading.Thread(target=work, daemon=True).start()

    def _set_models(self, models, status: str = "") -> None:
        if models:
            self.model_box.configure(values=models)
            text = f"　　发现 {len(models)} 个模型：{'、'.join(models[:6])}"
        else:
            text = ("　　没读到模型：请确认本地服务在运行"
                    "（ollama serve），并且已经 ollama pull 过视觉模型")
        if status:
            text += f"\n　　{status}"
        self.model_hint.configure(text=text)

    def reload_local_model(self) -> None:
        """卸载再重新加载本地模型，让 Ollama 按当前空闲显存重新规划分层。

        对应「识别突然变慢」这个坑：Ollama 只在加载那一刻决定 GPU/CPU 分层，之后即使显存
        空出来也不会重新规划，于是慢档会一直持续。实测重载后 6.6 tok/s → 74~80 tok/s。
        """
        self.apply_profile_fields()
        self.cfg.update(self._collect())
        client = ModelClient("local", self.cfg)
        self.model_hint.configure(text="　　正在重新加载模型（可能要几十秒，请稍候）…")

        def work():
            try:
                res = client.manager.reload_model()
                msg = "　　" + str(res.get("message", ""))
                if not res.get("ok"):
                    msg = "⚠️ " + msg.lstrip()
            except Exception as exc:
                msg = f"　　重新加载失败 ❌：{exc}"
            self.after(0, lambda: self.model_hint.configure(text=msg))

        threading.Thread(target=work, daemon=True).start()

    def test_local(self) -> None:
        self.apply_profile_fields()
        self.cfg.update(self._collect())
        self.model_hint.configure(text="　　正在测试本地模型连通性…")

        def work():
            res = ModelClient("local", self.cfg).ping(timeout=60)
            msg = ("　　本地模型正常 ✅（回复：" + (res.text or "").strip()[:20] + "）") if res.ok else \
                  f"　　连接失败 ❌：{res.error}"
            self.after(0, lambda: self.model_hint.configure(text=msg))

        threading.Thread(target=work, daemon=True).start()

    def test_cloud(self) -> None:
        self.apply_profile_fields()
        self.cfg.update(self._collect())
        self.cloud_hint.configure(text="正在测试云端连通性…")

        def work():
            try:
                res = ModelClient("cloud", self.cfg).ping(timeout=40)
                msg = "云端连接正常 ✅" if res.ok else f"连接失败 ❌：{res.error}"
            except Exception as exc:
                msg = f"连接异常 ❌：{exc}"
            self.after(0, lambda: self.cloud_hint.configure(text=msg))

        threading.Thread(target=work, daemon=True).start()

    def fetch_cloud_models(self) -> None:
        self.apply_profile_fields()
        self.cfg.update(self._collect())
        prof = self.cfg.active_cloud()
        base = str(prof.get("base_url", "")).rstrip("/")
        key = str(prof.get("api_key", "")).strip() or self.cfg.env_api_key()
        self.cloud_hint.configure(text="正在获取模型列表…")

        def work():
            import json
            import urllib.request

            try:
                req = urllib.request.Request(f"{base}/models", headers=(
                    {"Authorization": f"Bearer {key}"} if key else {}))
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                ids = [m.get("id", "") for m in data.get("data", [])][:20]
                msg = "可用模型：" + "、".join(ids) if ids else "服务未返回模型列表"
            except Exception as exc:
                msg = f"获取失败：{exc}（部分服务不提供 /models 接口，可手动填写模型名）"
            self.after(0, lambda: self.cloud_hint.configure(text=msg))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 云端档案
    # ------------------------------------------------------------------ #
    def _current_profile_index(self) -> int:
        name = self.profile_box.get()
        profiles = self.cfg.get("cloud.profiles", []) or []
        for i, p in enumerate(profiles):
            if p.get("name") == name:
                return i
        return 0

    def load_profile(self) -> None:
        """把选中的云端档案读进「当前编辑中」的字段。"""
        profiles = self.cfg.get("cloud.profiles", []) or []
        if not profiles:
            return
        idx = min(self._current_profile_index(), len(profiles) - 1)
        self.profile_box.set(profiles[idx].get("name", ""))
        prof = profiles[idx]
        for key, field in (("cloud.cur.name", "name"), ("cloud.cur.base_url", "base_url"),
                           ("cloud.cur.model", "model"), ("cloud.cur.api_key", "api_key")):
            if key in self._vars:
                self._vars[key].set(prof.get(field, ""))
            else:
                self._var(key, prof.get(field, ""))
        sv = self._var("cloud.cur.supports_vision", bool(prof.get("supports_vision", False)),
                       tk.BooleanVar)
        sv.set(bool(prof.get("supports_vision", False)))

    def apply_profile_fields(self) -> None:
        """把「当前编辑中」的字段写回所选档案。"""
        profiles = self.cfg.get("cloud.profiles", []) or []
        if not profiles:
            return
        idx = min(self._current_profile_index(), len(profiles) - 1)
        prof = dict(profiles[idx])
        for key, field in (("cloud.cur.name", "name"), ("cloud.cur.base_url", "base_url"),
                           ("cloud.cur.model", "model"), ("cloud.cur.api_key", "api_key")):
            if key in self._vars:
                prof[field] = self._vars[key].get()
        if "cloud.cur.supports_vision" in self._vars:
            prof["supports_vision"] = bool(self._vars["cloud.cur.supports_vision"].get())
        profiles[idx] = prof
        self.cfg.set("cloud.profiles", profiles)
        self.cfg.set("cloud.active", idx)

    def add_profile(self) -> None:
        self.apply_profile_fields()
        profiles = list(self.cfg.get("cloud.profiles", []) or [])
        profiles.append({"name": f"新配置 {len(profiles) + 1}", "base_url": "https://api.openai.com/v1",
                         "model": "gpt-4o-mini", "api_key": "", "supports_vision": True,
                         "timeout": 180, "max_tokens": 4096, "temperature": 0.3})
        self.cfg.set("cloud.profiles", profiles)
        self.cfg.set("cloud.active", len(profiles) - 1)
        self.profile_box.configure(values=[p.get("name", "") for p in profiles])
        self.profile_box.set(profiles[-1]["name"])
        self.load_profile()

    def del_profile(self) -> None:
        profiles = list(self.cfg.get("cloud.profiles", []) or [])
        if len(profiles) <= 1:
            messagebox.showinfo("提示", "至少要保留一套云端配置")
            return
        idx = self._current_profile_index()
        profiles.pop(idx)
        self.cfg.set("cloud.profiles", profiles)
        self.cfg.set("cloud.active", 0)
        self.profile_box.configure(values=[p.get("name", "") for p in profiles])
        self.profile_box.set(profiles[0].get("name", ""))
        self.load_profile()

    # ------------------------------------------------------------------ #
    def _collect(self) -> Dict:
        """把界面上的值收集成配置补丁。"""
        patch: Dict = {}
        bools = ("run.auto_preprocess", "run.auto_analyze", "run.cloud_on_error",
                 "run.cloud_for_analysis", "run.cloud_for_variants", "local.auto_start",
                 "local.disable_thinking", "cloud.enabled", "privacy.encrypt_db",
                 "privacy.send_image_to_cloud", "privacy.delete_local_after_cloud",
                 "archive.write_text", "archive.write_report", "archive.copy_original")
        numbers = {"run.confidence_threshold": float, "run.quality_threshold": float,
                   "run.max_questions_per_image": int, "local.timeout": float,
                   "local.max_image_side": int, "local.jpeg_quality": int,
                   "ui.math_scale": float, "ui.font_size": int, "ui.image_display_max": int}
        for key, var in self._vars.items():
            if key.startswith("cloud.cur."):
                continue
            if key == "ui.math_supersample_text":        # 下拉框里的「3（推荐）」取数字部分
                try:
                    patch["ui.math_supersample"] = int(str(var.get()).strip()[0])
                except Exception:
                    pass
                continue
            val = var.get()
            if key in bools:
                patch[key] = bool(val)
            elif key in numbers:
                try:
                    patch[key] = numbers[key](val)
                except Exception:
                    continue
            else:
                patch[key] = val
        return patch

    def restore_defaults(self) -> None:
        if not messagebox.askyesno("恢复默认", "把本地模型、云端、归档等设置恢复为默认值？\n"
                                              "（题库数据不受影响）"):
            return
        from ..config import DEFAULTS

        self.cfg.update({k: v for k, v in DEFAULTS.items() if k != "cloud"})
        self.cfg.update({"cloud": DEFAULTS["cloud"]})
        self.cfg.save()
        messagebox.showinfo("已恢复", "已恢复默认设置，窗口将关闭，请重新打开查看。")
        if self.on_saved:
            self.on_saved()
        self.destroy()

    def save(self) -> None:
        patch = self._collect()
        self.cfg.update(patch)
        self.apply_profile_fields()
        # 本地模型路径为空时尝试自动探测
        if not str(self.cfg.get("local.server_exe", "")).strip():
            found = default_ollama_paths()
            if found.get("exe"):
                self.cfg.set("local.server_exe", found["exe"])
            if found.get("models_dir") and not str(self.cfg.get("local.models_dir", "")).strip():
                self.cfg.set("local.models_dir", found["models_dir"])
        try:
            self.cfg.save()
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        log.info("设置已保存")
        if self.on_saved:
            self.on_saved()
        self.destroy()
