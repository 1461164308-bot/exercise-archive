# -*- coding: utf-8 -*-
"""打包交付：把项目压成一个 zip，便于拷贝/分享/留档。

排除：用户数据（``data/``，含个人题库与密钥）、缓存（``__pycache__``）、
测试临时目录（``tests/_tmp``、``verify/_tmp*``、``verify/_workflow``）。

用法::

    python tools/make_release_zip.py                    # 输出到上一级目录
    python tools/make_release_zip.py --out D:\\交付.zip   # 指定输出
    python tools/make_release_zip.py --with-data        # 连数据一起打包（迁移用）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import paths  # noqa: E402

SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "_tmp", "_workflow", "tmp"}
SKIP_DIR_PATHS = {"data"}          # 由 --with-data 控制
SKIP_EXT = {".pyc", ".pyo", ".log"}


def main() -> int:
    ap = argparse.ArgumentParser(description="打包交付 zip")
    ap.add_argument("--out", default=None, help="输出 zip 路径")
    ap.add_argument("--with-data", action="store_true", help="把 data/ 一起打包（迁移/备份用）")
    args = ap.parse_args()

    from exerarchive import logging_setup

    logging_setup.enable_utf8_console()   # 中文输出在 GBK 管道下别乱码/报错

    version = paths.APP_VERSION
    out = args.out or os.path.join(os.path.dirname(ROOT),
                                   f"AI本地错题习题归档整理工具-v{version}.zip")
    count = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for base, dirs, files in os.walk(ROOT):
            rel = os.path.relpath(base, ROOT)
            parts = [] if rel == "." else rel.split(os.sep)
            # 顶层 data/ 只在 --with-data 时打包；其余临时/缓存目录一概跳过
            dirs[:] = [
                d for d in dirs
                if d not in SKIP_DIRS
                and not (d == "data" and not parts and not args.with_data)
            ]
            arc_dir = "exercise-archive" if rel == "." else os.path.join("exercise-archive", rel)
            for name in files:
                if os.path.splitext(name)[1].lower() in SKIP_EXT:
                    continue
                full = os.path.join(base, name)
                try:
                    zf.write(full, os.path.join(arc_dir, name))
                    count += 1
                    total += os.path.getsize(full)
                except Exception as exc:
                    print(f"  跳过 {name}：{exc}")
    size = os.path.getsize(out)
    print(f"已打包：{out}")
    print(f"  文件 {count} 个，原始 {paths.human_size(total)}，压缩后 {paths.human_size(size)}"
          f"（压缩率 {size / max(1, total) * 100:.0f}%）")
    print(f"  包含 data/：{'是（含个人数据，请谨慎分享）' if args.with_data else '否（用户数据不打包）'}")
    print(f"  打包时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
