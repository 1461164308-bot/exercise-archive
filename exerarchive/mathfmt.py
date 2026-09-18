# -*- coding: utf-8 -*-
"""公式文本处理：把一段文字切成「普通文本 / 数学公式」片段，并提供纯文本回退写法。

两个用途：
1. **界面**：:func:`to_segments` 找出哪些片段要交给 :mod:`exerarchive.mathdraw`
   渲染成教科书式公式图，其余按普通文字显示。
2. **导出**：TXT / Markdown 里没法嵌图，用 :func:`math_to_plain` 把 LaTeX 转成
   尽量好读的 Unicode 写法（``\\frac{a}{b}`` → ``a/b``、``x^{2}`` → ``x²``、
   ``\\sqrt{x}`` → ``√x``、``\\alpha`` → ``α``），至少不会满屏反斜杠。

识别规则（按优先级）：
- ``$$...$$``、``\\[...\\]``：独立成行的公式
- ``$...$``、``\\(...\\)``：行内公式
- **裸公式**：模型有时不写 ``$``，直接给 ``e^(2πik/6)``、``x^(n-1)+...+1``、
  ``\\frac{1}{2}``。这类片段含 ``^``/``_``/反斜杠命令/数学符号时按公式处理
  （纯文字里出现 ``3 个``、``2024-2025`` 这种不会误判，因为没有数学信号）。
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .mathdraw import CMD_SYMBOLS

Segment = Tuple[str, str]          # ("text", 文本) 或 ("math", LaTeX)

# 裸公式里允许出现的字符（字母数字、运算符、括号、反斜杠命令、常见数学符号）
# 注意：反斜杠必须在内，否则 "\frac{a}{b}" 会被从中间截断而认不出来（踩过）
_MATH_BODY = set(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "\\+-=*/^_()[]{}.,'|<>!%:; "
    "αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ"
    "≤≥≠≈≡∞∫∑∏√±×÷·⋅∂∇∈∉⊂⊃∪∩∅∀∃→←↔⇒⇔°∠⊥∥△…⋯′″"
)
# 出现这些才算「数学信号」，否则当成普通文字
_MATH_SIGNALS = ("^", "_", "\\", "√", "∫", "∑", "∏", "≤", "≥", "≠", "≈", "∞", "±", "→", "⇒", "=")

_DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
_INLINE_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)|\\\((.+?)\\\)", re.S)


def _strip_dollars(text: str) -> str:
    return (text or "").replace("$$", "").replace("$", "")


def _is_math_run(run: str) -> bool:
    """判断一段「裸文本」是不是数学公式。"""
    s = run.strip()
    if not s or len(s) > 400:
        return False
    if not any(sig in s for sig in _MATH_SIGNALS):
        return False
    # 至少要有数字或字母，避免把 "..."、"→" 这种单独符号当公式
    return any(ch.isalnum() for ch in s)


def to_segments(text: str) -> List[Segment]:
    """把文本切成 ``[("text", …), ("math", latex), …]``。

    先按显式 ``$…$`` 切；剩下的普通文字里再找「裸公式」片段。
    """
    if not text:
        return []
    out: List[Segment] = []
    pos = 0
    pattern = re.compile(
        r"\$\$(?P<disp>.+?)\$\$|\\\[(?P<disp2>.+?)\\\]"
        r"|(?<!\$)\$(?!\$)(?P<inl>[^$\n]+?)\$(?!\$)|\\\((?P<inl2>.+?)\\\)",
        re.S,
    )
    for m in pattern.finditer(text):
        head = text[pos:m.start()]
        if head:
            out.extend(_split_bare(head))
        body = m.group("disp") or m.group("disp2") or m.group("inl") or m.group("inl2") or ""
        body = normalize_math_text(body.strip())
        if body:
            out.append(("math", body))
        pos = m.end()
    tail = text[pos:]
    if tail:
        out.extend(_split_bare(tail))
    return [(k, v) for k, v in out if v]


def _split_bare(text: str) -> List[Segment]:
    """在普通文字里找出裸公式片段（含 ^ _ \\ 或数学符号的连续片段）。"""
    out: List[Segment] = []
    buf: List[str] = []
    run: List[str] = []

    def flush_text() -> None:
        if buf:
            out.append(("text", "".join(buf)))
            buf.clear()

    def flush_run() -> None:
        if not run:
            return
        chunk = "".join(run)
        run.clear()
        # 首尾的空白还给正文，避免公式图两侧多出空隙
        lead = chunk[: len(chunk) - len(chunk.lstrip())]
        trail = chunk[len(chunk.rstrip()):]
        core = chunk.strip()
        if _is_math_run(core):
            if lead:
                buf.append(lead)
            flush_text()
            out.append(("math", normalize_math_text(core)))
            if trail:
                buf.append(trail)
        else:
            buf.append(chunk)

    for ch in text:
        if ch in _MATH_BODY:
            run.append(ch)
        else:
            flush_run()
            buf.append(ch)
    flush_run()
    flush_text()
    return out


# --------------------------------------------------------------------------- #
# 纯文本回退（TXT / Markdown 导出、复制到聊天窗口时用）
# --------------------------------------------------------------------------- #
_SUP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
        "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽",
        ")": "⁾", "n": "ⁿ", "i": "ⁱ", "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "x": "ˣ"}
_SUB = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
        "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍",
        ")": "₎", "n": "ₙ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "x": "ₓ", "a": "ₐ"}


def _read_group(text: str, start: int) -> Tuple[str, int]:
    """start 指向 '{'，返回 (组内容, '}' 之后的位置)。"""
    depth, i = 1, start + 1
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start + 1:i - 1], i


def _to_script(body: str, table: dict) -> str:
    """能整体转成上下标字符就转，否则保留 ^(...) 写法。"""
    plain = math_to_plain(body)
    if plain and all(c in table for c in plain):
        return "".join(table[c] for c in plain)
    return None


# 这些命令没有对应的可见符号：要么直接删掉，要么把参数原样展开
_DROP_CMDS = ("left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr",
              "Bigl", "Bigr", "displaystyle", "textstyle", "scriptstyle",
              "limits", "nolimits", "mathstrut", "strut", "phantom", "hspace",
              "vspace", "allowbreak", "nonumber")
_UNWRAP_CMDS = ("text", "textrm", "textnormal", "mathrm", "mathbf", "mathit",
                "mathsf", "mathtt", "mathcal", "mathbb", "mathfrak", "boldsymbol",
                "operatorname", "mbox", "hbox", "textsf", "texttt")


def math_to_plain(latex: str) -> str:
    """把 LaTeX 转成尽量好读的纯文本（用于 TXT/Markdown 与复制粘贴）。"""
    s = _strip_dollars(latex or "").strip()
    if not s:
        return ""
    # 0) 先展开「字体/文本包装」命令并删掉纯排版命令 —— 否则会留下字面的 text/left/right
    #    （实测踩过：\cos\left(\frac{k\pi}{3}\right) 被转成 "cosleft(kπ/3right)"）
    for _ in range(6):
        m = re.search(r"\\([A-Za-z]+)\s*\{", s)
        if not m:
            break
        name = m.group(1)
        if name in _UNWRAP_CMDS:
            body, end = _read_group(s, m.end() - 1)
            s = s[:m.start()] + math_to_plain(body) + s[end:]
        elif name in ("frac", "dfrac", "tfrac", "sqrt"):
            break                      # 交给下面的专门处理
        else:
            break
    s = re.sub(r"\\(?:" + "|".join(_DROP_CMDS) + r")\b", "", s)
    # 矩阵 / 行列式 → [a b; c d]
    def matrix_repl(m: re.Match) -> str:
        env = m.group("env")
        rows = []
        for raw in re.split(r"\\\\", m.group("body")):
            row = raw.strip()
            if row:
                rows.append(" ".join(c.strip() for c in row.split("&")))
        body = "; ".join(rows)
        if env in ("vmatrix", "Vmatrix"):
            return f"|{body}|"
        if env == "bmatrix":
            return f"[{body}]"
        return f"({body})"

    s = re.sub(r"\\begin\{(?P<env>[pbvBV]?matrix|array|cases)\}(?P<body>.*?)\\end\{\1\}",
               matrix_repl, s, flags=re.S)
    # \frac{a}{b} → (a)/(b)；简单情形省掉括号
    for _ in range(6):        # 嵌套分数要反复替换
        m = re.search(r"\\[dt]?frac\s*\{", s)
        if not m:
            break
        num, i = _read_group(s, m.end() - 1)
        j = i
        while j < len(s) and s[j] == " ":
            j += 1
        if j >= len(s) or s[j] != "{":
            break
        den, k = _read_group(s, j)
        num_p, den_p = math_to_plain(num), math_to_plain(den)
        wrap = lambda t: t if re.fullmatch(r"[\w.]+", t) else f"({t})"
        s = s[:m.start()] + f"{wrap(num_p)}/{wrap(den_p)}" + s[k:]
    # \sqrt[n]{x} → ⁿ√(x)；\sqrt{x} → √(x)
    for _ in range(6):
        m = re.search(r"\\sqrt\s*(\[([^\]]*)\])?\s*\{", s)
        if not m:
            break
        deg = (m.group(2) or "").strip()
        body, k = _read_group(s, m.end() - 1)
        body_p = math_to_plain(body)
        head = (deg + "√") if deg else "√"
        s = s[:m.start()] + f"{head}({body_p})" + s[k:]
    # 上下标
    def sup_repl(m: re.Match) -> str:
        arg = m.group(1) or m.group(2) or ""
        conv = _to_script(arg, _SUP)
        return conv if conv else f"^({math_to_plain(arg)})"

    def sub_repl(m: re.Match) -> str:
        arg = m.group(1) or m.group(2) or ""
        conv = _to_script(arg, _SUB)
        return conv if conv else f"_({math_to_plain(arg)})"

    s = re.sub(r"\^\s*\{([^{}]*)\}|\^\s*(\w)", sup_repl, s)
    s = re.sub(r"_\s*\{([^{}]*)\}|_\s*(\w)", sub_repl, s)
    # 命令 → 符号（同时吃掉命令后的一个空格：LaTeX 里控制词后的空格只是分隔符，不显示）
    def cmd_repl(m: re.Match) -> str:
        name = m.group(1)
        if name in CMD_SYMBOLS:
            return CMD_SYMBOLS[name]
        if name in _DROP_CMDS:
            return ""
        return name

    s = re.sub(r"\\([A-Za-z]+)[ \t]?", cmd_repl, s)
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "").replace("\\ ", " ")
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def format_for_export(text: str) -> str:
    """导出/复制用：把整段文字里的公式片段转成纯文本写法，其余原样保留。"""
    if not text:
        return ""
    parts: List[str] = []
    for kind, value in to_segments(text):
        parts.append(math_to_plain(value) if kind == "math" else value)
    return "".join(parts)


# 一个「简单原子」：单字符、希腊字母、\命令，或带上下标的整体；原子之间允许空格
_ATOM = (r"(?:\\[A-Za-z]+|[A-Za-z0-9\u0370-\u03ff])"
         r"(?:\s*[\^_]\s*(?:\{[^{}]*\}|[A-Za-z0-9]))?")
_RUN = _ATOM + r"(?:\s*" + _ATOM + r")*"
# 分子/分母：简单原子串，或一个不含嵌套括号的括号组（括号会被去掉：e^{(2πik)/n} → e^{\\frac{2πik}{n}}）
_SIDE = r"(?:\((?P<grp_%s>[^()]*)\)|(?P<plain_%s>" + _RUN + r"))"
_SLASH_FRAC_RE = re.compile(
    r"(?P<num>" + (_SIDE % ("n", "n")) + r")\s*/\s*(?P<den>" + (_SIDE % ("d", "d")) + r")")
_TEXT_SPAN_RE = re.compile(
    r"\\(?:" + "|".join(("text", "textrm", "textnormal", "mathrm", "mathbf", "mathit",
                         "operatorname", "mbox")) + r")\s*\{[^{}]*\}")


def stack_fractions(latex: str) -> str:
    """把数学模式里的 ``A/B`` 转成竖排分数 ``\\frac{A}{B}``（课本写法）。

    用户反馈：``e^{2πik/n}`` 的指数里用斜杠很难读，课本里是**上下分数**。
    只转换「两边都是简单原子」的情形（字母/数字/希腊字母/带上下标的整体），
    复杂式子如 ``(a+b)/(c-d)`` 保持原样交给模型自己写 ``\\frac``。
    ``\\text{...}`` 里的内容（如 ``\\text{km/h}``）不动。
    """
    src = latex or ""
    if "/" not in src:
        return src
    # 先把文字模式的片段抠出来占位，避免被改写
    spans: List[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x01{len(spans) - 1}\x02"

    work = _TEXT_SPAN_RE.sub(stash, src)
    for _ in range(2):                      # 允许嵌套一层，如 a/b/c 只转第一处
        def repl(m: re.Match) -> str:
            num = (m.group("grp_n") or m.group("plain_n") or "").strip()
            den = (m.group("grp_d") or m.group("plain_d") or "").strip()
            if not num or not den:
                return m.group(0)
            if num.startswith(("\\frac", "\\dfrac", "\\tfrac")):
                return m.group(0)
            return f"\\frac{{{num}}}{{{den}}}"

        new = _SLASH_FRAC_RE.sub(repl, work)
        if new == work:
            break
        work = new
    work = re.sub(r"\x01(\d+)\x02", lambda m: spans[int(m.group(1))], work)
    return work


def normalize_math_text(text: str) -> str:
    """把模型/OCR 可能写歪的公式写法收拾成规范 LaTeX（渲染与导出前统一调用）。

    处理这些**实测遇到过**的写法：
    - 纯文本上标：``e^(2πik/6)`` → ``e^{2πik/6}``、``x^(n-1)`` → ``x^{n-1}``（下标同理）
    - 上标里多余的外层括号：``e^{(n-1)}`` → ``e^{n-1}``（课本写法）
    - 多字符上标没加花括号：``x^12`` → ``x^{12}``、``a_ij`` → ``a_{ij}``
    - 全角括号/逗号 → 半角；去掉 ``**加粗**`` 标记
    """
    s = text or ""
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)

    def read_paren(start: int) -> Tuple[str, int]:
        depth, i = 1, start + 1
        while i < len(s) and depth:
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
            i += 1
        return s[start + 1:i - 1], i

    # ^(...) / _(...) → ^{...} / _{...}（从右往左替换，避免下标错位）
    for marker in ("^", "_"):
        pos = 0
        while True:
            m = re.compile(re.escape(marker) + r"\s*\(").search(s, pos)
            if not m:
                break
            body, end = read_paren(m.end() - 1)
            s = s[:m.start()] + marker + "{" + body + "}" + s[end:]
            pos = m.start() + 1

    # 上标/下标里多余的外层括号：^{(n-1)} → ^{n-1}
    def strip_outer(m: re.Match) -> str:
        marker, body = m.group(1), m.group(2)
        inner = body.strip()
        if inner.startswith("(") and inner.endswith(")"):
            depth = 0
            balanced = True
            for idx, ch in enumerate(inner):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and idx != len(inner) - 1:
                        balanced = False
                        break
            if balanced:
                inner = inner[1:-1].strip()
        return f"{marker}{{{inner}}}"

    s = re.sub(r"([\^_])\{([^{}]*)\}", strip_outer, s)
    # 紧跟 ^ 的连续字母数字（没花括号）包起来
    s = re.sub(r"\^(\s*)(?!\{)(-?[0-9A-Za-z]{2,})", lambda m: "^{" + m.group(2) + "}", s)
    s = re.sub(r"_(\s*)(?!\{)([0-9A-Za-z]{2,})", lambda m: "_{" + m.group(2) + "}", s)
    # 除法改成竖排分数（课本写法；斜杠在角标里尤其难读）
    s = stack_fractions(s)
    return s
