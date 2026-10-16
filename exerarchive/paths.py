# -*- coding: utf-8 -*-
"""路径与工作目录解析。

设计原则：
- 程序默认「绿色便携」：所有用户数据（数据库、归档图片、日志、密钥）都放在代码目录下的
  ``data/`` 里，拷贝整个文件夹即可迁移、备份、删除。
- 允许用环境变量 ``EXER_HOME`` 覆盖根目录（例如想放到 D 盘或 U 盘）。
- 所有路径都以 ``pathlib.Path`` 返回，调用方无需关心平台差异（Windows / macOS 通用）。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "AI本地错题习题归档整理工具"
APP_EN = "exerarchive"
# 版本号：界面标题、关于页、交付 zip 名、诊断 JSON 都用它。
# 1.0.1：识别变慢的可见化与「重新加载模型」按钮（见 verify/验收报告.md 第七章）
# 1.0.2：按知识点分类与筛选；命令名终止空格/求和上下限等公式排版修复
# 1.1.0：柔和玻璃主题、任务导航、渐进式表单与轻量动效
# 1.1.1：错题分析页增加原题图片对照、收起与放大查看
# 1.1.2：修复自动纠偏旋转方向；错题分析固定显示识别题干与选项
# 1.2.0：学生体验迭代：题目阅读预览、识别阶段与用时、持久日志、固定图片保存入口
APP_VERSION = "1.2.0"

# 图片扩展名（导入对话框、拖拽过滤、目录扫描统一用它）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}


def code_root() -> Path:
    """**代码**所在目录（即 exerarchive 包的上一级）。

    注意与 :func:`app_root` 的区别：``app_root`` 是**数据**根目录，可以被 ``EXER_HOME``
    改到别处（例如数据放 D 盘）；而 tools/、docs/、samples/ 这些**随代码一起发布**的东西
    永远在 ``code_root()`` 下。早期版本用 ``app_root()`` 去找 tools/，一旦用户设了
    ``EXER_HOME`` 就会报 ``No module named 'selfcheck'``。
    """
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """**数据**根目录：默认=代码目录，可用环境变量 EXER_HOME 覆盖。"""
    env = os.environ.get("EXER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return code_root()


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """用户数据总目录。"""
    return _ensure(app_root() / "data")


def db_path() -> Path:
    """SQLite 数据库文件。"""
    return data_dir() / "archive.db"


def key_path() -> Path:
    """本机加密主密钥文件（32 字节随机数，权限仅当前用户可读）。"""
    return data_dir() / "master.key"


def config_path() -> Path:
    """配置 JSON。"""
    return data_dir() / "config.json"


def logs_dir() -> Path:
    """日志目录。"""
    return _ensure(data_dir() / "logs")


def log_file() -> Path:
    return logs_dir() / "app.log"


def images_dir() -> Path:
    """导入的原图仓库（导入时复制一份进来，避免用户误删/移动源文件）。"""
    return _ensure(data_dir() / "images")


def preprocessed_dir() -> Path:
    """预处理（裁剪/去水印/纠偏/提亮）后的图片。"""
    return _ensure(data_dir() / "preprocessed")


def thumbs_dir() -> Path:
    """缩略图缓存。"""
    return _ensure(data_dir() / "thumbs")


def default_archive_dir() -> Path:
    """默认归档根目录（按学科/年级/题型/状态自动建目录）。"""
    return _ensure(data_dir() / "归档")


def archive_dir() -> Path:
    """实际使用的归档根目录，可在设置里改。"""
    from .config import load_config  # 局部导入避免循环依赖

    try:
        custom = str(load_config().get("archive.root", "") or "").strip()
    except Exception:
        custom = ""
    if custom:
        p = Path(os.path.expandvars(os.path.expanduser(custom)))
        return _ensure(p)
    return default_archive_dir()


def backup_dir() -> Path:
    return _ensure(data_dir() / "backup")


def samples_dir() -> Path:
    return code_root() / "samples"


def tools_dir() -> Path:
    """自检、示例图生成等附带工具脚本所在目录（随代码发布，不随 EXER_HOME 变）。"""
    return code_root() / "tools"


def docs_dir() -> Path:
    return code_root() / "docs"


def is_image(p) -> bool:
    """按扩展名判断是否是支持的图片文件。"""
    try:
        return Path(p).suffix.lower() in IMAGE_EXTS
    except Exception:
        return False


def scan_images(folder, recursive: bool = True):
    """扫描文件夹下所有支持的图片，返回排序后的路径列表。"""
    folder = Path(folder)
    out = []
    if not folder.is_dir():
        return out
    it = folder.rglob("*") if recursive else folder.glob("*")
    for p in it:
        if p.is_file() and is_image(p):
            out.append(p)
    return sorted(out, key=lambda x: str(x).lower())


def human_size(n: float) -> str:
    """人类可读体积。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
