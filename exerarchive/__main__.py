# -*- coding: utf-8 -*-
"""程序入口：``python -m exerarchive`` 启动图形界面。

也支持几个命令行参数，方便排障与自动化::

    python -m exerarchive                 # 打开图形界面
    python -m exerarchive --selfcheck     # 只做环境自检（不开窗口）
    python -m exerarchive --version       # 打印版本
    python -m exerarchive --import DIR    # 不开窗口，批量识别一个目录（CLI 模式）
"""

from __future__ import annotations

import argparse
import sys

from . import logging_setup, paths


def main(argv=None) -> int:
    # 命令行模式下中文/符号要能安全打印（重定向到文件时中文 Windows 是 GBK）
    logging_setup.enable_utf8_console()
    ap = argparse.ArgumentParser(prog="exerarchive", description=paths.APP_NAME)
    ap.add_argument("--version", action="store_true", help="打印版本号")
    ap.add_argument("--selfcheck", action="store_true", help="只做环境自检，不打开界面")
    ap.add_argument("--no-model", action="store_true",
                    help="配合 --selfcheck：跳过本地模型视觉实测（更快）")
    ap.add_argument("--json", action="store_true", help="配合 --selfcheck：输出 JSON")
    ap.add_argument("--out", default=None, help="配合 --selfcheck：把 JSON 写入指定文件")
    ap.add_argument("--import", dest="import_dir", default=None,
                    help="命令行批量识别一个文件夹（无界面）")
    ap.add_argument("--force", default=None, choices=["local", "cloud"], help="CLI 模式强制引擎")
    args = ap.parse_args(argv)

    if args.version:
        print(f"{paths.APP_NAME} v{paths.APP_VERSION}")
        return 0

    if args.selfcheck:
        sys.path.insert(0, str(paths.tools_dir()))
        import selfcheck  # type: ignore

        extra = []
        if args.no_model:
            extra.append("--no-model")
        if args.json:
            extra.append("--json")
        if args.out:
            extra += ["--out", args.out]
        return selfcheck.main(extra)

    if args.import_dir:
        sys.path.insert(0, str(paths.tools_dir()))
        import import_cli  # type: ignore

        return import_cli.main([args.import_dir] + (["--force", args.force] if args.force else []))

    from .ui.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
