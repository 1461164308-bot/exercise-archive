# -*- coding: utf-8 -*-
"""归档：按「学科 / 学段 / 题型 / 状态」自动建目录，原图 + 预处理图 + 文字稿 + 错题报告一起落盘。

目录结构（可在设置里改模板 ``archive.pattern``）::

    data/归档/数学/大学/选择题/错题/
        Q20260918-0007.jpg            原图（可关）
        Q20260918-0007_prep.jpg       预处理图（纠偏/提亮后的）
        Q20260918-0007.md             文字稿：题干/选项/作答/答案/知识点
        Q20260918-0007_错题分析.md     错题分析报告（仅错题有）

这样做的好处：不打开软件也能用资源管理器直接翻看、按学科打印、拷给同学；
数据库只是索引，文件才是「看得见摸得着」的归档成果。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from . import paths, prompts
from .analysis import question_to_text, report_to_text
from .logging_setup import get_logger

log = get_logger("archive")

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(text: str, fallback: str = "未命名", limit: int = 60) -> str:
    """把任意文本变成合法的文件名/目录名。"""
    s = _ILLEGAL.sub("_", str(text or "").strip())
    s = s.strip(". ")
    if not s:
        s = fallback
    return s[:limit]


STATUS_CN = {"wrong": "错题", "correct": "正确题", "blank": "未做题", "unknown": "未判定"}


def _first_knowledge(q: Dict) -> str:
    """取这道题的**第一个**知识点，供 ``{knowledge}`` 目录模板使用。

    一道题通常有多个知识点（如"复数、单位根、复数的几何表示"），目录只能取一个 ——
    取识别时排在第一位（也就是最核心的那个），其余知识点仍完整写在文字稿与报告里。
    """
    kps = q.get("knowledge_points") or []
    if isinstance(kps, str):
        kps = [kps]
    for k in kps:
        if isinstance(k, dict):
            k = k.get("name") or k.get("知识点") or ""
        name = str(k).strip()
        if name:
            return name
    return "未标知识点"


class Archiver:
    """归档器：负责目录规划、文件写入与索引维护。"""

    def __init__(self, cfg, store=None):
        self.cfg = cfg
        self.store = store

    # -- 目录规划 ---------------------------------------------------------- #
    def root(self):
        """归档根目录：优先用传入的配置对象里的 ``archive.root``，否则用全局配置/默认值。

        （早期版本这里只看全局单例配置，导致「传入的 cfg 被忽略」——既不好测，
        也让设置改动在某些调用路径上不生效。）
        """
        custom = ""
        if self.cfg is not None:
            try:
                custom = str(self.cfg.get("archive.root", "") or "").strip()
            except Exception:
                custom = ""
        if custom:
            p = Path(os.path.expandvars(os.path.expanduser(custom)))
            p.mkdir(parents=True, exist_ok=True)
            return p
        return paths.archive_dir()

    def folder_for(self, q: Dict) -> str:
        """按配置模板算出该题的归档目录（并创建）。"""
        pattern = str(self.cfg.get("archive.pattern", "{subject}/{grade}/{qtype}/{status}") or
                      "{subject}/{grade}/{qtype}/{status}")
        values = {
            "subject": safe_name(q.get("subject") or "未分类"),
            "subject_detail": safe_name(q.get("subject_detail") or q.get("subject") or "未分类"),
            "grade": safe_name(q.get("grade") or "未分学段"),
            "qtype": safe_name(q.get("qtype") or q.get("type") or "其他"),
            "status": STATUS_CN.get(str(q.get("status")), "未判定"),
            "knowledge": safe_name(_first_knowledge(q)),
            "year": str(q.get("created_at", ""))[:4] or "未标年份",
            "month": str(q.get("created_at", ""))[5:7] or "00",
            "source": safe_name(q.get("source") or "未标来源"),
        }
        try:
            rel = pattern.format(**values)
        except Exception:
            rel = "{subject}/{grade}/{qtype}/{status}".format(**values)
        folder = os.path.join(str(self.root()), *[p for p in rel.split("/") if p.strip()])
        os.makedirs(folder, exist_ok=True)
        return folder

    def question_file_stem(self, q: Dict) -> str:
        naming = str(self.cfg.get("archive.naming", "{uid}_{status}") or "{uid}_{status}")
        try:
            return safe_name(naming.format(
                uid=q.get("uid", "Q"), status=STATUS_CN.get(str(q.get("status")), "未判定"),
                subject=q.get("subject", ""), qtype=q.get("qtype") or q.get("type", ""),
            ), "题目")
        except Exception:
            return safe_name(q.get("uid") or "题目")

    # -- 文字稿 ------------------------------------------------------------ #
    def question_markdown(self, q: Dict, report: Optional[Dict] = None) -> str:
        lines = [
            f"# {q.get('uid', '')}　{prompts.status_cn(str(q.get('status', '')))}",
            "",
            f"- **学科**：{q.get('subject', '')}（{q.get('subject_detail', '')}）",
            f"- **学段**：{q.get('grade', '')}",
            f"- **题型**：{q.get('qtype') or q.get('type', '')}",
            f"- **难度**：{q.get('difficulty', '')}",
            f"- **知识点**：{'、'.join(str(k) for k in (q.get('knowledge_points') or []))}",
            f"- **识别引擎**：{q.get('engine', '')}　置信度：{float(q.get('confidence') or 0):.2f}",
            f"- **来源图片**：{q.get('source_image', '')}",
        ]
        if q.get("tags"):
            lines.append(f"- **标签**：{'、'.join(str(t) for t in q['tags'])}")
        lines += ["", "## 题干", "", str(q.get("stem") or "（未识别）"), ""]
        if q.get("options"):
            lines += ["## 选项", ""] + [f"- {o}" for o in q["options"]] + [""]
        if q.get("student_answer") or q.get("student_steps"):
            lines += ["## 我的作答", ""]
            if q.get("student_answer"):
                lines.append(f"- 答案：{q['student_answer']}")
            if q.get("student_steps"):
                lines += ["", q["student_steps"]]
            lines.append("")
        if q.get("correct_answer"):
            lines += ["## 参考答案", "", str(q["correct_answer"]), ""]
        if report:
            lines += ["## 错题分析", "", report_to_text(q, report), ""]
        return "\n".join(lines)

    # -- 归档单题 ---------------------------------------------------------- #
    def archive_question(self, q: Dict, report: Optional[Dict] = None) -> Dict:
        """把一道题归档到磁盘，返回 ``{'folder':…, 'files':[…] }``。"""
        folder = self.folder_for(q)
        stem = self.question_file_stem(q)
        files: List[str] = []
        written: Dict[str, str] = {}

        # 1) 图片
        if bool(self.cfg.get("archive.copy_original", True)):
            src = q.get("image_path") or q.get("original_path")
            if src and os.path.exists(src):
                dst = os.path.join(folder, f"{stem}{os.path.splitext(src)[1] or '.jpg'}")
                if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                    try:
                        shutil.copy2(src, dst)
                        files.append(dst)
                        written["original"] = dst
                    except Exception as exc:
                        log.warning("复制原图失败：%s", exc)
        work = q.get("work_path") or q.get("prep_path")
        if work and os.path.exists(work) and os.path.abspath(work) != os.path.abspath(str(q.get("image_path") or "")):
            dst = os.path.join(folder, f"{stem}_prep{os.path.splitext(work)[1] or '.jpg'}")
            try:
                shutil.copy2(work, dst)
                files.append(dst)
                written["prep"] = dst
            except Exception as exc:
                log.warning("复制预处理图失败：%s", exc)

        # 2) 文字稿
        if bool(self.cfg.get("archive.write_text", True)):
            md = os.path.join(folder, f"{stem}.md")
            with open(md, "w", encoding="utf-8") as f:
                f.write(self.question_markdown(q, report if bool(self.cfg.get("archive.write_report", True)) else None))
            files.append(md)
            written["text"] = md

        # 3) 单独的报告文件（方便直接打印错题分析）
        if report and bool(self.cfg.get("archive.write_report", True)):
            rp = os.path.join(folder, f"{stem}_错题分析.md")
            with open(rp, "w", encoding="utf-8") as f:
                f.write("# " + str(q.get("uid", "")) + " 错题分析\n\n")
                f.write(report_to_text(q, report))
            files.append(rp)
            written["report"] = rp

        written["folder"] = folder
        written["files"] = files
        return written

    # -- 批量归档 ---------------------------------------------------------- #
    def archive_many(self, items: List[Dict], on_item=None) -> List[Dict]:
        """``items`` 为 ``[{'question': q, 'report': r or None}, …]``。"""
        out = []
        for i, item in enumerate(items, start=1):
            q = item.get("question") or item
            rep = item.get("report")
            try:
                res = self.archive_question(q, rep)
            except Exception as exc:
                log.warning("归档失败（%s）：%s", q.get("uid"), exc)
                res = {"folder": "", "files": [], "error": str(exc)}
            out.append(res)
            if on_item:
                on_item(i, len(items), q, res)
        return out

    # -- 归档索引 ---------------------------------------------------------- #
    def write_index(self, stats: Optional[Dict] = None) -> str:
        """在归档根目录写一份人类可读的索引（学科/状态统计 + 目录说明）。"""
        root = self.root()
        lines = [
            "# 习题归档索引",
            "",
            "本目录由「AI本地错题习题归档整理工具」自动维护：",
            "",
            "```",
            "学科 / 学段 / 题型 / 状态 / 题目文件",
            "```",
            "",
            "每个题目目录里有：原图、预处理图（纠偏提亮后）、文字稿（.md）、错题分析（错题才有）。",
            "",
        ]
        if stats:
            lines += [
                "## 统计",
                "",
                f"- 图片：{stats.get('images', 0)} 张",
                f"- 题目：{stats.get('questions', 0)} 道（错题 {stats.get('wrong', 0)}、"
                f"正确 {stats.get('correct', 0)}、未做 {stats.get('blank', 0)}）",
                f"- 错题分析报告：{stats.get('reports', 0)} 份",
                "",
                "### 各学科题目数",
                "",
            ]
            for k, v in (stats.get("by_subject") or {}).items():
                lines.append(f"- {k}：{v}")
            lines.append("")
            if stats.get("weak_knowledge"):
                lines.append("### 薄弱知识点（错题次数）")
                lines.append("")
                for k, v in stats["weak_knowledge"].items():
                    lines.append(f"- {k}：{v}")
                lines.append("")
        path = os.path.join(str(root), "归档索引.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
