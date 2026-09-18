# -*- coding: utf-8 -*-
"""导出：TXT / Markdown / HTML / Word(.docx) / PDF，以及错题本、薄弱知识点清单、错题汇总。

零第三方依赖的实现要点
--------------------
- **Word**：``.docx`` 本质是一个 zip + 一组 XML，用标准库 ``zipfile`` 直接写
  ``[Content_Types].xml`` / ``word/document.xml`` / ``word/media/*`` 即可，图片用
  DrawingML 的 inline 图形内嵌（EMU 单位：1 像素 = 9525 EMU）。
- **PDF**：
  1. 优先用系统自带浏览器的无头模式把 HTML 打印成 PDF（文字可选、体积小、排版好）；
  2. 找不到浏览器时，退回**自带 PDF 生成器**：把每一页文字用 Windows GDI 渲染成图片
     （中文绝不会变方块/乱码），再按 DCTDecode 把 JPEG 塞进 PDF 页面对象，完全离线可用。
- **HTML**：可选把图片转成 base64 内嵌，导出的单个 html 文件发到手机上也能直接看。
"""

from __future__ import annotations

import base64
import datetime as _dt
import html as _html
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths
from .analysis import report_to_text
from .archive import STATUS_CN
from .logging_setup import get_logger
from . import prompts

log = get_logger("export")

EXPORT_FORMATS = {
    "txt": "纯文本 (.txt)",
    "md": "Markdown (.md)",
    "html": "网页 (.html)",
    "docx": "Word 文档 (.docx)",
    "pdf": "PDF 文档 (.pdf)",
}

DEFAULT_FONT = "SimHei"
FALLBACK_FONTS = ["SimHei", "DengXian", "Microsoft YaHei", "SimSun", "Arial"]


def _esc(text) -> str:
    return _html.escape(str(text if text is not None else ""), quote=False)


def _status_cn(status: str) -> str:
    return STATUS_CN.get(str(status), "未判定")


def _as_lines(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.splitlines() if v.strip()]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for v in value:
            out.extend(_as_lines(v))
        return out
    return [str(value)]


# --------------------------------------------------------------------------- #
# 内容组织：把题目 + 报告变成统一的「块」序列，各导出格式共用
# --------------------------------------------------------------------------- #
def question_blocks(q: Dict, report: Optional[Dict] = None) -> List[Tuple[str, str]]:
    """返回 [(kind, text)]，kind ∈ heading/sub/para/bullet/kv。"""
    blocks: List[Tuple[str, str]] = []
    title = f"{q.get('uid', '')}　{prompts.status_cn(str(q.get('status', '')))}"
    blocks.append(("heading", title))
    meta = (f"学科：{q.get('subject', '')}（{q.get('subject_detail', '')}）　"
            f"学段：{q.get('grade', '')}　题型：{q.get('qtype') or q.get('type', '')}　"
            f"难度：{q.get('difficulty', '')}")
    blocks.append(("kv", meta))
    kps = [str(k) for k in (q.get("knowledge_points") or [])]
    if kps:
        blocks.append(("kv", "知识点：" + "、".join(kps)))
    if q.get("tags"):
        blocks.append(("kv", "标签：" + "、".join(str(t) for t in q["tags"])))

    blocks.append(("sub", "题干"))
    blocks.append(("para", str(q.get("stem") or "（未识别）")))
    if q.get("options"):
        blocks.append(("sub", "选项"))
        for o in q["options"]:
            blocks.append(("bullet", str(o)))
    if q.get("student_answer") or q.get("student_steps"):
        blocks.append(("sub", "我的作答"))
        if q.get("student_answer"):
            blocks.append(("bullet", "答案：" + str(q["student_answer"])))
        for line in _as_lines(q.get("student_steps")):
            blocks.append(("para", line))
    if q.get("correct_answer"):
        blocks.append(("sub", "参考答案"))
        blocks.append(("para", str(q["correct_answer"])))

    if report:
        blocks.append(("sub", "错题还原"))
        blocks.append(("para", str(report.get("restate") or "（无）")))
        blocks.append(("sub", "错误原因分析"))
        for c in report.get("causes", []) or []:
            if isinstance(c, dict):
                blocks.append(("bullet", f"[{c.get('type', '')}] {c.get('detail', '')}"))
            else:
                blocks.append(("bullet", str(c)))
        blocks.append(("sub", "对应知识点"))
        for kp in report.get("knowledge_points", []) or []:
            if isinstance(kp, dict):
                blocks.append(("bullet", f"{kp.get('name', '')}（{kp.get('textbook', '')}）"))
                if kp.get("note"):
                    blocks.append(("para", "　　" + str(kp["note"])))
            else:
                blocks.append(("bullet", str(kp)))
        blocks.append(("sub", "标准解题步骤"))
        for i, s in enumerate(report.get("solution_steps", []) or [], start=1):
            blocks.append(("bullet", f"{i}. {s}"))
        if report.get("final_answer"):
            blocks.append(("kv", "最终答案：" + str(report["final_answer"])))
        blocks.append(("sub", "避错技巧"))
        for t in report.get("avoid_tips", []) or []:
            blocks.append(("bullet", str(t)))
        blocks.append(("sub", "举一反三（变式练习）"))
        for i, v in enumerate(report.get("variants", []) or [], start=1):
            if isinstance(v, dict):
                blocks.append(("para", f"变式{i}：{v.get('stem', '')}"))
                if v.get("answer"):
                    blocks.append(("kv", f"　　答案：{v['answer']}"))
                if v.get("hint"):
                    blocks.append(("kv", f"　　提示：{v['hint']}"))
            else:
                blocks.append(("para", f"变式{i}：{v}"))
        depth = {"rule": "本地规则生成", "local": "本地模型生成", "cloud": "云端模型生成"}.get(
            str(report.get("depth", "")), "生成")
        blocks.append(("kv", f"（报告来源：{depth}｜引擎：{report.get('engine', '')}）"))
    return blocks


# --------------------------------------------------------------------------- #
# 公式处理（导出用）
# --------------------------------------------------------------------------- #
def _plain(text: str) -> str:
    """TXT/Markdown/内置 PDF 用：把 LaTeX 转成可读的纯文本写法。

    这些格式没法嵌公式图：``\\frac{a}{b}`` → ``a/b``、``x^{2}`` → ``x²``、``\\alpha`` → ``α``。
    """
    try:
        from . import mathfmt

        return mathfmt.format_for_export(text or "")
    except Exception:
        return text or ""


def _math_image(latex: str, size_px: int = 20, color=(17, 24, 39), bg=(255, 255, 255)):
    """把一段 LaTeX 渲染成位图（供 HTML/Word 内嵌）。失败返回 None。"""
    try:
        from . import mathdraw

        if not mathdraw.available():
            return None
        return mathdraw.render_image(latex, size_px=size_px, color=color, bg=bg)
    except Exception:
        return None


def _html_with_math(text: str) -> str:
    """文本 → HTML，其中的数学公式渲染成内嵌 PNG（完全离线，不依赖 MathJax）。"""
    try:
        from . import mathfmt
    except Exception:
        return _esc(text)
    parts: List[str] = []
    for kind, value in mathfmt.to_segments(text or ""):
        if kind == "text":
            parts.append(_esc(value))
            continue
        got = _math_image(value, size_px=19)
        if not got:
            parts.append(_esc(mathfmt.math_to_plain(value)))
            continue
        img, _baseline = got
        try:
            b64 = base64.b64encode(img.to_png_bytes()).decode("ascii")
            parts.append(
                f"<img class='math' alt='{_esc(mathfmt.math_to_plain(value))}' "
                f"src='data:image/png;base64,{b64}' "
                f"style='height:{img.h}px;vertical-align:middle;border:0;margin:0'>"
            )
        except Exception:
            parts.append(_esc(mathfmt.math_to_plain(value)))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# TXT / Markdown / HTML
# --------------------------------------------------------------------------- #
def to_text(items: Sequence[Dict], title: str = "习题导出") -> str:
    lines = [title, "=" * 40, f"导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"共 {len(items)} 道题", ""]
    for i, item in enumerate(items, start=1):
        q = item.get("question") or item
        rep = item.get("report")
        lines.append("-" * 40)
        for kind, text in question_blocks(q, rep):
            text = _plain(text)
            if kind == "heading":
                lines += ["", f"【{text}】"]
            elif kind == "sub":
                lines += ["", f"◆ {text}"]
            elif kind == "bullet":
                lines.append(f"  · {text}")
            elif kind == "kv":
                lines.append(f"  {text}")
            else:
                lines.append(text)
        lines.append("")
    return "\n".join(lines)


def to_markdown(items: Sequence[Dict], title: str = "习题导出") -> str:
    lines = [f"# {title}", "", f"- 导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"- 共 {len(items)} 道题", ""]
    for item in items:
        q = item.get("question") or item
        rep = item.get("report")
        for kind, text in question_blocks(q, rep):
            text = _plain(text)
            if kind == "heading":
                lines += ["", f"## {text}", ""]
            elif kind == "sub":
                lines += ["", f"### {text}", ""]
            elif kind == "bullet":
                lines.append(f"- {text}")
            elif kind == "kv":
                lines.append(f"{text}")
            else:
                lines += [text, ""]
        lines += ["", "---", ""]
    return "\n".join(lines)


_HTML_CSS = """
body{font-family:"Microsoft YaHei","微软雅黑",sans-serif;line-height:1.75;max-width:900px;
margin:24px auto;padding:0 18px;color:#1c1c1e;background:#fff}
h1{font-size:24px;border-bottom:3px solid #2f6fed;padding-bottom:8px}
h2{font-size:19px;margin-top:28px;color:#20509e}
h3{font-size:16px;margin:18px 0 6px;color:#333}
.meta{color:#666;font-size:13px}
.q{border:1px solid #e3e6ee;border-radius:10px;padding:14px 18px;margin:18px 0;
box-shadow:0 1px 3px rgba(0,0,0,.05)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#fff}
.wrong{background:#e5484d}.correct{background:#2f9e44}.blank{background:#868e96}
ul{margin:6px 0 6px 20px}li{margin:3px 0}
img{max-width:100%;border:1px solid #e3e6ee;border-radius:8px;margin:8px 0}
img.math{border:0;border-radius:0;margin:0 1px;vertical-align:middle}
.kv{color:#555;font-size:13.5px}
"""


def to_html(items: Sequence[Dict], title: str = "习题导出", embed_images: bool = True) -> str:
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>{_esc(title)}</title><style>{_HTML_CSS}</style></head><body>",
        f"<h1>{_esc(title)}</h1>",
        f"<p class='meta'>导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}　共 {len(items)} 道题</p>",
    ]
    for item in items:
        q = item.get("question") or item
        rep = item.get("report")
        status = str(q.get("status", ""))
        cls = {"wrong": "wrong", "correct": "correct", "blank": "blank"}.get(status, "blank")
        parts.append("<div class='q'>")
        parts.append(
            f"<h2>{_esc(q.get('uid', ''))} "
            f"<span class='badge {cls}'>{_esc(_status_cn(status))}</span></h2>"
        )
        if item.get("image_data_uri"):
            parts.append(f"<img src='{item['image_data_uri']}' alt='题目原图'>")
        for kind, text in question_blocks(q, rep):
            if kind == "heading":
                continue
            if kind == "sub":
                parts.append(f"<h3>{_html_with_math(text)}</h3>")
            elif kind == "bullet":
                parts.append(f"<div>· {_html_with_math(text)}</div>")
            elif kind == "kv":
                parts.append(f"<div class='kv'>{_html_with_math(text)}</div>")
            else:
                parts.append(f"<p>{_html_with_math(text)}</p>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Word (.docx)
# --------------------------------------------------------------------------- #
_CT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="微软雅黑" w:hAnsi="Microsoft YaHei"/>
<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="1F4E9C"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:pPr><w:spacing w:before="180" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/><w:color w:val="20509E"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:pPr><w:spacing w:before="60" w:after="60"/><w:ind w:left="240"/></w:pPr>
<w:rPr><w:color w:val="555555"/></w:rPr></w:style>
</w:styles>"""


def _docx_paragraph(text: str, style: str = "", bold: bool = False) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    b = "<w:b/>" if bold else ""
    # 保留换行：按 <w:br/> 处理
    runs = []
    for i, line in enumerate(str(text).split("\n")):
        if i:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(f'<w:r><w:rPr>{b}</w:rPr><w:t xml:space="preserve">{_esc(line)}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _docx_image(rel_id: str, width_px: int, height_px: int, name: str) -> str:
    cx = int(width_px * 9525)
    cy = int(height_px * 9525)
    return f"""<w:p><w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{abs(hash(name)) % 100000}" name="{_esc(name)}"/>
<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:nvPicPr><pic:cNvPr id="0" name="{_esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rel_id}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
<a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"""


def _docx_inline_image(rel_id: str, width_px: int, height_px: int, name: str) -> str:
    """行内图片（嵌在段落里，用于公式）。"""
    cx = int(width_px * 9525)
    cy = int(height_px * 9525)
    return f"""<w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0">
<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{abs(hash(name)) % 100000}" name="{_esc(name)}"/>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="{_esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"""


def _docx_rich_paragraph(text: str, style: str = "", bold: bool = False,
                         media: Optional[List] = None, rels: Optional[List] = None,
                         size_px: int = 20) -> str:
    """带公式的段落：普通文字用 ``<w:t>``，公式渲染成行内图片。

    需要传入 ``media``/``rels`` 列表以登记图片部件（与题目原图共用一套机制）。
    """
    try:
        from . import mathfmt
    except Exception:
        return _docx_paragraph(text, style, bold)
    segments = mathfmt.to_segments(text)
    if not any(k == "math" for k, _ in segments):
        return _docx_paragraph(text, style, bold)
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    b = "<w:b/>" if bold else ""
    runs: List[str] = []
    for kind, value in segments:
        if kind == "text":
            for i, line in enumerate(str(value).split("\n")):
                if i:
                    runs.append("<w:r><w:br/></w:r>")
                runs.append(f'<w:r><w:rPr>{b}</w:rPr><w:t xml:space="preserve">{_esc(line)}</w:t></w:r>')
            continue
        got = _math_image(value, size_px=size_px)
        if not got or media is None or rels is None:
            runs.append(f'<w:r><w:rPr>{b}</w:rPr><w:t xml:space="preserve">'
                        f'{_esc(mathfmt.math_to_plain(value))}</w:t></w:r>')
            continue
        img, _baseline = got
        try:
            name = f"math{len(media) + 1}.png"
            rel_id = f"rIdMath{len(media) + 1}"
            media.append((name, img.to_png_bytes()))
            rels.append(
                '<Relationship Id="' + rel_id + '" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/image" Target="media/' + name + '"/>'
            )
            runs.append(_docx_inline_image(rel_id, img.w, img.h, name))
        except Exception:
            runs.append(f'<w:r><w:t xml:space="preserve">{_esc(mathfmt.math_to_plain(value))}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def export_docx(items: Sequence[Dict], path: str, title: str = "习题导出",
                include_images: bool = True, max_image_px: int = 520) -> str:
    """写一个真正能被 Word / WPS 打开的 .docx（标准库 zipfile 手工组装 OOXML）。

    数学公式会渲染成行内图片，所以在 Word 里也是教科书式排版（分数、根号、上下标）。
    """
    body: List[str] = [_docx_paragraph(title, "Heading1")]
    body.append(_docx_paragraph(f"导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}　"
                                f"共 {len(items)} 道题", "Quote"))
    media: List[Tuple[str, bytes]] = []
    rels: List[str] = []

    for item in items:
        q = item.get("question") or item
        rep = item.get("report")
        img = item.get("image_bytes")
        if include_images and img:
            name = f"image{len(media) + 1}.jpeg"
            rel_id = f"rIdImg{len(media) + 1}"
            media.append((name, img))
            rels.append(
                f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/'
                f'2006/relationships/image" Target="media/{name}"/>'
            )
            iw, ih = item.get("image_size") or (800, 600)
            k = min(1.0, max_image_px / max(1, iw))
            body.append(_docx_image(rel_id, int(iw * k), int(ih * k), name))

        for kind, text in question_blocks(q, rep):
            if kind == "heading":
                body.append(_docx_paragraph(text, "Heading2"))
            elif kind == "sub":
                body.append(_docx_rich_paragraph("◆ " + text, bold=True, media=media, rels=rels))
            elif kind == "bullet":
                body.append(_docx_rich_paragraph("· " + text, "Quote", media=media, rels=rels))
            elif kind == "kv":
                body.append(_docx_rich_paragraph(text, "Quote", media=media, rels=rels))
            else:
                body.append(_docx_rich_paragraph(text, media=media, rels=rels))
        body.append(_docx_paragraph(""))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<w:body>" + "".join(body) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>' + "".join(rels) + "</Relationships>"
    )

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES_XML)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, data in media:
            zf.writestr(f"word/media/{name}", data)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _wrap_text(text: str, max_units: float) -> List[str]:
    """按「中文字符=1 单位、ASCII=0.5 单位」粗略折行（用于图片版 PDF 的分页）。"""
    lines: List[str] = []
    for raw in str(text).split("\n"):
        cur = ""
        units = 0.0
        for ch in raw:
            w = 0.55 if ord(ch) < 128 else 1.0
            if units + w > max_units and cur:
                lines.append(cur)
                cur, units = "", 0.0
            cur += ch
            units += w
        lines.append(cur)
    return lines


def _find_browser() -> Optional[str]:
    """找一个可用于 --headless --print-to-pdf 的浏览器（Windows/macOS 常见路径）。"""
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _pdf_with_browser(html_text: str, path: str) -> Optional[str]:
    """用系统浏览器无头打印成 PDF（文字可选、体积小）。失败返回 None。"""
    browser = _find_browser()
    if not browser:
        return None
    # 临时 HTML 写在程序自己的 data/tmp 里：系统临时目录在某些受限环境下不可写
    tmpdir = str(paths.data_dir() / "tmp")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        html_path = os.path.join(tmpdir, f"pdf_{int(time.time() * 1000)}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_text)
        out = os.path.abspath(path)
        if os.path.exists(out):
            os.remove(out)
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-pdf-header-footer", f"--print-to-pdf={out}",
            "file:///" + html_path.replace("\\", "/"),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=180,
                              creationflags=0x08000000 if sys.platform == "win32" else 0)
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            log.info("已用浏览器生成 PDF：%s", browser)
            return out
        log.warning("浏览器生成 PDF 失败：%s", (proc.stderr or b"")[:300])
        return None
    except Exception as exc:
        log.warning("调用浏览器失败：%s", exc)
        return None
    finally:
        try:
            if "html_path" in dir() and os.path.exists(html_path):
                os.remove(html_path)
        except OSError:
            pass


class MiniPDF:
    """极简 PDF 生成器：只支持「整页一张图」这种页面，够用且绝对不跑版。

    - **文字页**：用 Windows GDI 把整页文字渲染成位图（中文绝不会变方块/乱码），
      再以 ``FlateDecode``（zlib 无损）嵌进 PDF —— 白底黑字压缩率很高，体积可控。
    - **图片页**：题目原图用 JPEG（``DCTDecode``）嵌入。
    - 全程在内存中编码（:func:`exerarchive.gdiplus.save_image_bytes`），不写临时文件。

    代价是文字不可选中；想要可选文字的 PDF，程序会优先调用系统自带浏览器的
    「打印成 PDF」（见 :func:`_pdf_with_browser`）。
    """

    PAGE_W = 595      # A4 宽（pt）
    PAGE_H = 842      # A4 高（pt）
    RENDER_SCALE = 2  # 2 倍分辨率渲染（约 144 DPI），打印清晰

    def __init__(self):
        # 每个元素: {"w": 像素宽, "h": 像素高, "data": 字节, "filter": PDF 滤镜名}
        self.pages: List[Dict] = []

    # -- 页面 -------------------------------------------------------------- #
    def add_jpeg_page(self, jpeg_bytes: bytes, px_w: int, px_h: int) -> None:
        """直接嵌入 JPEG 字节（照片用，体积最小）。"""
        self.pages.append({"w": int(px_w), "h": int(px_h), "data": jpeg_bytes,
                           "filter": "DCTDecode"})

    def add_rgb_page(self, rgb: bytes, px_w: int, px_h: int) -> None:
        """嵌入原始 RGB（内部用 zlib 无损压缩，适合白底文字页）。"""
        self.pages.append({"w": int(px_w), "h": int(px_h),
                           "data": zlib.compress(bytes(rgb), 6), "filter": "FlateDecode"})

    def add_text_page(self, lines: List[str], font_size: int = 30, margin: int = 60) -> None:
        from . import gdiplus

        if not gdiplus.available():
            raise RuntimeError("当前系统不支持内置 PDF 渲染（需要 Windows GDI），请改用 HTML/Word 导出")
        w = self.PAGE_W * self.RENDER_SCALE
        h = self.PAGE_H * self.RENDER_SCALE
        font = self._pick_font()
        _, _, rgb = gdiplus.draw_text_bitmap(
            w, h, lines=lines, font_size=font_size * self.RENDER_SCALE // 2,
            font_name=font, margin=margin * self.RENDER_SCALE // 2,
        )
        self.add_rgb_page(bytes(rgb), w, h)

    @staticmethod
    def _pick_font() -> str:
        for name in FALLBACK_FONTS:
            if sys.platform != "win32":
                return name
            path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name + ".ttf")
            if os.path.exists(path) or name in ("SimHei", "Microsoft YaHei", "SimSun"):
                return name
        return DEFAULT_FONT

    # -- 写出 -------------------------------------------------------------- #
    def write(self, path: str) -> str:
        if not self.pages:
            raise ValueError("没有可写入的页面")
        objects: List[bytes] = []

        def obj(data: bytes) -> int:
            objects.append(data)
            return len(objects)

        catalog_id = obj(b"")   # 1（先占位，最后回填）
        pages_id = obj(b"")     # 2
        page_ids: List[int] = []
        for page in self.pages:
            data = page["data"]
            img_id = obj(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                b"/BitsPerComponent 8 /Filter /%s /Length %d >>\nstream\n"
                % (page["w"], page["h"], page["filter"].encode("ascii"), len(data))
                + data + b"\nendstream"
            )
            content = b"q %f 0 0 %f 0 0 cm /Im0 Do Q" % (self.PAGE_W, self.PAGE_H)
            content_id = obj(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
            page_id = obj(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, self.PAGE_W, self.PAGE_H, img_id, content_id)
            )
            page_ids.append(page_id)

        kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
        objects[pages_id - 1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(page_ids)
        objects[catalog_id - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, data in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + data + b"\nendobj\n"
        xref_pos = len(out)
        out += b"xref\n0 %d\n" % (len(objects) + 1)
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += b"%010d 00000 n \n" % off
        out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, catalog_id, xref_pos))

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(bytes(out))
        os.replace(tmp, path)
        return path

    @classmethod
    def from_text_blocks(cls, blocks: Iterable[Tuple[str, str]], title: str = "",
                         lines_per_page: int = 44, font_size: int = 30) -> "MiniPDF":
        """把「块」序列分页渲染成 PDF。"""
        pdf = cls()
        header: List[str] = []
        if title:
            header = [title, f"导出时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}", ""]
        page: List[str] = list(header)
        usable_units = (cls.PAGE_W - 120) / (font_size / 2.0)  # 每行可容纳的字符单位

        def flush():
            nonlocal page
            if page:
                pdf.add_text_page(page, font_size=font_size)
                page = []

        for kind, text in blocks:
            prefix = {"heading": "【", "sub": "◆ ", "bullet": "· "}.get(kind, "")
            suffix = "】" if kind == "heading" else ""
            body = prefix + str(text) + suffix
            for line in _wrap_text(body, usable_units):
                page.append(line)
                if len(page) >= lines_per_page:
                    flush()
            if kind == "heading":
                page.append("")
        flush()
        return pdf


def export_pdf(items: Sequence[Dict], path: str, title: str = "习题导出",
               include_images: bool = True, prefer_browser: bool = True) -> str:
    """导出 PDF：优先浏览器打印（文字可选），失败则用内置渲染器（离线可用）。"""
    html_text = to_html(items, title, embed_images=False)
    # 让浏览器能显示本地图片（用 file:// 绝对路径）
    for item in items:
        src = item.get("image_path")
        if include_images and src and os.path.exists(src):
            html_text = html_text.replace(
                "</div>", f"<img src='file:///{str(src).replace(chr(92), '/')}'>\n</div>", 1
            )
    if prefer_browser:
        try:
            got = _pdf_with_browser(html_text, path)
            if got:
                return got
        except Exception as exc:
            log.warning("浏览器导出 PDF 失败，转内置渲染：%s", exc)

    blocks: List[Tuple[str, str]] = []
    for item in items:
        q = item.get("question") or item
        rep = item.get("report")
        blocks.extend(question_blocks(q, rep))
        blocks.append(("para", ""))
        blocks.append(("para", "—" * 20))
    text_pdf = MiniPDF.from_text_blocks(blocks, title=title)
    pdf = MiniPDF()
    if include_images:  # 先把每道题的题目原图各放一页，再跟文字页
        for item in items:
            img = item.get("image_bytes")
            size = item.get("image_size")
            if img and size:
                pdf.add_jpeg_page(img, size[0], size[1])
    pdf.pages.extend(text_pdf.pages)
    return pdf.write(path)


# --------------------------------------------------------------------------- #
# 批量导出入口
# --------------------------------------------------------------------------- #
class Exporter:
    """对外唯一入口：按格式导出题目集合、错题本、薄弱清单、汇总。"""

    def __init__(self, cfg=None, store=None):
        self.cfg = cfg
        self.store = store

    # -- 组装导出项 -------------------------------------------------------- #
    def collect(self, uids: Optional[Sequence[str]] = None, status: str = "",
                subject: str = "", with_report: bool = True,
                include_images: bool = True, max_image_px: int = 900) -> List[Dict]:
        """把题目（含报告、图片字节）组装成导出项列表。"""
        from . import imageops

        questions: List[Dict] = []
        if self.store is not None:
            if uids:
                for uid in uids:
                    q = self.store.get_question(uid)
                    if q:
                        questions.append(q)
            else:
                questions = self.store.list_questions(limit=2000, status=status, subject=subject)
        items: List[Dict] = []
        for q in questions:
            item: Dict = {"question": q}
            rep = q.get("report")
            if with_report and not rep and self.store is not None:
                rep = self.store.get_report(q.get("uid", ""))
            item["report"] = rep if with_report else None
            if include_images:
                src = q.get("work_path") or q.get("image_path") or ""
                if src and os.path.exists(src):
                    item["image_path"] = src
                    try:
                        img = imageops.load(src)
                        if max(img.w, img.h) > max_image_px:
                            img = img.scale_to_fit(max_image_px)
                        item["image_bytes"] = img.to_jpeg_bytes(85)
                        item["image_size"] = (img.w, img.h)
                        item["image_data_uri"] = "data:image/jpeg;base64," + base64.b64encode(
                            item["image_bytes"]
                        ).decode("ascii")
                    except Exception as exc:
                        log.warning("导出图片失败（%s）：%s", src, exc)
            items.append(item)
        return items

    def export(self, path: str, uids: Optional[Sequence[str]] = None, fmt: str = "",
               status: str = "", subject: str = "", title: str = "",
               with_report: bool = True, include_images: bool = True) -> str:
        """按扩展名或 ``fmt`` 导出，返回真实写入的文件路径。"""
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "txt").lower()
        if fmt == "htm":
            fmt = "html"
        if fmt not in EXPORT_FORMATS:
            raise ValueError(f"不支持的导出格式：{fmt}")
        items = self.collect(uids, status=status, subject=subject, with_report=with_report,
                             include_images=include_images)
        title = title or self._auto_title(status)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        if fmt == "txt":
            with open(path, "w", encoding="utf-8") as f:
                f.write(to_text(items, title))
        elif fmt == "md":
            with open(path, "w", encoding="utf-8") as f:
                f.write(to_markdown(items, title))
        elif fmt == "html":
            with open(path, "w", encoding="utf-8") as f:
                f.write(to_html(items, title, embed_images=True))
        elif fmt == "docx":
            export_docx(items, path, title, include_images=include_images)
        elif fmt == "pdf":
            export_pdf(items, path, title, include_images=include_images)
        return path

    def _auto_title(self, status: str) -> str:
        if status == "wrong":
            return "个人错题本"
        return {
            "correct": "正确题汇总",
            "blank": "未做题清单",
        }.get(status, "习题汇总")

    # -- 预置文档 ---------------------------------------------------------- #
    def export_wrong_book(self, path: str, fmt: str = "") -> str:
        """一键生成个人错题本（题目 + 完整错题分析）。"""
        return self.export(path, fmt=fmt, status="wrong", title="个人错题本",
                           with_report=True, include_images=True)

    def export_weak_list(self, path: str, fmt: str = "") -> str:
        """知识点薄弱清单：按错题次数排序的知识点 + 对应题目清单。"""
        stats = self.store.stats() if self.store else {}
        weak = stats.get("weak_knowledge") or {}
        lines = [
            "# 知识点薄弱清单", "",
            f"生成时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}", "",
            f"共 {len(weak)} 个薄弱知识点（按错题次数排序）", "",
            "| 知识点 | 错题次数 | 建议 |", "| --- | --- | --- |",
        ]
        for kp, cnt in weak.items():
            advice = "优先复习" if cnt >= 3 else ("需要巩固" if cnt == 2 else "保持关注")
            lines.append(f"| {kp} | {cnt} | {advice} |")
        lines += ["", "## 错题明细", ""]
        for kp, cnt in weak.items():
            lines += [f"### {kp}（{cnt} 次）", ""]
            found = self.store.search(keyword=kp, status="wrong", limit=20) if self.store else []
            if not found:
                continue
            for q in found:
                stem = (q.get("stem") or "").replace("\n", " ")[:70]
                lines.append(f"- {q.get('uid')}　{stem}…")
            lines.append("")
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "md").lower()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        text = "\n".join(lines)
        if fmt == "txt":
            with open(path, "w", encoding="utf-8") as f:
                f.write(re.sub(r"^[|#].*$", "", text, flags=re.M))
        elif fmt == "html":
            body = "".join(f"<p>{_esc(l)}</p>" for l in lines)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                        f"<title>知识点薄弱清单</title><style>{_HTML_CSS}</style></head>"
                        f"<body><h1>知识点薄弱清单</h1>{body}</body></html>")
        elif fmt == "docx":
            items = [{"question": {"uid": "薄弱知识点统计", "stem": "\n".join(lines),
                                   "status": "", "subject": "", "qtype": "", "grade": ""}}]
            export_docx(items, path, "知识点薄弱清单", include_images=False)
        else:  # md
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return path

    def export_summary(self, path: str, fmt: str = "") -> str:
        """错题汇总文档：统计 + 各学科错题列表（不含完整分析，便于快速浏览）。"""
        stats = self.store.stats() if self.store else {}
        lines = [
            "# 错题汇总", "",
            f"生成时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}", "",
            "## 总览", "",
            f"- 图片：{stats.get('images', 0)} 张",
            f"- 题目：{stats.get('questions', 0)} 道",
            f"- 错题：{stats.get('wrong', 0)} 道　正确：{stats.get('correct', 0)} 道　"
            f"未做：{stats.get('blank', 0)} 道",
            f"- 已有错题分析：{stats.get('reports', 0)} 份", "",
            "## 学科分布", "",
        ]
        for k, v in (stats.get("by_subject") or {}).items():
            lines.append(f"- {k}：{v} 道")
        lines += ["", "## 错误原因分布", ""]
        for k, v in (stats.get("cause_stats") or {}).items():
            lines.append(f"- {k}：{v} 次")
        lines += ["", "## 错题明细", ""]
        wrong = self.store.wrong_questions(limit=500) if self.store else []
        for q in wrong:
            stem = (q.get("stem") or "").replace("\n", " ")[:80]
            lines.append(f"- **{q.get('uid')}**（{q.get('subject')}/{q.get('qtype')}）{stem}…")
            if q.get("student_answer"):
                lines.append(f"  - 我的答案：{q['student_answer']}")
            if q.get("correct_answer"):
                lines.append(f"  - 正确答案：{q['correct_answer']}")
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "md").lower()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        text = "\n".join(lines)
        if fmt == "html":
            items_html = "".join(f"<p>{_esc(l)}</p>" for l in lines)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                        f"<title>错题汇总</title><style>{_HTML_CSS}</style></head>"
                        f"<body><h1>错题汇总</h1>{items_html}</body></html>")
        elif fmt == "docx":
            export_docx([{"question": {"uid": "错题汇总", "stem": text, "status": "wrong",
                                       "subject": "", "qtype": "", "grade": ""}}],
                        path, "错题汇总", include_images=False)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return path
