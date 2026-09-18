# -*- coding: utf-8 -*-
"""提示词模板：识别、错题分析、变式题、云端精修。

设计要点（直接影响识别质量与稳定性）：
- **强制 JSON**：本地 qwen3-vl:4b 这类小模型容易「话痨」，模板里反复强调只输出 JSON，
  并要求字段名固定；解析层还有容错（见 ``vlm.extract_json``）。
- **枚举值写死**：学科/题型/状态都用固定中文取值，便于归档目录与统计。
- **留空而不是编造**：明确要求「看不清就填 unknown」，避免小模型幻觉出错误答案，
  置信度低的题会被路由到云端复核。
- **少样本**：给一个 1 题的示例，小模型照抄格式的准确率明显更高。
"""

from __future__ import annotations

# 学科 / 题型 / 状态的固定取值（统计、归档、搜索都依赖它们）
SUBJECTS = ["数学", "英语", "物理", "化学", "生物", "语文", "其他"]
SUBJECT_DETAIL_EXAMPLES = {
    "数学": ["高等数学", "线性代数", "概率统计", "初等代数", "几何", "三角", "函数", "数列"],
    "英语": ["语法", "词汇", "完形填空", "阅读理解", "写作", "翻译"],
    "物理": ["力学", "电磁学", "热学", "光学", "近代物理"],
    "化学": ["无机化学", "有机化学", "化学计算", "实验"],
}
QTYPES = ["选择题", "填空题", "解答题", "计算题", "判断题", "简答题", "证明题", "作文题", "其他"]
STATUSES = ["wrong", "correct", "blank"]
STATUS_CN = {"wrong": "错题", "correct": "正确题", "blank": "未做题", "unknown": "未判定"}
GRADES = ["小学", "初中", "高中", "大学", "其他"]


RECOGNIZE_SYSTEM = "你是严谨的习题识别引擎，只输出 JSON，绝不输出解释性文字。"

RECOGNIZE_PROMPT = """请识别这张习题图片，并把结果**严格**输出为一个 JSON 对象。

输出格式（字段名必须完全一致，不要加注释、不要用 Markdown 代码块）：
{{
  "subject": "数学",
  "subject_detail": "高等数学",
  "grade": "大学",
  "image_quality": "清晰",
  "questions": [
    {{
      "index": 1,
      "type": "选择题",
      "status": "wrong",
      "stem": "完整题干（含题号、已知条件、要问的问题）",
      "options": ["A. 选项内容", "B. 选项内容"],
      "student_answer": "学生手写的答案，没有手写就填空字符串",
      "student_steps": "学生手写的解题步骤，没有就填空字符串",
      "correct_answer": "你能确定的参考答案，不确定填空字符串",
      "knowledge_points": ["导数", "函数最值"],
      "difficulty": "中等",
      "confidence": 0.86
    }}
  ]
}}

判定规则：
1. subject 只能取：{subjects}；subject_detail 写具体分支（如 线性代数、微积分、力学、完形填空），
   不确定时写 subject 本身。
2. grade 只能取：{grades}（按题目难度与内容判断学段）。
3. type 只能取：{qtypes}。
4. status 判定依据：
   - wrong：有红笔打叉 ✗、有"错/符号错/单位漏了"等批注、或学生作答与正确答案明显不符；
   - correct：有红笔对勾 ✓、或学生作答正确；
   - blank：该题位置空白、没有任何作答痕迹（注意：题干本身不算作答）。
5. 一道题一个元素，按图片中从上到下的顺序编号，最多输出 {max_q} 道题。
6. 看不清的字用 "?" 占位，无法判断的字段填空字符串；**不要编造题目内容**。
7. confidence 是你对本题识别（题干+状态）准确的把握，0~1 之间的小数。

**数学写法（重要）**：所有数学式子一律用标准 LaTeX 写在 `$...$` 里，
不要写成 `e^(2πik/6)`、`1/2` 这种纯文本形式。示例：
- 上下标：`$x^2$`、`$x^{{n-1}}$`、`$a_{{ij}}$`、`$e^{{2\\pi i k/6}}$`
- 分式：`$\\frac{{a}}{{b}}$`、`$\\frac{{-b\\pm\\sqrt{{b^2-4ac}}}}{{2a}}$`
- 根号 / 积分 / 求和 / 极限：`$\\sqrt{{x+1}}$`、`$\\int_0^1 x^2\\,dx$`、`$\\sum_{{i=1}}^{{n}} a_i$`、`$\\lim_{{x\\to 0}}\\frac{{\\sin x}}{{x}}$`
- 矩阵行列式：`$\\begin{{pmatrix}} 1 & 2 \\\\ 3 & 4 \\end{{pmatrix}}$`、`$\\begin{{vmatrix}} 1 & 2 \\\\ 3 & 4 \\end{{vmatrix}}$`
- 希腊字母与符号：`$\\alpha$`、`$\\pi$`、`$\\theta$`、`$\\le$`、`$\\ge$`、`$\\Delta$`、`$\\infty$`

只输出 JSON。"""


RECOGNIZE_RETRY_PROMPT = """上一次输出不是合法 JSON。请**只**输出 JSON 对象本身，
第一个字符必须是 {{，最后一个字符必须是 }}，不要任何前言、解释或 ``` 代码块。"""


ANALYZE_SYSTEM = "你是中学与大学的一线教师，擅长错题归因与提分训练，只输出 JSON。"

ANALYZE_PROMPT = """下面是一道学生的**错题**（已经在图片中识别出来）。请你做一份完整、通俗、可执行的错题分析。

题目信息（JSON）：
{question}

要求输出**严格 JSON**（字段名一致，不要 Markdown 代码块）：
{{
  "restate": "错题还原：完整题干 + 学生的错误答案与错误步骤，用 2-4 句话写清楚",
  "causes": [
    {{"type": "公式记错", "detail": "把行列式展开时的符号规则记反了，导致第二项符号出错"}}
  ],
  "knowledge_points": [
    {{"name": "二阶行列式", "textbook": "线性代数·行列式", "note": "主对角线乘积减副对角线乘积，注意符号"}}
  ],
  "solution_steps": ["第一步……", "第二步……", "第三步……"],
  "final_answer": "标准答案（含单位）",
  "avoid_tips": ["同类题的易错点1", "解题注意事项2", "快速解法/口诀3"],
  "variants": [
    {{"stem": "同类变式练习题题干", "answer": "变式题答案", "hint": "提示"}}
  ]
}}

要求：
- causes 的 type 只能取：审题失误、公式记错、知识点盲区、计算失误、思路偏差、粗心大意、书写不规范、时间不够。
- knowledge_points 至少 1 条，要具体到课本章节/考点（例如"微积分·定积分·换元法"）。
- solution_steps 要逐步可复现。
- **所有数学式子用标准 LaTeX 写在 `$...$` 里**（软件会排版成教科书样式），例如
  `$x^2$`、`$x^{{n-1}}$`、`$\\frac{{a}}{{b}}$`、`$\\sqrt{{b^2-4ac}}$`、`$\\int_0^1 x^2\\,dx$`、
  `$\\sum_{{i=1}}^{{n}} a_i$`、`$\\lim_{{x\\to 0}}\\frac{{\\sin x}}{{x}}$`、
  `$\\begin{{pmatrix}} 1 & 2 \\\\ 3 & 4 \\end{{pmatrix}}$`、`$\\alpha$`、`$\\pi$`、`$\\le$`。
  不要写成 `e^(2πik/6)`、`1/2` 这类纯文本形式。
- variants 给 1-2 道同类变式题（数值换一下，考同一个知识点），并给出答案与提示。
- 全部用中文，语气像老师在讲题，不要空话、不要"建议多练习"这类泛泛而谈。

只输出 JSON。"""


ANALYZE_BATCH_PROMPT = """下面是一个学生的多道错题。请为每一道题都输出一份错题分析。

错题列表（JSON 数组）：
{questions}

输出**严格 JSON 对象**：{{"reports":[{{"index":1, "restate":"…", "causes":[{{"type":"…","detail":"…"}}],
"knowledge_points":[{{"name":"…","textbook":"…","note":"…"}}], "solution_steps":["…"],
"final_answer":"…", "avoid_tips":["…"], "variants":[{{"stem":"…","answer":"…","hint":"…"}}]}}]}}
其中 index 必须与输入错题的 index 对应；causes 的 type 只能取：审题失误、公式记错、知识点盲区、
计算失误、思路偏差、粗心大意、书写不规范、时间不够。只输出 JSON。"""


CLOUD_RECOGNIZE_PROMPT = """请识别这张习题图片，输出**严格 JSON**（不要 Markdown 代码块）：
{{
  "subject": "学科", "subject_detail": "细分学科", "grade": "学段",
  "questions": [{{"index":1, "type":"题型", "status":"wrong|correct|blank", "stem":"题干",
  "options":["A. …"], "student_answer":"学生答案", "student_steps":"学生步骤",
  "correct_answer":"参考答案", "knowledge_points":["知识点"], "difficulty":"易|中等|难",
  "confidence":0.9}}]
}}
学科取：{subjects}；题型取：{qtypes}；学段取：{grades}。
status：wrong=有错批/答案错误，correct=对勾或正确，blank=空白未作答。
看不清就填 "?"，不要编造；最多输出 {max_q} 道题。只输出 JSON。"""


CLOUD_VERIFY_PROMPT = """下面是本地轻量模型对一张习题图片的识别结果，可能存在错误。请你复核并**修正**。

本地识别结果：
{local_result}

人工/图片之外的补充信息：{hint}

请重新输出**严格 JSON**，字段与上面完全一致（subject / subject_detail / grade / questions[…]，每题的
index、type、status、stem、options、student_answer、student_steps、correct_answer、knowledge_points、
difficulty、confidence）。重点复核：学科细分、题型、错题/正确/空白判定、手写作答内容。
只输出 JSON。"""


def recognize_prompt(max_questions: int = 8) -> str:
    return RECOGNIZE_PROMPT.format(
        subjects="/".join(SUBJECTS),
        grades="/".join(GRADES),
        qtypes="/".join(QTYPES),
        max_q=max_questions,
    )


def cloud_recognize_prompt(max_questions: int = 8) -> str:
    return CLOUD_RECOGNIZE_PROMPT.format(
        subjects="/".join(SUBJECTS),
        grades="/".join(GRADES),
        qtypes="/".join(QTYPES),
        max_q=max_questions,
    )


def status_cn(status: str) -> str:
    return STATUS_CN.get(str(status), "未判定")


def normalize_subject(value: str) -> str:
    """把模型输出的学科归一化到大类（容忍"数学类""高中数学"这类写法）。"""
    v = str(value or "").strip()
    if not v:
        return "其他"
    for s in SUBJECTS:
        if s and s in v:
            return s
    # 常见别名
    alias = {
        "高数": "数学",
        "微积分": "数学",
        "线代": "数学",
        "线性代数": "数学",
        "english": "英语",
        "physics": "物理",
        "math": "数学",
        "maths": "数学",
    }
    low = v.lower()
    for k, s in alias.items():
        if k in v or k in low:
            return s
    return "其他"


def normalize_status(value: str) -> str:
    """把状态归一化到 wrong/correct/blank（容忍中文、符号、英文写法）。

    坑：早期版本用「关键词是否出现在字符串里」判断，结果 ``"unknown"`` 里含有 ``"no"``
    被误判成错题。现在改为：先精确匹配，再用**长度≥2 的关键词**做子串匹配（按长度从长到短），
    并显式处理"不确定/无法判断"这类未知状态。
    """
    v = str(value or "").strip().lower()
    if not v:
        return "unknown"
    if v in STATUSES:
        return v
    if v in ("unknown", "不确定", "无法判断", "无法确定", "不明", "未判定", "n/a", "na", "null", "none"):
        return "unknown"

    exact = {
        "错题": "wrong", "错误": "wrong", "错": "wrong", "做错": "wrong", "false": "wrong",
        "incorrect": "wrong", "wrong": "wrong", "✗": "wrong", "✘": "wrong", "×": "wrong", "x": "wrong",
        "正确": "correct", "正确题": "correct", "对": "correct", "做对": "correct", "true": "correct",
        "right": "correct", "correct": "correct", "✓": "correct", "✔": "correct", "√": "correct",
        "空白": "blank", "未做": "blank", "未做题": "blank", "未作答": "blank", "未答": "blank",
        "空": "blank", "blank": "blank", "empty": "blank", "unanswered": "blank",
    }
    if v in exact:
        return exact[v]

    # 子串匹配：关键词长度 ≥ 2，按长度降序，避免短词误伤
    keywords = [
        ("没有作答", "blank"), ("未作答", "blank"), ("未做题", "blank"), ("未做", "blank"),
        ("没做", "blank"), ("空白", "blank"), ("打叉", "wrong"), ("红叉", "wrong"),
        ("错误", "wrong"), ("做错", "wrong"), ("错题", "wrong"), ("不符", "wrong"),
        ("对勾", "correct"), ("打勾", "correct"), ("正确", "correct"), ("做对", "correct"),
    ]
    for kw, status in keywords:
        if kw in v:
            return status
    return "unknown"


def normalize_qtype(value: str) -> str:
    """把题型归一化到固定取值。"""
    v = str(value or "").strip()
    if not v:
        return "其他"
    if v in QTYPES:
        return v
    table = [
        ("选择", "选择题"),
        ("单选", "选择题"),
        ("多选", "选择题"),
        ("填空", "填空题"),
        ("计算", "计算题"),
        ("解答", "解答题"),
        ("应用", "解答题"),
        ("判断", "判断题"),
        ("简答", "简答题"),
        ("问答", "简答题"),
        ("证明", "证明题"),
        ("作文", "作文题"),
        ("写作", "作文题"),
        ("choice", "选择题"),
        ("blank", "填空题"),
        ("fill", "填空题"),
    ]
    low = v.lower()
    for k, t in table:
        if k in v or k in low:
            return t
    return "其他"


def normalize_grade(value: str) -> str:
    """归一化学段。"""
    v = str(value or "").strip()
    if not v:
        return "其他"
    if v in GRADES:
        return v
    for g in ("大学", "高中", "初中", "小学"):
        if g in v:
            return g
    if any(k in v for k in ("本科", "大一", "大二", "考研", "高数", "线性代数", "微积分", "college")):
        return "大学"
    if any(k in v for k in ("高一", "高二", "高三", "高考")):
        return "高中"
    if any(k in v for k in ("初一", "初二", "初三", "中考")):
        return "初中"
    return "其他"
