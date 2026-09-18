# -*- coding: utf-8 -*-
"""导出功能验证：用一份合成数据把所有导出格式各生成一遍，并逐个校验文件是否有效。

用途：
- 交付验收留证（产物落在 ``verify/exports/``，可以直接双击打开看效果）；
- 改代码后回归：**不调用任何模型**，纯 CPU 几秒跑完，确认 5 种格式 + 3 种预置文档都没坏。

用法::

    python tools/verify_exports.py                 # 全部生成 + 校验（浏览器可用时同时验证浏览器 PDF）
    python tools/verify_exports.py --no-browser    # 只用内置 PDF 渲染器（最省资源）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exerarchive import gdiplus, imageops, logging_setup  # noqa: E402
from exerarchive.analysis import rule_based_report  # noqa: E402
from exerarchive.exporter import (EXPORT_FORMATS, Exporter, _find_browser,  # noqa: E402
                                 export_pdf, to_html, to_markdown, to_text)
from exerarchive.store import Store  # noqa: E402

OUT = os.path.join(ROOT, "verify", "exports")
TMP = os.path.join(ROOT, "verify", "_tmp")

Q1 = {
    "index": 1, "type": "解答题", "status": "wrong", "qtype": "解答题",
    "stem": "计算三阶行列式 $D=\\begin{vmatrix} 1 & 2 & 3 \\\\ 4 & 5 & 6 \\\\ 7 & 8 & 10 \\end{vmatrix}$ 的值，并说明所用方法。",
    "options": [], "student_answer": "$D=7$",
    "student_steps": "按对角线法则：$1\\times5\\times10-2\\times4\\times10=50-80=7$",
    "correct_answer": "$D=-3$",
    "knowledge_points": ["行列式", "对角线法则"],
    "confidence": 0.93, "subject": "数学", "subject_detail": "线性代数", "grade": "大学",
    "difficulty": "中等",
}
Q2 = {
    "index": 2, "type": "填空题", "status": "correct", "qtype": "填空题",
    "stem": "函数 $f(x)=x^2-4x+3$ 的最小值是 ______。",
    "options": [], "student_answer": "$-1$",
    "student_steps": "配方得 $(x-2)^2-1$，故最小值为 $-1$",
    "correct_answer": "$-1$", "knowledge_points": ["二次函数最值"],
    "confidence": 0.95, "subject": "数学", "subject_detail": "初等代数", "grade": "高中",
    "difficulty": "简单",
}
Q3 = {
    "index": 3, "type": "选择题", "status": "blank", "qtype": "选择题",
    "stem": "关于矩阵 A 与 B，下列说法正确的是（　　）",
    "options": ["A. AB=BA", "B. (AB)ᵀ=BᵀAᵀ", "C. |A+B|=|A|+|B|", "D. A²=0 则 A=0"],
    "student_answer": "", "student_steps": "", "correct_answer": "B",
    "knowledge_points": ["矩阵运算", "转置性质"], "confidence": 0.88,
    "subject": "数学", "subject_detail": "线性代数", "grade": "大学", "difficulty": "中等",
}


def make_dataset(store: Store, img_path: str) -> list:
    """造 3 道题（1 错 1 对 1 空）+ 1 份报告，返回 [(uid, question, report)]。"""
    img_uid = store.add_image(img_path, original_name="样张.png", source="导出验证")
    image_id = store.image_id(img_uid)
    out = []
    for q in (Q1, Q2, Q3):
        uid = store.add_question(q, image_id, "样张.png")
        report = None
        if q["status"] == "wrong":
            report = rule_based_report({**q, "uid": uid})
            # 手工补一份"像云端深度版"的报告，验证各段落都能导出
            report.update({
                "restate": "题目要求计算三阶行列式 $D=\\begin{vmatrix} 1 & 2 & 3 \\\\ 4 & 5 & 6 \\\\ 7 & 8 & 10 \\end{vmatrix}$ 的值。"
                           "学生用对角线法则展开，但第二、三项符号写反，得到 $D=7$，正确答案是 $D=-3$。",
                "causes": [{"type": "公式记错", "detail": "三阶行列式展开式中后两项的符号规则记反了"},
                           {"type": "计算失误", "detail": "末尾 $50-80$ 的减法也算错，本应是 $-30$"}],
                "knowledge_points": [
                    {"name": "三阶行列式展开", "textbook": "线性代数·行列式",
                     "note": "按第一行展开：$D=a_{11}A_{11}-a_{12}A_{12}+a_{13}A_{13}$，符号是 + - +"},
                    {"name": "对角线法则（萨吕斯法则）", "textbook": "线性代数·行列式",
                     "note": "三条主对角线之和减去三条副对角线之和，注意方向"},
                ],
                "solution_steps": [
                    "① 写出三阶行列式，按萨吕斯法则列出六项：三条主对角线取正、三条副对角线取负。",
                    "② 主对角线：$1\\times5\\times10+2\\times6\\times7+3\\times4\\times8=230$。",
                    "③ 副对角线：$3\\times5\\times7+1\\times6\\times8+2\\times4\\times10=233$。",
                    "④ $D=230-233=-3$。",
                ],
                "final_answer": "$D=-3$",
                "avoid_tips": [
                    "先把六项的符号写在草稿上（+ + + - - -）再代数，别边算边想符号",
                    "算完用「按另一行展开」复核一次，两次结果一致基本可以确认",
                ],
                "variants": [
                    {"stem": "计算二阶行列式 $\\begin{vmatrix} 2 & 1 \\\\ 3 & 4 \\end{vmatrix}$ 的值。",
                     "answer": "$5$", "hint": "主对角线乘积减副对角线乘积：$2\\times4-1\\times3$"},
                    {"stem": "计算三阶行列式 $\\begin{vmatrix} 1 & 0 & 2 \\\\ 3 & 1 & 0 \\\\ 2 & 4 & 1 \\end{vmatrix}$ 的值。",
                     "answer": "$-25$", "hint": "同样用萨吕斯法则，注意含 0 的项可以直接省略"},
                ],
                "depth": "cloud", "engine": "cloud:演示模型",
            })
            store.save_report(uid, report, depth="cloud", engine="cloud:演示模型")
        out.append((uid, {**q, "uid": uid}, report))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="导出功能验证")
    ap.add_argument("--no-browser", action="store_true", help="跳过浏览器 PDF（只用内置渲染器）")
    ap.add_argument("--out", default=OUT, help="产物目录")
    args = ap.parse_args()

    logging_setup.setup()
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    # 造一张题目图片（不依赖模型，纯 GDI 渲染）
    img_path = os.path.join(TMP, "样张.png")
    if gdiplus.available():
        w, h, px = gdiplus.draw_text_bitmap(
            1000, 700,
            lines=["三、解答题（12 分）",
                   "1. 计算三阶行列式 D = |1 2 3; 4 5 6; 7 8 10| 的值。",
                   "学生作答：D = 1×5×10 - 2×4×10 = 50 - 80 = 7"],
            font_size=30)
        imageops.Image(w, h, px).save(img_path)
    else:
        imageops.Image(400, 200, bytearray(b"\xff" * (400 * 200 * 3))).save(img_path)

    db = os.path.join(TMP, "export_demo.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    store = Store(db, encrypt=True)
    rows = make_dataset(store, img_path)
    uids = [r[0] for r in rows]
    wrong_uid = rows[0][0]

    exp = Exporter(None, store)
    items = exp.collect(uids, with_report=True, include_images=True)
    print(f"数据集：3 道题（1 错 1 对 1 空），共 {len(items)} 个导出项，"
          f"其中带图片 {sum(1 for i in items if i.get('image_bytes'))} 个")

    results = []

    def check(path: str, kind: str, magic=None, min_size: int = 200) -> None:
        ok = os.path.exists(path) and os.path.getsize(path) >= min_size
        extra = ""
        if ok and magic:
            with open(path, "rb") as f:
                head = f.read(8)
            ok = head.startswith(magic)
            extra = f"（文件头 {head[:4]!r}）"
        if ok and kind == "docx":
            try:
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
                    doc = zf.read("word/document.xml").decode("utf-8")
                ok = "word/document.xml" in names and "行列式" in doc
                extra = f"（含 {len(names)} 个部件，document.xml {len(doc)} 字符）"
            except Exception as exc:
                ok, extra = False, f"（zip 校验失败：{exc}）"
        if ok and kind == "pdf":
            try:
                with open(path, "rb") as f:
                    data = f.read()
                pages = data.count(b"/Type /Page ")
                has_eof = data[-64:].count(b"%%EOF") > 0
                filters = {f for f in (b"DCTDecode", b"FlateDecode") if f in data}
                ok = pages >= 1 and has_eof
                extra = (f"（{pages} 页，结束标记 {'有' if has_eof else '缺失'}，"
                         f"滤镜 {'/'.join(sorted(x.decode() for x in filters))}）")
            except Exception as exc:
                ok, extra = False, f"（PDF 解析失败：{exc}）"
        size = os.path.getsize(path) if os.path.exists(path) else 0
        results.append((os.path.basename(path), size, ok, extra))
        print(f"  {'✅' if ok else '❌'} {os.path.basename(path):<28} {size/1024:8.1f} KB {extra}")

    print("\n1) 五种格式（全部题目 + 报告 + 图片）")
    for fmt, ext in (("txt", "txt"), ("md", "md"), ("html", "html"), ("docx", "docx")):
        path = os.path.join(args.out, f"错题本.{ext}")
        exp.export(path, uids=uids, fmt=fmt, title="个人错题本", with_report=True, include_images=True)
        check(path, fmt, b"" if fmt == "txt" else None)
    # PDF：优先浏览器打印（文字可选），失败/跳过时用内置渲染
    pdf_path = os.path.join(args.out, "错题本.pdf")
    used = "内置渲染器"
    t0 = time.time()
    try:
        if not args.no_browser:
            from exerarchive.exporter import _pdf_with_browser

            html_text = to_html(items, "个人错题本", embed_images=False)
            got = _pdf_with_browser(html_text, pdf_path)
            if got:
                used = "浏览器打印（文字可选）"
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1000:
            export_pdf(items, pdf_path, "个人错题本", include_images=True, prefer_browser=False)
        check(pdf_path, "pdf", b"%PDF")
        print(f"     （PDF 用时 {time.time() - t0:.1f}s，实际使用的路径：{used}）")
    except Exception as exc:
        print(f"  ❌ PDF 生成失败：{exc}")

    print("\n2) 三种预置文档")
    check(exp.export_wrong_book(os.path.join(args.out, "个人错题本_预置.docx"), fmt="docx"), "docx")
    check(exp.export_weak_list(os.path.join(args.out, "知识点薄弱清单.md")), "md")
    check(exp.export_summary(os.path.join(args.out, "错题汇总.md")), "md")
    check(exp.export_summary(os.path.join(args.out, "错题汇总.html")), "html")

    print("\n3) 纯文本渲染（不落盘，直接看前几行）")
    text = to_text(items, "个人错题本")
    md = to_markdown(items, "个人错题本")
    html = to_html(items, "个人错题本")
    print(f"   txt {len(text)} 字符 | md {len(md)} 字符 | html {len(html)} 字符")
    print("   " + text.splitlines()[5][:60] + " …")
    for seg in ("错题还原", "错误原因分析", "对应知识点", "标准解题步骤", "避错技巧", "举一反三"):
        assert seg in text, f"TXT 缺少段落：{seg}"
        assert seg in md, f"MD 缺少段落：{seg}"
    print("   ✅ 六个报告段落在各格式中都存在")

    print("\n4) 数学公式在各格式里的呈现")

    def ok(label: str, cond: bool, detail: str = "") -> None:
        """布尔断言（check() 是给文件用的，这里判条件）。"""
        results.append((label, 0, bool(cond), detail))
        print(f"  {'✅' if cond else '❌'} {label}" + (f"　{detail}" if detail else ""))

    # TXT / Markdown：公式转成可读纯文本（不能留满屏反斜杠）
    ok("TXT 里没有残留 LaTeX 命令", "\\frac" not in text and "\\begin" not in text,
       next((l for l in text.splitlines() if "行列式" in l), "")[:60])
    ok("TXT 里公式已转成可读写法（× / ² / 行列式竖线）",
       ("×" in text) or ("²" in text) or ("|1 2 3; 4 5 6; 7 8 10|" in text),
       next((l for l in text.splitlines() if "×" in l or "²" in l), "")[:60])
    ok("Markdown 同样无 LaTeX 残留", "\\begin" not in md and "\\times" not in md)
    # HTML：公式渲染成内嵌 PNG
    n_math_img = html.count("class='math'")
    ok(f"HTML 内嵌公式图（{n_math_img} 处）", n_math_img >= 8)
    ok("HTML 公式图是 base64 PNG", html.count("data:image/png;base64,") >= 8,
       f"{html.count('data:image/png;base64,')} 张")
    # Word：公式作为行内图片进 media
    docx_path = os.path.join(args.out, "错题本.docx")
    if os.path.exists(docx_path):
        with zipfile.ZipFile(docx_path) as zf:
            names = zf.namelist()
            doc = zf.read("word/document.xml").decode("utf-8")
        math_media = [n for n in names if n.startswith("word/media/math")]
        ok(f"Word 里公式作为图片内嵌（{len(math_media)} 张）", len(math_media) >= 8)
        ok("Word 段落里引用了公式图", doc.count("rIdMath") >= 8, f"{doc.count('rIdMath')} 处")
        ok("Word 里没有残留 LaTeX 源码", "\\begin{vmatrix}" not in doc and "\\frac" not in doc)

    store.close()
    ok_all = all(r[2] for r in results)
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "browser": _find_browser() or "",
        "formats": EXPORT_FORMATS,
        "files": [{"name": n, "size": s, "ok": o, "note": e} for n, s, o, e in results],
        "all_ok": ok_all,
    }
    with open(os.path.join(args.out, "校验结果.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\n{'✅ 全部导出格式校验通过' if ok_all else '❌ 存在失败项'}，"
          f"产物目录：{args.out}")
    # 给批处理用的纯 ASCII 结果行（findstr 对 emoji 不友好）
    print(f"RESULT: {'OK' if ok_all else 'FAILED'} ({sum(1 for r in results if r[2])}/{len(results)} files)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
