# -*- coding: utf-8 -*-
"""命令行批量导入识别（无界面）：适合服务器/定时任务/批量处理一个学期的卷子。

用法::

    python tools/import_cli.py 照片目录                # 识别目录下所有图片
    python tools/import_cli.py 照片目录 --force local   # 强制本地
    python tools/import_cli.py a.jpg b.jpg --no-analyze # 不生成错题分析
    python tools/import_cli.py 照片目录 --json 报告.json

流程与图形界面完全一致：导入 → 预处理 → 识别（本地优先/云端兜底）→ 入库 → 归档 → 错题分析。
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

from exerarchive import config, imageops, logging_setup, paths  # noqa: E402
from exerarchive.analysis import Analyzer  # noqa: E402
from exerarchive.archive import Archiver, STATUS_CN  # noqa: E402
from exerarchive.pipeline import Pipeline  # noqa: E402
from exerarchive.store import Store  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="命令行批量识别习题图片")
    ap.add_argument("paths", nargs="+", help="图片文件或文件夹")
    ap.add_argument("--force", default=None, choices=["local", "cloud"], help="强制指定引擎")
    ap.add_argument("--no-analyze", action="store_true", help="不自动生成错题分析")
    ap.add_argument("--no-archive", action="store_true", help="不写入归档目录")
    ap.add_argument("--no-preprocess", action="store_true", help="跳过自动预处理")
    ap.add_argument("--json", dest="json_out", default=None, help="把结果写入 JSON 文件")
    ap.add_argument("--quiet", action="store_true", help="只打印摘要")
    args = ap.parse_args(argv)

    logging_setup.setup()
    cfg = config.load_config()
    config.bootstrap_local_model(cfg)
    store = Store(encrypt=bool(cfg.get("privacy.encrypt_db", True)))
    pipeline = Pipeline(cfg, on_log=(lambda m: None if args.quiet else print("   ", m, flush=True)))
    analyzer = Analyzer(cfg, on_log=(lambda m: None if args.quiet else print("   ", m, flush=True)))
    archiver = Archiver(cfg, store)

    # 收集图片
    files = []
    for p in args.paths:
        if os.path.isdir(p):
            files.extend(str(x) for x in paths.scan_images(p))
        elif os.path.isfile(p):
            files.append(p)
    if not files:
        print("没有找到任何图片")
        return 1

    print(f"共 {len(files)} 张图片；模式：{cfg.mode_label()}；本地模型：{cfg.get('local.model')}")
    batch = time.strftime("CLI_%Y%m%d_%H%M%S")
    batch_dir = paths.images_dir() / batch
    batch_dir.mkdir(parents=True, exist_ok=True)

    summary = {"batch": batch, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "images": []}
    t0 = time.time()
    n_q = n_wrong = 0

    for i, src in enumerate(files, start=1):
        name = os.path.basename(src)
        print(f"\n[{i}/{len(files)}] {name}")
        try:
            dst = batch_dir / name
            if not dst.exists():
                shutil.copy2(src, dst)
            img_uid = store.add_image(str(dst), original_name=name, batch=batch)
            image_id = store.image_id(img_uid)
            outcome = pipeline.process_image(str(dst), force=args.force,
                                             skip_preprocess=args.no_preprocess)
            uids = []
            for q in outcome.questions:
                rec = dict(q)
                rec["image_path"] = str(dst)
                rec["work_path"] = outcome.work_path
                uid = store.add_question(rec, image_id, name)
                uids.append(uid)
                n_q += 1
                if q.get("status") == "wrong":
                    n_wrong += 1
                    if not args.no_analyze:
                        rec["uid"] = uid
                        rec["source_path"] = str(dst)
                        report = analyzer.analyze(rec)
                        store.save_report(uid, report, depth=report.get("depth", "local"),
                                          engine=report.get("engine", ""))
                if not args.no_archive:
                    rec["uid"] = uid
                    try:
                        res = archiver.archive_question(rec, store.get_report(uid))
                        store.update_question(uid, {"archived": 1,
                                                    "archive_path": res.get("folder", "")})
                    except Exception as exc:
                        print(f"    归档失败：{exc}")
            store.update_image(img_uid, {
                "work_path": outcome.work_path,
                "subject": outcome.questions[0].get("subject", "") if outcome.questions else "",
                "grade": outcome.questions[0].get("grade", "") if outcome.questions else "",
                "status": "wrong" if any(q.get("status") == "wrong" for q in outcome.questions) else "correct",
                "quality": outcome.quality.get("score", 0),
                "engine": outcome.engine, "route": outcome.route,
                "preprocess": "，".join(outcome.preprocess_steps), "error": outcome.error,
            })
            print(f"    → {outcome.question_count} 题，引擎 {outcome.engine or '—'}，"
                  f"用时 {outcome.elapsed:.1f}s")
            summary["images"].append({
                "file": name, "uid": img_uid, "questions": outcome.question_count,
                "uids": uids, "engine": outcome.engine, "route": outcome.route,
                "elapsed": round(outcome.elapsed, 2), "error": outcome.error,
                "statuses": [q.get("status") for q in outcome.questions],
            })
        except Exception as exc:
            print(f"    处理失败：{type(exc).__name__}: {exc}")
            summary["images"].append({"file": name, "error": f"{type(exc).__name__}: {exc}"})

    stats = store.stats()
    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    summary["elapsed"] = round(time.time() - t0, 1)
    summary["stats"] = stats
    print("\n" + "=" * 60)
    print(f"完成：{len(files)} 张图，共识别 {n_q} 道题（错题 {n_wrong} 道），"
          f"用时 {summary['elapsed']}s")
    print(f"题库累计：{stats['questions']} 道题（错题 {stats['wrong']}）")
    print(f"归档目录：{archiver.root()}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print(f"结果已写入：{args.json_out}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
