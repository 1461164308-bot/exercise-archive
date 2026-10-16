# -*- coding: utf-8 -*-
"""端到端识别验证：拿 samples/ 里的标准样张跑一遍完整流水线，和 ground_truth.json 对答案。

用法::

    python tools/verify_recognition.py                 # 跑全部样张（用当前配置的引擎）
    python tools/verify_recognition.py --only s1 s6    # 只跑指定样张
    python tools/verify_recognition.py --force cloud   # 强制走云端

输出：
- 控制台逐张对比（学科 / 题数 / 每题状态 / 置信度 / 用时）
- ``samples/recognition_report.json``：完整原始结果，方便回溯与调参
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

from exerarchive import config, logging_setup, paths  # noqa: E402
from exerarchive.pipeline import Pipeline  # noqa: E402

STATUS_CN = {"wrong": "错题", "correct": "正确", "blank": "未做", "unknown": "未判定"}


def load_truth(sample_dir: str = "samples") -> dict:
    path = os.path.join(ROOT, sample_dir, "ground_truth.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description="习题识别端到端验证")
    ap.add_argument("--only", nargs="*", default=None, help="只跑指定样张前缀，如 s1 s6 或 u1 u5")
    ap.add_argument("--force", default=None, choices=["local", "cloud"], help="强制指定引擎")
    ap.add_argument("--no-preprocess", action="store_true", help="跳过自动预处理（测原图识别率）")
    ap.add_argument("--dir", default="samples",
                    help="样张目录（默认 samples；指向 samples\\大学数学 就跑大学数学那套完备性样张）")
    ap.add_argument("--out", default=None,
                    help="结果 JSON 输出路径（默认写在样张目录下的 recognition_report.json）")
    ap.add_argument("--use-real-data", action="store_true",
                    help="用真实 data/ 目录（默认用临时目录，避免把预处理图写进你的数据目录）")
    args = ap.parse_args()
    if not args.out:
        args.out = os.path.join(ROOT, args.dir, "recognition_report.json")

    # 默认把数据目录切到临时位置：这个脚本会写预处理图（data/preprocessed），
    # 早期版本直接用真实 data/，跑一次验证就在用户的数据目录里留一堆 _prep.jpg。
    if not args.use_real_data:
        os.environ["EXER_HOME"] = os.path.join(ROOT, "verify", "_recognition")
    print(f"数据目录：{os.environ.get('EXER_HOME') or '（真实 data/）'}")

    logging_setup.setup()
    cfg = config.load_config()
    config.bootstrap_local_model(cfg)
    truth = load_truth(args.dir)

    pipe = Pipeline(cfg, on_log=lambda m: print("   ", m, flush=True))
    print(f"运行模式：{cfg.mode_label()}　本地模型：{cfg.get('local.model')}　"
          f"云端：{'已启用' if cfg.cloud_enabled() else '未启用'}")
    print("=" * 78)

    results = []
    total_q = hit_q = total_status = hit_status = 0
    t_all = time.time()

    for name, expect in truth.items():
        stem = name.split("_")[0]
        if args.only and stem not in args.only:
            continue
        path = os.path.join(ROOT, args.dir, name)
        print(f"\n▶ {name}（期望：{expect['subject']} / {expect['question_count']} 题 / "
              f"{[''.join(STATUS_CN.get(s, s)) for s in expect['expected_statuses']]}）")
        out = pipe.process_image(path, force=args.force, skip_preprocess=args.no_preprocess)

        got_status = [q.get("status") for q in out.questions]
        exp_status = list(expect["expected_statuses"])
        n_ok = sum(1 for i, s in enumerate(exp_status) if i < len(got_status) and got_status[i] == s)
        count_ok = len(got_status) == expect["question_count"]
        subject_ok = out.questions and out.questions[0].get("subject") == expect["subject"]

        total_q += expect["question_count"]
        hit_q += min(len(got_status), expect["question_count"]) if count_ok else 0
        total_status += len(exp_status)
        hit_status += n_ok

        mark = "✅" if (count_ok and n_ok == len(exp_status) and subject_ok) else "⚠️"
        print(f"  {mark} 识别：{len(got_status)} 题　学科：{out.questions[0].get('subject') if out.questions else '—'}"
              f"（{out.questions[0].get('subject_detail') if out.questions else ''}）"
              f"　状态命中 {n_ok}/{len(exp_status)}　用时 {out.elapsed:.1f}s")
        for i, q in enumerate(out.questions, start=1):
            exp = exp_status[i - 1] if i <= len(exp_status) else "?"
            flag = "✓" if STATUS_CN.get(q.get("status")) == STATUS_CN.get(exp) else "✗"
            print(f"     {flag} 第{i}题 [{q.get('type')}/{STATUS_CN.get(q.get('status'))}] "
                  f"conf={q.get('confidence', 0):.2f}  {str(q.get('stem'))[:46]}…")
            if q.get("student_answer"):
                print(f"        学生作答：{q['student_answer'][:60]}")

        results.append({
            "file": name,
            "expect": expect,
            "subject": out.questions[0].get("subject") if out.questions else "",
            "subject_detail": out.questions[0].get("subject_detail") if out.questions else "",
            "grade": out.questions[0].get("grade") if out.questions else "",
            "engine": out.engine, "route": out.route, "confidence": out.confidence,
            "elapsed": round(out.elapsed, 2), "quality": out.quality,
            "preprocess": out.preprocess_steps, "error": out.error,
            "questions": out.questions, "status_hits": n_ok,
            "count_ok": count_ok, "subject_ok": bool(subject_ok),
        })

    print("\n" + "=" * 78)
    print(f"题数准确率：{hit_q}/{total_q}")
    print(f"状态（对/错/空白）准确率：{hit_status}/{total_status}")
    print(f"总用时：{time.time() - t_all:.1f}s")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "mode": cfg.mode(), "model": cfg.get("local.model"),
                   "results": results}, f, ensure_ascii=False, indent=1)
    print(f"完整结果已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
