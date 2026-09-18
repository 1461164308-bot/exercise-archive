# -*- coding: utf-8 -*-
"""错题分析：错题还原 → 错误原因 → 知识点 → 标准解法 → 避错技巧 → 举一反三。

三层实现，保证「任何情况下都有一份可用的报告」：

1. :func:`rule_based_report` —— **纯规则兜底**，不调用任何模型，离线也能生成基础报告
   （错题还原、原因清单、知识点标注、复习任务卡）。本地模型没装好时用它兜底。
2. :class:`Analyzer` 的**本地分析** —— 用本机轻量模型（qwen3-vl / 任意本地模型）生成
   完整报告，满足「离线也能深度分析」。
3. :class:`Analyzer` 的**云端增强** —— 云端强力模型给出更细的归因、更贴合考纲的知识点、
   以及真正的同类变式练习题（本地小模型在这块最容易编得空泛）。

报告结构（各层输出统一成同一份 dict，缺字段用规则版补齐）::

    {
      "restate": str,                      # 错题还原
      "causes": [{"type": str, "detail": str}],
      "knowledge_points": [{"name": str, "textbook": str, "note": str}],
      "solution_steps": [str],
      "final_answer": str,
      "avoid_tips": [str],
      "variants": [{"stem": str, "answer": str, "hint": str}],
      "depth": "rule" | "local" | "cloud",
      "engine": str,
    }
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from .logging_setup import get_logger
from .vlm import ModelClient, extract_json

log = get_logger("analysis")

# 错误原因分类（与提示词保持一致）
CAUSE_TYPES = [
    "审题失误", "公式记错", "知识点盲区", "计算失误", "思路偏差",
    "粗心大意", "书写不规范", "时间不够",
]

# 各学科的通用避错技巧（规则兜底用；模型可用时优先用模型给的）
AVOID_LIBRARY = {
    "数学": [
        "先把题目里的条件逐条圈出来，再动笔：已知什么、要求什么、单位是什么",
        "公式抄在草稿纸最上方再代入，避免凭记忆写错符号",
        "算完回代检验（把答案代回原式或取特殊值验证），能挡掉大部分计算失误",
    ],
    "物理": [
        "先画受力分析图/过程示意图，再列方程，避免漏力漏过程",
        "统一单位（全部换成国际单位）后再计算，答案别忘写单位",
        "检查是否漏掉方向、正负号与矢量性",
    ],
    "英语": [
        "先看题干关键词（时态标志词、搭配）再回原文定位，不要凭语感选",
        "固定搭配与不规则变化单独记在错题本上，每天滚动复习 5 分钟",
        "完形/阅读先通读一遍把握大意，再逐空作答",
    ],
    "化学": [
        "先写正确的化学方程式并配平，再按方程式做量的计算",
        "注意条件（温度、催化剂、过量/少量）对产物的影响",
        "把单位与物质的量关系写在旁边，避免摩尔换算错误",
    ],
}


def _as_text(value, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v, limit) for v in value)
    if isinstance(value, dict):
        return "；".join(f"{k}：{_as_text(v)}" for k, v in value.items())
    return str(value).strip()[:limit]


def question_to_text(q: Dict) -> str:
    """把题目 dict 拼成人/模型都易读的文本。"""
    lines = [f"学科：{q.get('subject', '')}（{q.get('subject_detail', '')}）  学段：{q.get('grade', '')}",
             f"题型：{q.get('qtype') or q.get('type', '')}  状态：{q.get('status', '')}",
             f"题干：{_as_text(q.get('stem'))}"]
    if q.get("options"):
        lines.append("选项：" + " / ".join(_as_text(o) for o in q["options"]))
    if q.get("student_answer"):
        lines.append(f"学生答案：{_as_text(q['student_answer'])}")
    if q.get("student_steps"):
        lines.append(f"学生解题步骤：{_as_text(q['student_steps'])}")
    if q.get("correct_answer"):
        lines.append(f"参考答案：{_as_text(q['correct_answer'])}")
    if q.get("knowledge_points"):
        lines.append("已知知识点：" + "、".join(_as_text(k) for k in q["knowledge_points"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 规则兜底报告
# --------------------------------------------------------------------------- #
def guess_causes(q: Dict) -> List[Dict]:
    """根据题目信息猜测错误原因（离线规则版）。"""
    causes: List[Dict] = []
    answer = _as_text(q.get("student_answer"))
    steps = _as_text(q.get("student_steps"))
    correct = _as_text(q.get("correct_answer"))
    qtype = str(q.get("qtype") or q.get("type") or "")

    if not answer and not steps:
        causes.append({"type": "知识点盲区", "detail": "该题没有作答痕迹，说明对应知识点还没有掌握，需要先复习概念与例题。"})
    if answer and correct and answer.strip() and correct.strip():
        # 只差一点点的答案（长度接近或仅符号不同）通常是计算/书写问题
        a, c = answer.strip(), correct.strip()
        if a != c and (a.replace(" ", "") == c.replace(" ", "") or abs(len(a) - len(c)) <= 1):
            causes.append({"type": "计算失误", "detail": f"作答与正确答案非常接近（你写 {a}，正确是 {c}），多半是中间步骤算错或抄错。"})
        else:
            causes.append({"type": "知识点盲区", "detail": f"作答 {a} 与正确答案 {c} 不一致，需要回到知识点本身重新梳理。"})
    if steps and len(steps) < 12:
        causes.append({"type": "书写不规范", "detail": "解题步骤写得过于简略，容易在中途丢失条件或符号。"})
    if qtype in ("选择题",) and answer:
        causes.append({"type": "审题失误", "detail": "选择题要先排除干扰项、看清问的是'正确'还是'不正确'，再落笔。"})
    if not causes:
        causes.append({"type": "知识点盲区", "detail": "该题做错，需要重新梳理对应知识点的定义、公式与典型例题。"})
    return causes[:3]


def textbook_hint(q: Dict, name: str) -> str:
    """给知识点猜一个课本出处（规则版）。"""
    subject = str(q.get("subject", ""))
    detail = str(q.get("subject_detail", ""))
    grade = str(q.get("grade", ""))
    known = {
        "导数": "高等数学/高中数学·导数及其应用",
        "极限": "高等数学·极限与连续",
        "定积分": "高等数学·定积分及其应用",
        "不定积分": "高等数学·不定积分",
        "行列式": "线性代数·行列式",
        "矩阵": "线性代数·矩阵",
        "特征值": "线性代数·特征值与特征向量",
        "概率": "概率统计·随机事件与概率",
        "牛顿第二定律": "高中物理·牛顿运动定律",
        "受力分析": "高中物理·物体的受力分析",
        "化学方程式": "化学·化学方程式与计算",
        "时态": "英语·动词时态",
        "完形填空": "英语·完形填空",
    }
    for key, val in known.items():
        if key in name or name in key:
            return val
    base = detail or subject or "本科目"
    return f"{grade}{base}·{name}" if grade else f"{base}·{name}"


def rule_based_report(q: Dict) -> Dict:
    """不依赖任何模型的基础报告（离线兜底）。"""
    stem = _as_text(q.get("stem")) or "（题干缺失）"
    answer = _as_text(q.get("student_answer"))
    steps = _as_text(q.get("student_steps"))
    correct = _as_text(q.get("correct_answer"))
    kps = [_as_text(k) for k in (q.get("knowledge_points") or []) if _as_text(k)]
    if not kps:
        kps = [str(q.get("subject_detail") or q.get("subject") or "本科目") + "相关知识点"]

    restate_parts = [f"题目：{stem}"]
    if answer:
        restate_parts.append(f"你的答案：{answer}")
    else:
        restate_parts.append("你的答案：空白/未作答")
    if steps:
        restate_parts.append(f"你的步骤：{steps}")
    if correct:
        restate_parts.append(f"正确答案：{correct}")

    steps_out: List[str] = []
    if correct:
        steps_out.append(f"① 先明确考点（{ '、'.join(kps) }），写出本题要用的公式/定理。")
        steps_out.append("② 把已知条件逐一代入，逐步化简（每一步都写出来，方便回头检查）。")
        steps_out.append(f"③ 得到结果并对照参考答案：{correct}。若不一致，回到上一步找符号/单位/计算问题。")
        steps_out.append("④ 合上答案，重新独立做一遍，确认能一次做对。")
    else:
        steps_out.append(f"① 复习考点：{'、'.join(kps)}，把定义与公式抄写一遍。")
        steps_out.append("② 对照课本例题，按例题的格式重做本题，写出完整步骤。")
        steps_out.append("③ 请老师/同学核对步骤，或补齐参考答案后对照检查。")

    subject = str(q.get("subject", ""))
    tips = AVOID_LIBRARY.get(subject, AVOID_LIBRARY["数学"])

    return {
        "restate": "\n".join(restate_parts),
        "causes": guess_causes(q),
        "knowledge_points": [
            {"name": kp, "textbook": textbook_hint(q, kp),
             "note": f"重点：{kp} 的定义、适用条件与常见变形，做到能默写并直接套用。"}
            for kp in kps
        ],
        "solution_steps": steps_out,
        "final_answer": correct or "（本地未能确定参考答案，请核对后手动补充）",
        "avoid_tips": list(tips),
        "variants": [
            {"stem": f"复习任务：把本题重做一遍（考点：{'、'.join(kps)}），要求写出完整步骤并自行检查。",
             "answer": correct or "对照参考答案", "hint": "先复述考点，再动笔；做完回代检验。"},
            {"stem": "同类变式：修改本题中的一个已知条件（如把系数或数值替换），重新求解。",
             "answer": "（自测用，做完可对照原题解法检查）", "hint": "核心考点不变，只换数字。"},
        ],
        "depth": "rule",
        "engine": "rule",
    }


# --------------------------------------------------------------------------- #
# 报告归一化与合并
# --------------------------------------------------------------------------- #
def normalize_report(obj, q: Dict, depth: str, engine: str) -> Dict:
    """把模型输出的报告归一化，缺的字段用规则版补齐。"""
    base = rule_based_report(q)
    if not isinstance(obj, dict):
        base["depth"] = depth
        base["engine"] = engine
        return base

    out = dict(base)

    # 有的模型会把报告包一层
    for key in ("report", "data", "result"):
        if isinstance(obj.get(key), dict) and any(
            k in obj[key] for k in ("restate", "causes", "solution_steps", "错题还原")
        ):
            obj = obj[key]
            break

    restate = _as_text(obj.get("restate") or obj.get("错题还原") or obj.get("question"))
    if restate:
        out["restate"] = restate

    causes = obj.get("causes") or obj.get("原因") or obj.get("错误原因")
    clean_causes: List[Dict] = []
    if isinstance(causes, list):
        for c in causes:
            if isinstance(c, dict):
                ctype = _as_text(c.get("type") or c.get("类型") or c.get("cause"))
                detail = _as_text(c.get("detail") or c.get("detail_text") or c.get("说明") or c.get("analysis"))
                if not ctype and detail:
                    ctype = "知识点盲区"
                if ctype:
                    clean_causes.append({"type": ctype[:12], "detail": detail or ctype})
            elif isinstance(c, str) and c.strip():
                clean_causes.append({"type": c.strip()[:12], "detail": c.strip()})
    if clean_causes:
        out["causes"] = clean_causes

    kps = obj.get("knowledge_points") or obj.get("知识点")
    clean_kps: List[Dict] = []
    if isinstance(kps, list):
        for kp in kps:
            if isinstance(kp, dict):
                name = _as_text(kp.get("name") or kp.get("名称") or kp.get("point"))
                if not name:
                    continue
                clean_kps.append({
                    "name": name[:60],
                    "textbook": _as_text(kp.get("textbook") or kp.get("课本") or kp.get("source")) or textbook_hint(q, name),
                    "note": _as_text(kp.get("note") or kp.get("说明") or kp.get("detail")) or f"重点掌握 {name}",
                })
            elif isinstance(kp, str) and kp.strip():
                clean_kps.append({"name": kp.strip()[:60], "textbook": textbook_hint(q, kp),
                                  "note": f"重点掌握 {kp.strip()}"})
    if clean_kps:
        out["knowledge_points"] = clean_kps

    steps = obj.get("solution_steps") or obj.get("解题步骤") or obj.get("steps")
    if isinstance(steps, str):
        steps = [s for s in re.split(r"\n+|(?<=。)\s*", steps) if s.strip()]
    if isinstance(steps, list) and steps:
        out["solution_steps"] = [_as_text(s, 800) for s in steps if _as_text(s)]

    final = _as_text(obj.get("final_answer") or obj.get("答案") or obj.get("标准答案"))
    if final:
        out["final_answer"] = final

    tips = obj.get("avoid_tips") or obj.get("避错技巧") or obj.get("tips")
    if isinstance(tips, str):
        tips = [t for t in re.split(r"\n+|(?<=。)\s*", tips) if t.strip()]
    if isinstance(tips, list) and tips:
        out["avoid_tips"] = [_as_text(t, 300) for t in tips if _as_text(t)]

    variants = obj.get("variants") or obj.get("变式题") or obj.get("相似题")
    clean_variants: List[Dict] = []
    if isinstance(variants, list):
        for v in variants:
            if isinstance(v, dict):
                stem = _as_text(v.get("stem") or v.get("question") or v.get("题干"))
                if not stem:
                    continue
                clean_variants.append({
                    "stem": stem[:1200],
                    "answer": _as_text(v.get("answer") or v.get("答案"))[:600],
                    "hint": _as_text(v.get("hint") or v.get("提示"))[:300],
                })
            elif isinstance(v, str) and v.strip():
                clean_variants.append({"stem": v.strip()[:1200], "answer": "", "hint": ""})
    if clean_variants:
        out["variants"] = clean_variants

    out["depth"] = depth
    out["engine"] = engine
    return out


def merge_reports(local: Dict, cloud: Dict) -> Dict:
    """云端深度版为主，本地版补空缺（两者都来自归一化后的报告）。"""
    if not cloud:
        return local
    if not local:
        return cloud
    out = dict(cloud)
    if not cloud.get("final_answer") or cloud.get("final_answer", "").startswith("（本地未能"):
        out["final_answer"] = local.get("final_answer", "")
    for key in ("restate", "solution_steps", "avoid_tips", "variants", "causes", "knowledge_points"):
        if not cloud.get(key):
            out[key] = local.get(key, [])
    out["depth"] = cloud.get("depth", "cloud")
    out["engine"] = cloud.get("engine", "")
    out["local_engine"] = local.get("engine", "")
    return out


def report_to_text(q: Dict, report: Dict) -> str:
    """把报告渲染成可读文本（导出、归档用）。"""
    lines: List[str] = []
    lines.append(f"【题号】{q.get('uid', '')}    学科：{q.get('subject', '')} / {q.get('subject_detail', '')}")
    lines.append(f"【题型】{q.get('qtype') or q.get('type', '')}    【状态】{q.get('status', '')}"
                 f"    【学段】{q.get('grade', '')}")
    kps = q.get("knowledge_points") or []
    if kps:
        lines.append("【知识点】" + "、".join(str(k) for k in kps))
    lines.append("")
    lines.append("一、错题还原")
    lines.append(_as_text(report.get("restate")) or "（无）")
    lines.append("")
    lines.append("二、错误原因分析")
    for c in report.get("causes", []) or []:
        if isinstance(c, dict):
            lines.append(f"  · [{c.get('type', '')}] {c.get('detail', '')}")
        else:
            lines.append(f"  · {c}")
    lines.append("")
    lines.append("三、对应知识点")
    for kp in report.get("knowledge_points", []) or []:
        if isinstance(kp, dict):
            lines.append(f"  · {kp.get('name', '')}（{kp.get('textbook', '')}）")
            if kp.get("note"):
                lines.append(f"      说明：{kp['note']}")
        else:
            lines.append(f"  · {kp}")
    lines.append("")
    lines.append("四、标准解题步骤")
    for i, s in enumerate(report.get("solution_steps", []) or [], start=1):
        lines.append(f"  {i}. {s}")
    if report.get("final_answer"):
        lines.append(f"  → 最终答案：{report['final_answer']}")
    lines.append("")
    lines.append("五、避错技巧")
    for t in report.get("avoid_tips", []) or []:
        lines.append(f"  · {t}")
    lines.append("")
    lines.append("六、举一反三（变式练习）")
    for i, v in enumerate(report.get("variants", []) or [], start=1):
        if isinstance(v, dict):
            lines.append(f"  变式{i}：{v.get('stem', '')}")
            if v.get("answer"):
                lines.append(f"        答案：{v['answer']}")
            if v.get("hint"):
                lines.append(f"        提示：{v['hint']}")
        else:
            lines.append(f"  变式{i}：{v}")
    depth_label = {"rule": "本地规则生成", "local": "本地模型生成", "cloud": "云端模型生成"}.get(
        str(report.get("depth", "")), "生成"
    )
    lines.append("")
    lines.append(f"（报告来源：{depth_label}｜引擎：{report.get('engine', '')}）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 分析器
# --------------------------------------------------------------------------- #
class Analyzer:
    """错题分析调度：本地模型 → 云端增强，失败自动降级到规则版。"""

    def __init__(self, cfg, on_log: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_log = on_log or (lambda m: log.info("%s", m))
        self.local = ModelClient("local", cfg)
        self.cloud = ModelClient("cloud", cfg)

    def cloud_usable(self) -> bool:
        return self.cfg.cloud_enabled() and self.cloud.is_configured()

    # -- 单题 -------------------------------------------------------------- #
    def analyze(self, q: Dict, allow_cloud: Optional[bool] = None) -> Dict:
        """生成错题报告。``allow_cloud`` 为 None 时按配置自动决定。"""
        run = self.cfg.run()
        if allow_cloud is None:
            allow_cloud = bool(run.get("cloud_for_analysis", True)) and self.cloud_usable()

        local_report = self._local_analyze(q)
        if not allow_cloud:
            return local_report

        cloud_report = self._cloud_analyze(q, local_report)
        if cloud_report and cloud_report.get("depth") == "cloud":
            return merge_reports(local_report, cloud_report)
        return local_report

    # -- 本地模型分析 ------------------------------------------------------ #
    def _local_analyze(self, q: Dict) -> Dict:
        from . import prompts

        model = str(self.cfg.get("local.model", ""))
        payload = json.dumps(q, ensure_ascii=False, indent=1)[:4000]
        prompt = prompts.ANALYZE_PROMPT.format(question=payload)
        try:
            res = self.local.chat(prompt, [], system=prompts.ANALYZE_SYSTEM, json_mode=True, on_log=self.on_log)
            if res.ok:
                obj = extract_json(res.text)
                if obj:
                    rep = normalize_report(obj, q, "local", f"local:{model}")
                    self.on_log(f"本地模型已生成错题分析（{res.elapsed:.1f}s）")
                    return rep
                self.on_log("本地模型分析输出无法解析为 JSON，改用规则版")
            else:
                self.on_log(f"本地模型分析失败（{res.error}），改用规则版")
        except Exception as exc:
            self.on_log(f"本地模型分析异常（{exc}），改用规则版")
        rep = rule_based_report(q)
        self.on_log("已使用本地规则生成基础分析报告")
        return rep

    # -- 云端深度分析 ------------------------------------------------------ #
    def _cloud_analyze(self, q: Dict, local_report: Dict) -> Optional[Dict]:
        from . import prompts

        prof = self.cfg.active_cloud()
        model = str(prof.get("model", ""))
        payload = json.dumps(q, ensure_ascii=False, indent=1)[:5000]
        prompt = prompts.ANALYZE_PROMPT.format(question=payload)
        prompt += (
            "\n\n下面是本地轻量模型给出的初版分析，请在此基础上**更深入、更具体**，"
            "并务必给出 1-2 道真正的同类变式题（换数值、同考点）：\n"
            + json.dumps(
                {k: local_report.get(k) for k in ("causes", "knowledge_points", "solution_steps", "final_answer")},
                ensure_ascii=False,
            )[:3000]
        )
        try:
            # 云端若支持视觉，可以把原图一起发过去，归因会更准
            images = []
            src = q.get("source_path") or q.get("work_path") or q.get("image_path")
            if src and self.cfg.get("privacy.send_image_to_cloud", True) and prof.get("supports_vision"):
                images = [src]
            res = self.cloud.chat(prompt, images, system=prompts.ANALYZE_SYSTEM, json_mode=True, on_log=self.on_log)
            if res.ok:
                obj = extract_json(res.text)
                if obj:
                    self.on_log(f"云端已完成深度分析（{res.elapsed:.1f}s）")
                    return normalize_report(obj, q, "cloud", f"cloud:{model}")
            self.on_log(f"云端分析未成功（{res.error or '输出无法解析'}），保留本地报告")
        except Exception as exc:
            self.on_log(f"云端分析异常（{exc}），保留本地报告")
        return None

    # -- 批量（云端更省 token，本地逐题更稳） ------------------------------ #
    def analyze_many(self, questions: List[Dict], on_item: Optional[Callable[[int, int, Dict], None]] = None
                     ) -> Dict[str, Dict]:
        """批量分析；返回 {题号: 报告}。"""
        from . import prompts

        out: Dict[str, Dict] = {}
        total = len(questions)
        cloud_ok = self.cloud_usable() and bool(self.cfg.run().get("cloud_for_analysis", True))

        # 云端批量：一次请求分析多题，速度快、token 省
        if cloud_ok and total > 1:
            chunk_size = 4
            for start in range(0, total, chunk_size):
                chunk = questions[start : start + chunk_size]
                payload = json.dumps(
                    [{"index": i, **{k: c.get(k) for k in
                      ("subject", "subject_detail", "grade", "qtype", "status", "stem", "options",
                       "student_answer", "student_steps", "correct_answer", "knowledge_points")}}
                     for i, c in enumerate(chunk, start=1)],
                    ensure_ascii=False, indent=1,
                )[:9000]
                prompt = prompts.ANALYZE_BATCH_PROMPT.format(questions=payload)
                res = self.cloud.chat(prompt, [], system=prompts.ANALYZE_SYSTEM, json_mode=True, on_log=self.on_log)
                obj = extract_json(res.text) if res.ok else None
                reports = (obj or {}).get("reports") if isinstance(obj, dict) else None
                by_index = {}
                if isinstance(reports, list):
                    for item in reports:
                        if isinstance(item, dict):
                            try:
                                by_index[int(item.get("index") or 0)] = item
                            except Exception:
                                continue
                for i, q in enumerate(chunk, start=1):
                    local_rep = self._local_analyze(q)
                    cloud_rep = normalize_report(by_index.get(i), q, "cloud",
                                                 f"cloud:{self.cfg.active_cloud().get('model', '')}") if by_index.get(i) else None
                    rep = merge_reports(local_rep, cloud_rep) if cloud_rep else local_rep
                    uid = str(q.get("uid") or i)
                    out[uid] = rep
                    if on_item:
                        on_item(start + i, total, rep)
            return out

        # 本地逐题
        for i, q in enumerate(questions, start=1):
            rep = self.analyze(q, allow_cloud=cloud_ok)
            out[str(q.get("uid") or i)] = rep
            if on_item:
                on_item(i, total, rep)
        return out
