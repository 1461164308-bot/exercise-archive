# -*- coding: utf-8 -*-
"""测试公共工具：所有测试都在项目内的临时目录里跑（不污染用户数据，也不依赖系统临时目录）。"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TMP_ROOT = os.path.join(ROOT, "tests", "_tmp")


def scratch(name: str = "case") -> str:
    """创建（并清空）一个测试用临时目录，返回路径。"""
    path = os.path.join(TMP_ROOT, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
