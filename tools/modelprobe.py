# -*- coding: utf-8 -*-
"""本地模型探测：列出可用模型、看模型能力、实测识图速度，并给出选型建议。

用法::

    python tools/modelprobe.py                 # 列出本地服务与模型
    python tools/modelprobe.py --start         # 先自动拉起本地服务再列
    python tools/modelprobe.py --vision        # 用一张自造图片实测识图能力与速度
    python tools/modelprobe.py --set qwen3-vl:4b   # 把某个模型写进配置
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import config, gdiplus, imageops, logging_setup  # noqa: E402
from exerarchive.vlm import ModelClient, _get_json, _split_base  # noqa: E402

VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "qwen2.5-vl", "qwen3-vl", "gemma3", "internvl")


def show_models(cfg) -> list:
    client = ModelClient("local", cfg)
    kind = str(cfg.get("local.kind", "ollama"))
    base = str(cfg.get("local.base_url", ""))
    print(f"本地服务：{kind} @ {base}　{'在线 ✅' if client.manager.alive() else '离线 ❌'}")
    models = client.manager.list_models()
    if not models:
        print("  （没有发现模型。Ollama 用户请执行：ollama pull qwen3-vl:4b）")
        return []
    print(f"共 {len(models)} 个模型：")
    cur = str(cfg.get("local.model", ""))
    for m in models:
        vision = "⌾识图" if any(h in m.lower() for h in VISION_HINTS) else "  纯文本"
        mark = " ←当前" if m == cur else ""
        extra = ""
        if kind == "ollama":
            try:
                info = _get_json(f"{_split_base(base)[0]}/api/show", 6) if False else None
            except Exception:
                info = None
        print(f"  - {m:<45} {vision}{mark}{extra}")
    if kind == "ollama":
        # 逐个查 /api/show 拿到参数量、量化、能力（可能稍慢）
        for m in models[:6]:
            try:
                import urllib.request

                req = urllib.request.Request(
                    f"{_split_base(base)[0]}/api/show",
                    data=json.dumps({"model": m}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                details = info.get("details") or {}
                caps = "、".join(info.get("capabilities") or [])
                print(f"      · {m}: 参数 {details.get('parameter_size', '?')}　"
                      f"量化 {details.get('quantization_level', '?')}　能力：{caps or '未知'}")
            except Exception as exc:
                print(f"      · {m}: 详情获取失败（{exc}）")
    return models


def vision_test(cfg) -> int:
    client = ModelClient("local", cfg)
    if not client.manager.alive() and not client.manager.ensure_running(on_log=print):
        print("本地服务不可用，无法实测")
        return 1
    if not gdiplus.available():
        print("本机没有 GDI+，跳过实测（可用 samples 里的示例图手工测试）")
        return 0
    print("正在生成测试图（含中文、英文、数学符号）…")
    w, h, px = gdiplus.draw_text_bitmap(
        900, 320,
        lines=[
            "识别测试卷　班级：高一(3)班",
            "1. 计算 12 × 8 + 25 = 121（红笔打叉）",
            "2. 求函数 f(x)=x²-4x+3 的最小值。",
            "学生答案：x=2 时取得最小值 -1 ✓",
        ],
        font_size=30, font_name="SimHei",
    )
    img = imageops.Image(w, h, px)
    for max_side in (1200, 1600):
        t0 = time.time()
        res = client.chat(
            "请把图片里的文字逐行读出来，并说明第 1 题的对错（打叉表示做错）。只输出 JSON："
            '{"lines":["…"],"q1_status":"wrong|correct"}',
            [img.scale_to_fit(max_side)], json_mode=True, timeout=300,
        )
        dt = time.time() - t0
        print(f"\n— 最长边 {max_side}px：{'成功' if res.ok else '失败'}　{dt:.1f}s　"
              f"{res.output_tokens} tokens　{res.tokens_per_sec:.0f} tok/s")
        if res.ok:
            print("  模型输出：", (res.text or "").replace("\n", " ")[:400])
            obj = res.json() or {}
            if obj.get("q1_status"):
                print(f"  第 1 题判定：{obj.get('q1_status')}（期望 wrong）")
        else:
            print("  错误：", res.error)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模型探测")
    ap.add_argument("--start", action="store_true", help="自动拉起本地模型服务")
    ap.add_argument("--vision", action="store_true", help="实测识图能力与速度")
    ap.add_argument("--set", default=None, help="把指定模型写入配置")
    args = ap.parse_args()

    logging_setup.setup()
    cfg = config.load_config()
    config.bootstrap_local_model(cfg)

    if args.start:
        ModelClient("local", cfg).manager.ensure_running(on_log=print)
    models = show_models(cfg)
    if args.set:
        if args.set in models or not models:
            cfg.set("local.model", args.set)
            cfg.save()
            print(f"已把本地模型设置为：{args.set}")
        else:
            print(f"模型 {args.set} 不在列表里，未修改配置")
    if args.vision:
        return vision_test(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
