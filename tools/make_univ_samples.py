# -*- coding: utf-8 -*-
"""生成「大学数学」完备性测试样张：题干 + **手写解题过程** + 老师红笔批改。

用法::

    python tools\\make_univ_samples.py                    # 默认写进 samples\\大学数学\\
    python tools\\make_univ_samples.py --out D:\\测试卷     # 换输出目录
    python tools\\make_univ_samples.py --only u3 u5        # 只生成指定页

产物::

    u1_math_limits_fill.jpg        高等数学·极限与连续     填空 3 + 选择 1（2 对 2 错）
    u2_math_derivative_choice.jpg  高等数学·导数与微分     选择题 3（1 对 1 错 1 空白）
    u3_math_integral_fill.jpg      高等数学·积分           填空 3（1 对 1 错 1 空白）
    u4_math_series_solution.jpg    高等数学·数项级数       解答 2（1 对 1 错）
    u5_math_linalg_det.jpg         线性代数·行列式与矩阵   计算 2（1 对 1 错）
    u6_math_linalg_eigen.jpg       线性代数·特征值与方程组 解答 2（1 对 1 错）
    u7_math_probability.jpg        概率论·条件概率与贝叶斯 解答 2（1 对 1 错）
    u8_math_ode.jpg                常微分方程             解答 2（1 对 1 错）
    u9_math_integral_tilted.jpg    u3 的退化版（倾斜 7° + 模糊 + 压暗 + 水印）
    ground_truth.json              页级标准答案（与 samples\\ground_truth.json 同格式）
    questions.json                 逐题标准答案（题干/学生作答/正确答案/状态/错误类型/知识点）

为什么单独做这一套（和 samples\\ 里的 s1~s6 什么关系）
----------------------------------------------------
``s1~s6`` 是「**能不能跑通**」的冒烟样张：6 张图 14 道题，以高中内容为主，
用来证明流水线端到端没坏。这一套是「**跑得全不全**」的完备性样张，两者互补：

1. **只考大学数学**：极限与连续 / 导数与微分 / 积分 / 数项级数 / 行列式与矩阵 /
   特征值与线性方程组 / 条件概率与贝叶斯 / 常微分方程 —— 8 个主题。
2. **每题都有手写解题过程**（不是只写一个字母答案），用来测能不能读出
   "学生是怎么算的"、错误出在哪一步。
3. **标准答案是逐题的**：``questions.json`` 把题干、学生作答、正确答案、对错状态、
   错误类型、知识点都写全了，所以既能算"识别率"，也能算"**错因判断准不准**"。
4. **覆盖三种状态与 6 类典型错误**：正确 / 错误 / 空白；
   公式记错、概念错误、计算失误、思路偏差、步骤不完整、审题失误。
5. **学生用蓝黑墨、老师用红笔**（``s1~s6`` 里学生作答也是红的，不够真实）——
   这本身就多测了一件事：模型能不能分清"学生的字"和"老师的批改"。
6. **一张退化图像**（u9 = u3 旋转模糊压暗带水印），量出图像质量对识别的影响。

技术实现复用 ``tools/make_samples.py`` 的 GDI 绘制、手写笔迹与拍照退化函数，
只加"题干 + 手写过程 + 批改"的版面编排，视觉风格与既有样张一致。

自查：``python tools\\verify_recognition.py --dir samples\\大学数学``
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

# 两个目录都要进 sys.path：tools\ 是为了 import make_samples（复用绘制能力），
# 项目根目录是为了 import exerarchive。运行脚本时 sys.path[0] 只有 tools\，根目录不会自动加。
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_TOOLS_DIR)
for _p in (_TOOLS_DIR, _ROOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import make_samples as S  # noqa: E402  复用绘制/手写/退化能力，不重复造轮子
from make_samples import (  # noqa: E402
    F_HAND_CJK, F_HAND_LAT, F_MATH, F_PRINT, INK, INK_LIGHT, MARGIN, PAGE_H, PAGE_W,
    RED, RED_DEEP, RULE, Canvas, Dib, GdiPlus, answer_area, bracket_blank, determinant,
    hand_check, hand_circle, hand_cross, hand_text, hand_text_fit, hand_underline,
    make_paper, option_row, page_footer, page_header, photo_like, render_page,
    save_image, text_fit, underline_blank, verify,
)

# ================================================================ 版面参数
BODY = 30            # 印刷题干字号
OPT = 28             # 选项字号
HAND = 30            # 手写字号
COMMENT_SIZE = 26    # 老师批注字号（放不下会自动降字号，再放不下就另起一行）
LINE_STEM = 52       # 题干基线 → 下一行基线
LINE_OPT = 46        # 题干 → 选项
LINE_WORK = 58       # 手写过程行距
Q_GAP = 34           # 题目之间的留白
INDENT = 30          # 手写过程相对版心的缩进

# 学生用蓝黑墨、老师用红笔 —— 真实卷面就是这样，也让模型必须分清"谁写的"
BLUE = (26, 60, 148)
# 手写数学式：西文手写体优先，遇到 π / ∫ / λ 这类符号自动回退到楷体/黑体（逐字体查字形，不会出豆腐块）
F_HAND_MATH = ["Segoe Print", "Ink Free", "KaiTi", "SimHei"]


def hand_math(c: Canvas, x, base, s, size=HAND, color=BLUE, rnd=None, jitter=1.4):
    """学生手写的数学式（蓝黑墨）。"""
    rnd = rnd or random.Random(7)
    return c.text(x, base, s, size=size, color=color, families=F_HAND_MATH,
                  hand=True, rnd=rnd, jitter=jitter)


def hand_say(c: Canvas, x, base, s, size=HAND, color=BLUE, rnd=None, jitter=1.6):
    """学生手写的中文（如"可去间断点"）。"""
    rnd = rnd or random.Random(8)
    return c.text(x, base, s, size=size, color=color, families=F_HAND_CJK,
                  hand=True, rnd=rnd, jitter=jitter)


def bracket_matrix(c: Canvas, x, y, rows, cell_w=62, cell_h=48, size=30, color=INK,
                   label="A = "):
    """画带方括号的矩阵（行列式用的是竖线，矩阵必须用方括号才能区分）。返回整体高度。"""
    if label:
        x = c.text(x, y + cell_h - 14, label, size=size, color=color, families=F_MATH)
    h = cell_h * len(rows)
    cols = max(len(r) for r in rows)
    body_w = cell_w * cols
    # 左右方括号：竖线 + 上下各一小段横折
    for side in (0, 1):
        bx = x + (0 if side == 0 else body_w + 18)
        c.fill_rect(bx, y, bx + 4, y + h, color)
        c.fill_rect(bx + (0 if side == 0 else -8), y, bx + 12 if side == 0 else bx + 4,
                    y + 4, color)
        c.fill_rect(bx + (0 if side == 0 else -8), y + h - 4,
                    bx + 12 if side == 0 else bx + 4, y + h, color)
    for r, row in enumerate(rows):
        for cidx, val in enumerate(row):
            cx = x + 22 + cidx * cell_w
            w = c.measure(str(val), size, F_MATH)
            c.text(cx + (cell_w - 16 - w) / 2.0, y + r * cell_h + cell_h - 16, str(val),
                   size=size, color=color, families=F_MATH)
    return h


# ================================================================ 题目数据
# 每题字段说明（questions.json 与绘制共用同一份数据，保证"画的"和"写的"永远一致）：
#   no/score      题号与分值
#   type          题型（程序枚举：选择题/填空题/解答题/计算题/证明题）
#   stem          印刷题干（支持 x^2 上标、a_1 下标、∫_0^1 与 ∑_{n=1}^{∞} 的上下限）
#   blank         "bracket"（选择题括号）/ "underline"（填空横线）/ None
#   options       选择题选项；pick 是学生手写选的字母（None = 没作答）
#   answer_line   填空题学生手写的答案（None = 空白）
#   work          学生手写过程，逐行；字符串或 {"t": 文本, "u": [要划线的片段]}
#   status        correct / wrong / blank
#   mark          check（红勾）/ cross（红叉）/ none
#   comment       老师红笔批注
#   correct_answer / error_type / knowledge   标准答案、错误类型、知识点
PAGES = [
    {
        "file": "u1_math_limits_fill",
        "seed": 20260401,
        "title": "高等数学 · 极限与连续 随堂练习",
        "subtitle": "一、填空与选择（每小题 5 分，共 20 分）",
        "subject_detail": "高等数学·极限与连续",
        "questions": [
            {
                "no": 1, "score": 5, "type": "填空题",
                "stem": "求极限 lim_{x→0} (sin 3x)/x =",
                "blank": "underline", "answer_line": "3",
                "status": "correct", "mark": "check",
                "correct_answer": "3", "error_type": "",
                "knowledge": ["极限", "等价无穷小"],
            },
            {
                "no": 2, "score": 5, "type": "填空题",
                "stem": "求极限 lim_{x→∞} (1 + 2/x)^x =",
                "blank": "underline", "answer_line": "e",
                "status": "wrong", "mark": "cross",
                "comment": "指数上是 2，结果应为 e^2",
                "correct_answer": "e^2", "error_type": "公式记错",
                "knowledge": ["重要极限", "极限"],
            },
            {
                "no": 3, "score": 5, "type": "填空题",
                "stem": "设 f(x) = (x^2 - 1)/(x - 1)，则 x = 1 是 f(x) 的",
                "blank": "underline", "answer_line": "可去间断点", "answer_cjk": True,
                "status": "correct", "mark": "check",
                "correct_answer": "可去间断点", "error_type": "",
                "knowledge": ["函数连续性", "间断点分类"],
            },
            {
                "no": 4, "score": 5, "type": "选择题",
                "stem": "当 x → 0 时，与 x 等价的无穷小是",
                "blank": "bracket", "pick": "B",
                "options": ["A. 1 - cos x", "B. tan 2x", "C. sin x", "D. ln(1 + x^2)"],
                "status": "wrong", "mark": "cross",
                "comment": "tan 2x ~ 2x，应选 C",
                "correct_answer": "C", "error_type": "概念错误",
                "knowledge": ["等价无穷小", "极限"],
            },
            {
                "no": 5, "score": 5, "type": "填空题",
                "stem": "设 f(x) = x·sin(1/x)，则 lim_{x→0} f(x) =",
                "blank": "underline", "answer_line": "不存在",
                "status": "wrong", "mark": "cross",
                "comment": "用夹逼准则：|x·sin(1/x)| ≤ |x| → 0，极限是 0",
                "correct_answer": "0", "error_type": "概念错误",
                "knowledge": ["极限", "夹逼准则", "无穷小与有界量之积"],
            },
            {
                "no": 6, "score": 5, "type": "选择题",
                "stem": "设 f(x) = ln(1 + x)，则 f'(0) =",
                "blank": "bracket", "pick": "B",
                "options": ["A. 0", "B. 1", "C. 1/2", "D. e"],
                "status": "correct", "mark": "check",
                "correct_answer": "B", "error_type": "",
                "knowledge": ["导数公式", "导数值"],
            },
        ],
    },
    {
        "file": "u2_math_derivative_choice",
        "seed": 20260402,
        "title": "高等数学 · 导数与微分 单元测验",
        "subtitle": "一、选择题（每小题 5 分，共 15 分）",
        "subject_detail": "高等数学·导数与微分",
        "questions": [
            {
                "no": 1, "score": 5, "type": "选择题",
                "stem": "设 y = x^3 + ln x，则 y' =",
                "blank": "bracket", "pick": "A",
                "options": ["A. 3x^2 + 1/x", "B. 3x^2 + ln x", "C. x^2 + 1/x", "D. 3x^2 - 1/x"],
                "status": "correct", "mark": "check",
                "correct_answer": "A", "error_type": "",
                "knowledge": ["导数公式", "求导法则"],
            },
            {
                "no": 2, "score": 5, "type": "选择题",
                "stem": "设 y = sin(2x)，则 dy =",
                "blank": "bracket", "pick": "A",
                "options": ["A. cos 2x dx", "B. 2cos 2x dx", "C. -2cos 2x dx", "D. 2sin 2x dx"],
                "status": "wrong", "mark": "cross",
                "comment": "复合函数求导别忘乘 2",
                "correct_answer": "B", "error_type": "公式记错",
                "knowledge": ["复合函数求导", "微分"],
            },
            {
                "no": 3, "score": 5, "type": "选择题",
                "stem": "设 y = e^x·arctan x，则 y'(0) =",
                "blank": "bracket", "pick": None,          # 空白：完全不写
                "options": ["A. 0", "B. 1", "C. π/4", "D. 2"],
                "status": "blank", "mark": "none",
                "correct_answer": "B", "error_type": "",
                "knowledge": ["乘积求导", "导数值"],
            },
            {
                "no": 4, "score": 5, "type": "填空题",
                "stem": "设 y = x^x，则 y' =",
                "blank": "underline", "answer_line": "x^x(ln x + 1)",
                "status": "correct", "mark": "check",
                "correct_answer": "x^x(ln x + 1)", "error_type": "",
                "knowledge": ["幂指函数求导", "对数求导法"],
            },
            {
                "no": 5, "score": 5, "type": "选择题",
                "stem": "函数 f(x) = |x| 在点 x = 0 处",
                "blank": "bracket", "pick": "A",
                "options": ["A. 可导", "B. 连续但不可导", "C. 不连续", "D. 可导但不连续"],
                "status": "wrong", "mark": "cross",
                "comment": "左右导数分别是 -1 和 1，不相等",
                "correct_answer": "B", "error_type": "概念错误",
                "knowledge": ["导数定义", "分段函数求导"],
            },
        ],
    },
    {
        "file": "u3_math_integral_fill",
        "seed": 20260403,
        "title": "高等数学 · 积分 课后作业",
        "subtitle": "一、填空题（每小题 6 分，共 18 分）",
        "subject_detail": "高等数学·不定积分与定积分",
        "questions": [
            {
                "no": 1, "score": 6, "type": "填空题",
                "stem": "∫ (2x + 1) dx =",
                "blank": "underline", "answer_line": "x^2 + x + C",
                "status": "correct", "mark": "check",
                "correct_answer": "x^2 + x + C", "error_type": "",
                "knowledge": ["不定积分", "基本积分公式"],
            },
            {
                "no": 2, "score": 6, "type": "填空题",
                "stem": "∫ x·e^(x^2) dx =",
                "blank": "underline", "answer_line": "e^(x^2) + C",
                "status": "wrong", "mark": "cross",
                "comment": "凑微分后要乘 1/2",
                "correct_answer": "(1/2)e^(x^2) + C", "error_type": "计算失误",
                "knowledge": ["不定积分", "第一换元法"],
            },
            {
                "no": 3, "score": 6, "type": "填空题",
                "stem": "∫_1^e (ln x)/x dx =",
                "blank": "underline", "answer_line": None,   # 空白
                "status": "blank", "mark": "none",
                "correct_answer": "1/2", "error_type": "",
                "knowledge": ["定积分", "换元积分法"],
            },
            {
                "no": 4, "score": 6, "type": "填空题",
                "stem": "∫_0^1 (2x + 1) dx =",
                "blank": "underline", "answer_line": "2",
                "status": "correct", "mark": "check",
                "correct_answer": "2", "error_type": "",
                "knowledge": ["定积分", "牛顿-莱布尼茨公式"],
            },
            {
                "no": 5, "score": 6, "type": "填空题",
                "stem": "∫ 1/(1 + x^2) dx =",
                "blank": "underline", "answer_line": "ln(1 + x^2) + C",
                "status": "wrong", "mark": "cross",
                "comment": "这是 arctan x + C；被积函数里没有 2x",
                "correct_answer": "arctan x + C", "error_type": "公式记错",
                "knowledge": ["不定积分", "基本积分公式"],
            },
        ],
    },
    {
        "file": "u4_math_series_solution",
        "seed": 20260404,
        "title": "高等数学 · 数项级数 解答题",
        "subtitle": "二、解答题（每小题 10 分，共 20 分）",
        "subject_detail": "高等数学·数项级数",
        "questions": [
            {
                "no": 1, "score": 10, "type": "解答题",
                "stem": "判别级数 ∑_{n=1}^{∞} n/2^n 的收敛性。",
                "work": [
                    "解：用比值判别法。",
                    "lim |a_{n+1}/a_n| = lim ((n+1)/2^(n+1))·(2^n/n)",
                    "= lim (n+1)/(2n) = 1/2 < 1，",
                    "故级数收敛。",
                ],
                "status": "correct", "mark": "check",
                "correct_answer": "收敛（比值判别法，极限为 1/2）", "error_type": "",
                "knowledge": ["正项级数", "比值判别法"],
            },
            {
                "no": 2, "score": 10, "type": "解答题",
                "stem": "判别级数 ∑_{n=1}^{∞} 1/(2n-1) 的收敛性。",
                "work": [
                    "解：因为 1/(2n-1) < 1/n，",
                    {"t": "而 ∑ 1/n 收敛，", "u": ["∑ 1/n 收敛"]},
                    "故由比较判别法，原级数收敛。",
                ],
                "status": "wrong", "mark": "cross",
                "comment": "1/n 的级数发散；比发散级数小也判不出收敛",
                "correct_answer": "发散（1/(2n-1) ~ 1/(2n)，与调和级数同阶）",
                "error_type": "概念错误",
                "knowledge": ["正项级数", "比较判别法", "调和级数"],
            },
        ],
    },
    {
        "file": "u5_math_linalg_det",
        "seed": 20260405,
        "title": "线性代数 · 行列式与矩阵 计算题",
        "subtitle": "二、计算题（每小题 10 分，共 20 分）",
        "subject_detail": "线性代数·行列式与矩阵",
        "questions": [
            {
                "no": 1, "score": 10, "type": "计算题",
                "stem": "计算下列三阶行列式 D 的值：",
                "determinant": [["2", "0", "1"], ["1", "3", "2"], ["1", "0", "1"]],
                "work": [
                    "解：按第一行展开，",
                    "D = 2×(3×1 - 2×0) - 0 + 1×(1×0 - 3×1)",
                    "= 2×3 + 1×(-3) = 6 - 3 = 3。",
                ],
                "status": "correct", "mark": "check",
                "correct_answer": "3", "error_type": "",
                "knowledge": ["行列式", "按行展开", "代数余子式"],
            },
            {
                "no": 2, "score": 10, "type": "计算题",
                "stem": "求 AB。",
                "matrices": [
                    {"rows": [["1", "2"], ["3", "4"]], "label": "A = "},
                    {"rows": [["0", "1"], ["1", "0"]], "label": "　B = "},
                ],
                "work": [
                    {"t": "解：AB = [0, 2; 3, 0]。", "u": ["[0, 2; 3, 0]"]},
                ],
                "status": "wrong", "mark": "cross",
                "comment": "矩阵乘法是行乘列，不是对应元素相乘：AB = [2, 1; 4, 3]",
                "correct_answer": "AB = [[2, 1], [4, 3]]", "error_type": "概念错误",
                "knowledge": ["矩阵乘法"],
            },
        ],
    },
    {
        "file": "u6_math_linalg_eigen",
        "seed": 20260406,
        "title": "线性代数 · 特征值与线性方程组 解答题",
        "subtitle": "三、解答题（每小题 10 分，共 20 分）",
        "subject_detail": "线性代数·特征值与线性方程组",
        "questions": [
            {
                "no": 1, "score": 10, "type": "解答题",
                "stem": "求矩阵 A 的特征值。",
                "matrices": [{"rows": [["2", "1"], ["1", "2"]], "label": "A = "}],
                "work": [
                    "解：|λE - A| = (λ-2)^2 - 1 = λ^2 - 4λ + 3 = 0，",
                    "解得 λ_1 = 1，λ_2 = 3。",
                ],
                "status": "correct", "mark": "check",
                "correct_answer": "λ1 = 1，λ2 = 3", "error_type": "",
                "knowledge": ["特征值", "特征方程"],
            },
            {
                "no": 2, "score": 10, "type": "解答题",
                "stem": "解线性方程组：x + y + z = 6，2x - y + z = 3，x + 2y - z = 2。",
                "work": [
                    "解：由第一个方程得 z = 6 - x - y，",
                    "代入后两个方程得 x - 2y = -3，2x + 3y = 8，",
                    "解得 y = 2，x = 1，",
                    {"t": "所以 z = 6 - 1 + 2 = 7。", "u": ["6 - 1 + 2"]},
                ],
                "status": "wrong", "mark": "cross",
                "comment": "z = 6 - 1 - 2 = 3，代回第一个方程验算一下",
                "correct_answer": "(x, y, z) = (1, 2, 3)", "error_type": "计算失误",
                "knowledge": ["线性方程组", "高斯消元法", "代入消元"],
            },
        ],
    },
    {
        "file": "u7_math_probability",
        "seed": 20260407,
        "title": "概率论与数理统计 · 条件概率 解答题",
        "subtitle": "三、解答题（每小题 10 分，共 20 分）",
        "subject_detail": "概率论·条件概率与贝叶斯公式",
        "questions": [
            {
                "no": 1, "score": 10, "type": "解答题",
                "stem": "设 P(A) = 0.5，P(B) = 0.4，P(AB) = 0.2，求 P(A|B)。",
                "work": [
                    "解：由条件概率公式，",
                    "P(A|B) = P(AB)/P(B) = 0.2/0.4 = 0.5。",
                ],
                "status": "correct", "mark": "check",
                "correct_answer": "0.5", "error_type": "",
                "knowledge": ["条件概率"],
            },
            {
                "no": 2, "score": 10, "type": "解答题",
                "stem": "甲线产量占 60%，次品率 2%；乙线产量占 40%，次品率 5%。"
                        "随机取一件发现是次品，求它来自甲线的概率。",
                "work": [
                    "解：设 A = 来自甲线，B = 取到次品。",
                    {"t": "P(A|B) = P(A)P(B|A) / P(B|A)", "u": ["/ P(B|A)"]},
                    "= (0.6×0.02)/0.02 = 0.6。",
                ],
                "status": "wrong", "mark": "cross",
                "comment": "分母要用全概率公式：0.6×0.02 + 0.4×0.05 = 0.032，答案 0.375",
                "correct_answer": "0.375", "error_type": "公式记错",
                "knowledge": ["贝叶斯公式", "全概率公式"],
            },
        ],
    },
    {
        "file": "u8_math_ode",
        "seed": 20260408,
        "title": "常微分方程 · 一阶与二阶 解答题",
        "subtitle": "三、解答题（每小题 10 分，共 20 分）",
        "subject_detail": "常微分方程·一阶与二阶线性方程",
        "questions": [
            {
                "no": 1, "score": 10, "type": "解答题",
                "stem": "求微分方程 y' = 2xy 的通解。",
                "work": [
                    "解：分离变量得 dy/y = 2x dx，",
                    "两边积分：ln|y| = x^2 + C，",
                    {"t": "所以 y = e^(x^2) + C。", "u": ["e^(x^2) + C"]},
                ],
                "status": "wrong", "mark": "cross",
                "comment": "常数在指数外面乘：y = C·e^(x^2)",
                "correct_answer": "y = C·e^(x^2)", "error_type": "概念错误",
                "knowledge": ["一阶微分方程", "分离变量法"],
            },
            {
                "no": 2, "score": 10, "type": "解答题",
                "stem": "求微分方程 y'' - 3y' + 2y = 0 的通解。",
                "work": [
                    "解：特征方程为 r^2 - 3r + 2 = 0，",
                    "解得 r_1 = 1，r_2 = 2，",
                    "所以通解 y = C_1·e^x + C_2·e^(2x)。",
                ],
                "status": "correct", "mark": "check",
                "correct_answer": "y = C1·e^x + C2·e^(2x)", "error_type": "",
                "knowledge": ["二阶常系数线性微分方程", "特征方程"],
            },
        ],
    },
]

# u9：u3 的退化版（同一份内容 + 旋转/模糊/压暗/水印），用来量图像质量的影响
DEGRADED = {
    "file": "u9_math_integral_tilted",
    "from": "u3_math_integral_fill",
    "seed": 20260403,                       # 同种子 → 纸纹与笔迹和 u3 完全一致
    "subject_detail": "高等数学·不定积分与定积分（u3 的倾斜模糊压暗带水印版本）",
}


# ================================================================ 版面编排
def draw_question(c: Canvas, q: dict, y: int, rnd) -> int:
    """画一道完整的题（题干 → 选项/附件 → 手写过程 → 老师批改），返回下一题的 y。"""
    # ---- 1) 题干（含题号与分值） ----
    head = "%d." % q["no"]
    if q.get("score"):
        head += "（%d 分）" % q["score"]
    head_base = y              # 题号所在基线（批改时要圈题号）
    stem_base = y
    x = c.text(MARGIN, y, head + q["stem"], size=BODY, color=INK, families=F_PRINT)

    # ---- 2) 作答位置（括号 / 横线 / 行列式 / 矩阵） ----
    blank_box = None          # (左 x, 宽) 供手写答案落位
    mark_x = None             # 批改符号的落位
    if q.get("blank") == "bracket":
        bx, bw, ex = bracket_blank(c, x + 8, y, width=78)
        blank_box = (bx, bw)
        mark_x = ex + 34
    elif q.get("blank") == "underline":
        lx = x + 14
        underline_blank(c, lx, y + 9, width=210)
        blank_box = (lx + 8, 190)
        mark_x = lx + 224
    y += LINE_STEM

    # 选择题选项
    if q.get("options"):
        option_row(c, y, q["options"], size=OPT)
        y += LINE_OPT

    # 附件：行列式 / 矩阵（印在题干下方）
    if q.get("determinant"):
        h = determinant(c, MARGIN + 130, y - 8, q["determinant"])
        y += h + 16
    for m in q.get("matrices") or []:
        if mark_x is None:
            mark_x = PAGE_W - MARGIN - 190
        h = bracket_matrix(c, MARGIN + 60, y - 6, m["rows"], label=m.get("label", ""))
        y += h + 14

    # ---- 3) 学生手写作答 ----
    # 选择题：字母写进括号里
    if q.get("pick") and blank_box:
        aw = c.measure(q["pick"], 32, F_HAND_LAT)
        hand_math(c, blank_box[0] + (blank_box[1] - aw) / 2.0, stem_base, q["pick"], size=32,
                  rnd=rnd)
    # 填空题：答案写在横线上
    if q.get("answer_line") and blank_box:
        fn = hand_say if q.get("answer_cjk") else hand_math
        fn(c, blank_box[0], stem_base - 2, q["answer_line"], size=HAND, rnd=rnd)

    # 解答/计算题：手写过程逐行
    work = q.get("work") or []
    last_y = stem_base
    for line in work:
        text = line["t"] if isinstance(line, dict) else line
        fn = hand_say if any("\u4e00" <= ch <= "\u9fff" for ch in text) else hand_math
        wx = MARGIN + INDENT
        fn(c, wx, y, text, size=HAND, rnd=rnd)
        # 老师划出写错的地方（按前缀实测宽度精确落位，不是拍脑袋取比例）
        for sub in (line.get("u") or []) if isinstance(line, dict) else []:
            if sub in text:
                off = c.measure(text.split(sub)[0], HAND, F_HAND_MATH)
                hand_underline(c, wx + off - 2, wx + off + c.measure(sub, HAND, F_HAND_MATH) + 2,
                               y + 10, rnd=rnd, wobble=2.0)
        last_y = y
        y += LINE_WORK
    if work:
        mark_x = PAGE_W - MARGIN - 210     # 解答题的批改符号落在右侧空白
        stem_base = last_y

    # ---- 4) 老师批改（红笔） ----
    if q.get("mark") == "check" and mark_x:
        hand_check(c, mark_x, stem_base - 6, size=32, rnd=rnd)
    elif q.get("mark") == "cross" and mark_x:
        hand_cross(c, mark_x, stem_base - 4, size=36, rnd=rnd)
        comment = q.get("comment") or ""
        if comment:
            cx = mark_x + 48
            if c.measure(comment, COMMENT_SIZE, F_HAND_CJK) <= (PAGE_W - MARGIN) - cx:
                hand_text_fit(c, cx, stem_base, comment, COMMENT_SIZE, PAGE_W - MARGIN, rnd=rnd)
            else:
                # 右侧放不下就**另起一行写在下面**（真实老师的批注也是这么写的）。
                # 不这么办的话文字会越过版心、被页面右边缘裁掉半句 —— 实测在 u5 踩到过。
                y += 16
                hand_text_fit(c, MARGIN + INDENT, y + 24, comment, COMMENT_SIZE,
                              PAGE_W - MARGIN, rnd=rnd)
                y += 52
        # 再圈一下题号（老师的习惯动作，也让"这题错了"更明显）
        nw = c.measure(head, BODY, F_PRINT)
        hand_circle(c, MARGIN + nw / 2.0, head_base - 11, nw / 2.0 + 8, 19, rnd=rnd)

    return y + Q_GAP


def build_page(c: Canvas, page: dict, rnd) -> None:
    """画一整页：页眉 → 逐题 → 页脚（页面下半留白就补一个答题区，避免像合成图）。"""
    y = page_header(c, page["title"], page["subtitle"], rnd)
    for q in page["questions"]:
        y = draw_question(c, q, y, rnd)
    page_footer(c)
    if PAGE_H - 150 - y > 170:
        answer_area(c, y - 10, PAGE_H - 120, label="演算区", lines=4)


def render_univ_page(page: dict, gp: GdiPlus) -> Dib:
    rnd = random.Random(page["seed"])
    dib = make_paper(page["seed"])
    canvas = Canvas(dib)
    build_page(canvas, page, rnd)
    S.gdi32.GdiFlush()
    if page.get("degrade"):
        out = photo_like(gp, dib)
        dib.close()
        return out
    return dib


# ================================================================ 标准答案
def page_meta(page: dict) -> dict:
    """页级标准答案（与 samples/ground_truth.json 同格式，verify_recognition.py 直接能用）。"""
    qs = page["questions"]
    return {
        "subject": "数学",
        "subject_detail": page["subject_detail"],
        "grade": "大学",
        "question_count": len(qs),
        "types": [q["type"] for q in qs],
        "expected_statuses": [q["status"] for q in qs],
    }


def questions_truth(page: dict) -> list:
    """逐题标准答案：识别得对不对、错因判得准不准，都靠它对答案。"""
    out = []
    for q in page["questions"]:
        out.append({
            "no": q["no"],
            "type": q["type"],
            "score": q.get("score", 0),
            "stem": q["stem"],
            "options": q.get("options") or [],
            "student_answer": q.get("pick") or q.get("answer_line") or "",
            "student_steps": [ln["t"] if isinstance(ln, dict) else ln for ln in (q.get("work") or [])],
            "status": q["status"],
            "correct_answer": q["correct_answer"],
            "error_type": q["error_type"],
            "knowledge_points": q["knowledge"],
        })
    return out


def verify_files(out_dir: str, files: list) -> bool:
    """重新读回每个文件：字节数 + 像素尺寸，缺文件/过小/尺寸不对就判失败。

    （不能直接用 make_samples.verify —— 它把路径拼到 samples\\ 根目录下。）
    """
    print("\n===== 产物校验 =====")
    print("%-30s %12s %14s  %s" % ("文件名", "字节数", "像素尺寸", "结果"))
    print("-" * 76)
    all_ok = True
    for name in files:
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            print("%-30s %12s %14s  %s" % (name, "-", "-", "失败：文件不存在"))
            all_ok = False
            continue
        size = os.path.getsize(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
            if name.lower().endswith((".jpg", ".jpeg")):
                w, h = S.jpeg_dimensions(data)
                kind = "JPEG"
            else:
                w, h = S.bmp_dimensions(data)
                kind = "BMP"
        except Exception as exc:                  # noqa: BLE001
            print("%-30s %12d %14s  %s" % (name, size, "-", "失败：%s" % exc))
            all_ok = False
            continue
        problems = []
        if size < S.MIN_BYTES:
            problems.append("小于 %d 字节" % S.MIN_BYTES)
        if (w, h) != (PAGE_W, PAGE_H):
            problems.append("尺寸应为 %dx%d" % (PAGE_W, PAGE_H))
        note = "%s %dx%d  OK" % (kind, w, h) if not problems else "失败：" + "，".join(problems)
        print("%-30s %12d %14s  %s" % (name, size, "%dx%d" % (w, h), note))
        if problems:
            all_ok = False
    print("-" * 76)
    print("结论：%s" % ("全部通过" if all_ok else "存在失败项"))
    return all_ok


def main() -> int:
    from exerarchive import logging_setup

    logging_setup.enable_utf8_console()

    ap = argparse.ArgumentParser(description="生成大学数学完备性测试样张")
    ap.add_argument("--out", default=os.path.join(S.BASE_DIR, "samples", "大学数学"),
                    help="输出目录（默认 samples\\大学数学）")
    ap.add_argument("--only", nargs="*", default=None, help="只生成指定页前缀，如 u3 u5")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("输出目录：%s" % args.out)

    pages = list(PAGES)
    degraded_src = next((p for p in PAGES if p["file"] == DEGRADED["from"]), None)
    if degraded_src:
        deg = dict(degraded_src)
        deg["file"] = DEGRADED["file"]
        deg["seed"] = DEGRADED["seed"]
        deg["subject_detail"] = DEGRADED["subject_detail"]
        deg["degrade"] = True
        pages.append(deg)

    if args.only:
        pages = [p for p in pages if p["file"].split("_")[0] in args.only]

    gp = GdiPlus()
    print("GDI+ 可用：%s%s" % (gp.ok, "" if gp.ok else "（将兜底输出 24bpp BMP）"))

    files, truth, details = [], {}, {}
    try:
        for idx, page in enumerate(pages, 1):
            dib = render_univ_page(page, gp)
            name = None
            if gp.ok:
                jpg = os.path.join(args.out, page["file"] + ".jpg")
                if gp.save_jpeg(dib, jpg):
                    name = os.path.basename(jpg)
            if not name:                       # GDI+ 不可用 → 兜底写 BMP
                bmp = os.path.join(args.out, page["file"] + ".bmp")
                S.write_bmp24(dib, bmp)
                name = os.path.basename(bmp)
            dib.close()
            files.append(name)
            truth[name] = page_meta(page)
            details[name] = questions_truth(page)
            print("[%d/%d] 已生成 %s（%d 题：%s）" % (
                idx, len(pages), name, len(page["questions"]),
                "/".join({"correct": "对", "wrong": "错", "blank": "空白"}[q["status"]]
                         for q in page["questions"])))
    except Exception as exc:                      # noqa: BLE001
        print("生成失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        gp.shutdown()

    for fname, payload in (("ground_truth.json", truth), ("questions.json", details)):
        with open(os.path.join(args.out, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

    total = sum(len(v) for v in details.values())
    stats = {}
    for qs in details.values():
        for q in qs:
            stats[q["status"]] = stats.get(q["status"], 0) + 1
    print("-" * 74)
    print("图片：%d 张　题目：%d 道（正确 %d / 错误 %d / 空白 %d）" % (
        len(files), total, stats.get("correct", 0), stats.get("wrong", 0), stats.get("blank", 0)))
    print("页级标准答案：%s" % os.path.join(args.out, "ground_truth.json"))
    print("逐题标准答案：%s" % os.path.join(args.out, "questions.json"))

    ok = verify_files(args.out, files) if files else False
    if not ok:
        print("生成失败：校验未通过。", file=sys.stderr)
        return 1
    print("自查命令：python tools\\verify_recognition.py --dir \"%s\"" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
