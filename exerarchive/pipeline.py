# -*- coding: utf-8 -*-
"""识别流水线：预处理 → 本地轻量模型 → 置信度判定 → 云端强力模型兜底 → 结果归一化。

三种运行模式（设置里可切换）
--------------------------
- ``local_only`` ：全程本地，断网也能用（隐私最强）
- ``hybrid``     ：本地优先，遇到「模糊 / 置信度低 / 识别失败 / 题目太多」自动转云端（默认）
- ``cloud_first``：直接云端处理（需要 API Key；最准但依赖网络）

无论哪种模式，**本地留存所有原始数据**：原图、预处理图、识别文本都在本机；
云端只临时接收待分析的数据（可在隐私设置里关掉图片上传，只发文字）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import imageops, paths, prompts
from .logging_setup import get_logger
from .vlm import ChatResult, ModelClient, extract_json

log = get_logger("pipeline")


@dataclass
class RecognizeOutcome:
    """一张图片的识别结果。"""

    original_path: str = ""
    work_path: str = ""                     # 预处理后的图片（用于归档/送模型）
    questions: List[dict] = field(default_factory=list)
    engine: str = ""                        # 最终结果来自哪个引擎
    route: str = ""                         # ""=未定 / local / cloud / local→cloud / cloud→local
    quality: Dict = field(default_factory=dict)
    preprocess_steps: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    confidence: float = 0.0
    elapsed: float = 0.0
    error: str = ""
    local_raw: str = ""
    cloud_raw: str = ""
    image_meta: Dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.questions) and not self.error

    @property
    def question_count(self) -> int:
        return len(self.questions)

    def summary(self) -> str:
        parts = [f"{self.question_count} 道题", f"引擎 {self.engine or '—'}"]
        if self.preprocess_steps:
            parts.append("预处理：" + "，".join(self.preprocess_steps))
        if self.quality:
            parts.append(f"质量分 {self.quality.get('score', 0):.2f}")
        if self.error:
            parts.append("错误：" + self.error)
        return "；".join(parts)


# --------------------------------------------------------------------------- #
# 结果归一化
# --------------------------------------------------------------------------- #
def _as_float(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        if v != v:  # NaN
            return default
        return v
    except Exception:
        return default


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # 兼容 [{"name": "…"}] 这类写法
                for key in ("name", "text", "point", "value"):
                    if item.get(key):
                        out.append(str(item[key]).strip())
                        break
        return out
    return [str(value)]


def _clean_text(value, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = " ".join(_as_list(value))
    text = str(value).strip()
    text = text.replace("\r\n", "\n")
    return text[:limit]


def normalize_question(item: dict, index: int, engine: str) -> dict:
    """把模型输出的一道题归一化成内部结构（字段齐全、取值合法）。"""
    if not isinstance(item, dict):
        item = {"stem": str(item)}
    qtype = prompts.normalize_qtype(item.get("type") or item.get("qtype") or item.get("题型"))
    status = prompts.normalize_status(item.get("status") or item.get("状态"))
    knowledge = _as_list(
        item.get("knowledge_points") or item.get("knowledge") or item.get("知识点")
    )
    conf = _as_float(item.get("confidence", item.get("置信度")), 0.0)
    if conf > 1.0:  # 有的模型会写 90 表示 90%
        conf = conf / 100.0
    conf = max(0.0, min(1.0, conf))
    options = _as_list(item.get("options") or item.get("选项"))
    return {
        "index": int(item.get("index") or index),
        "type": qtype,
        "qtype": qtype,          # 与数据库字段同名，界面/导出直接取用不踩空
        "status": status,
        "stem": _clean_text(item.get("stem") or item.get("题干") or item.get("question")),
        "options": options,
        "student_answer": _clean_text(item.get("student_answer") or item.get("学生答案"), 800),
        "student_steps": _clean_text(item.get("student_steps") or item.get("学生步骤"), 2000),
        "correct_answer": _clean_text(item.get("correct_answer") or item.get("参考答案"), 1500),
        "knowledge_points": knowledge,
        "difficulty": _clean_text(item.get("difficulty") or item.get("难度"), 20) or "未知",
        "confidence": conf,
        "engine": engine,
    }


def normalize_result(obj, engine: str, max_questions: int = 20) -> Dict:
    """把模型返回的 JSON 归一化成 ``{"subject":…, "questions":[…] }``。"""
    if obj is None:
        return {"subject": "", "subject_detail": "", "grade": "", "questions": []}
    if isinstance(obj, list):
        obj = {"questions": obj}
    if isinstance(obj, dict) and "questions" not in obj:
        # 有的模型直接返回单题
        if any(k in obj for k in ("stem", "题干", "student_answer")):
            obj = {"questions": [obj]}
        else:
            # 有的模型用中文键名包一层（{"学科":…, "题目":[…] }）——
            # 注意要把顶层的学科/学段等标量字段保留下来，不能整段替换掉
            for key in ("data", "result", "items", "题目", "results", "questions_list"):
                inner = obj.get(key)
                if isinstance(inner, dict):
                    merged = {k: v for k, v in obj.items() if k != key}
                    merged.update(inner)
                    obj = merged
                    break
                if isinstance(inner, list):
                    merged = {k: v for k, v in obj.items()
                              if k != key and not isinstance(v, (list, dict))}
                    merged["questions"] = inner
                    obj = merged
                    break
    questions_raw = obj.get("questions") or obj.get("items") or obj.get("题目") or []
    if isinstance(questions_raw, dict):
        questions_raw = [questions_raw]
    questions: List[dict] = []
    for i, item in enumerate(questions_raw or [], start=1):
        q = normalize_question(item, i, engine)
        if q["stem"] or q["student_answer"] or q["options"]:
            questions.append(q)
        if len(questions) >= max_questions:
            break
    return {
        "subject": prompts.normalize_subject(obj.get("subject") or obj.get("学科") or ""),
        "subject_detail": _clean_text(
            obj.get("subject_detail") or obj.get("subject_detailed") or obj.get("细分学科"), 40
        ),
        "grade": prompts.normalize_grade(obj.get("grade") or obj.get("学段") or ""),
        "image_quality": _clean_text(obj.get("image_quality") or obj.get("图片质量"), 20),
        "questions": questions,
    }


def merge_results(local: Dict, cloud: Dict) -> Dict:
    """本地 + 云端结果合并：以云端为准，但保留本地有而云端缺失的细节。"""
    if not cloud or not cloud.get("questions"):
        return local or {"questions": []}
    if not local or not local.get("questions"):
        return cloud
    merged = dict(cloud)
    for key in ("subject", "subject_detail", "grade"):
        if not merged.get(key):
            merged[key] = local.get(key, "")
    # 单题互补：云端题干过短而本地更长时，取信息量更大的
    by_index = {q["index"]: q for q in local["questions"]}
    for q in merged["questions"]:
        lq = by_index.get(q["index"])
        if not lq:
            continue
        for fld in ("stem", "student_answer", "student_steps"):
            if len(q.get(fld, "")) < len(lq.get(fld, "")) * 0.6:
                q[fld] = lq[fld]
        if not q.get("knowledge_points"):
            q["knowledge_points"] = lq.get("knowledge_points", [])
        if q.get("status") == "unknown":
            q["status"] = lq.get("status", "unknown")
    return merged


# --------------------------------------------------------------------------- #
# 流水线
# --------------------------------------------------------------------------- #
class Pipeline:
    """把「图片 → 结构化题目」的全过程封装起来，GUI 与命令行共用。"""

    def __init__(self, cfg, on_log: Optional[Callable[[str], None]] = None,
                 on_stage: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_log = on_log or (lambda m: log.info("%s", m))
        self.on_stage = on_stage or (lambda m: None)
        self.local = ModelClient("local", cfg)
        self.cloud = ModelClient("cloud", cfg)

    # -- 工具 -------------------------------------------------------------- #
    def log(self, msg: str) -> None:
        self.on_log(msg)

    def mode(self) -> str:
        return self.cfg.mode()

    def cloud_usable(self) -> bool:
        return self.cfg.cloud_enabled() and self.cloud.is_configured()

    # -- 预处理 ------------------------------------------------------------ #
    def preprocess(self, path: str, watermark_boxes=None, work_dir: Optional[str] = None) -> Dict:
        """按设置自动预处理图片，返回 ``{'image': Image, 'path': 输出路径, 'steps': [...]}``。"""
        run = self.cfg.run()
        img = imageops.load(path)
        quality = img.quality_score()
        steps: List[str] = []
        out_img = img
        if run.get("auto_preprocess", True) or watermark_boxes:
            out_img, steps = imageops.auto_preprocess(
                img,
                {
                    "deskew": bool(run.get("auto_skew", True)),
                    "brighten": bool(run.get("auto_brighten", True)),
                    "sharpen": bool(run.get("auto_sharpen", True)),
                    "watermark_boxes": watermark_boxes or [],
                    "max_side": int(self.cfg.get("local.max_image_side", 1600)),
                },
            )
        # 预处理结果落到 data/preprocessed/，归档与后续分析都用它
        out_dir = work_dir or str(paths.preprocessed_dir())
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(out_dir, f"{base}_prep.jpg")
        try:
            out_img.save(out_path, quality=92)
        except Exception as exc:
            self.log(f"预处理图片保存失败（不影响识别）：{exc}")
            out_path = path
        return {"image": out_img, "original": img, "path": out_path, "steps": steps, "quality": quality}

    # -- 单张识别 ---------------------------------------------------------- #
    def process_image(
        self,
        path: str,
        watermark_boxes=None,
        force: Optional[str] = None,
        skip_preprocess: bool = False,
    ) -> RecognizeOutcome:
        """识别一张习题图片（完整流水线）。``force`` 可强制 ``local`` / ``cloud``。"""
        t0 = time.time()
        out = RecognizeOutcome(original_path=str(path))
        mode = force or self.mode()
        run = self.cfg.run()
        max_q = int(run.get("max_questions_per_image", 8) or 8)

        # 1) 预处理 + 质量评估
        self.on_stage("预处理")
        try:
            if skip_preprocess:
                img = imageops.load(path)
                work_path = str(path)
                steps: List[str] = []
                quality = img.quality_score()
            else:
                pre = self.preprocess(path, watermark_boxes)
                img, work_path, steps, quality = pre["image"], pre["path"], pre["steps"], pre["quality"]
            out.work_path = work_path
            out.preprocess_steps = steps
            out.quality = quality
            out.image_meta = {"width": img.w, "height": img.h, "bytes_hint": img.w * img.h * 3}
            if steps:
                self.log("预处理：" + "，".join(steps))
            self.log(f"图片质量分 {quality['score']:.2f}（清晰度 {quality['sharpness']:.0f}，"
                     f"亮度 {quality['brightness']:.0f}）")
        except Exception as exc:
            out.error = f"图片读取/预处理失败：{exc}"
            out.elapsed = time.time() - t0
            self.log(out.error)
            return out

        local_res: Optional[ChatResult] = None
        cloud_res: Optional[ChatResult] = None
        local_norm: Dict = {}
        cloud_norm: Dict = {}

        want_cloud_first = mode == "cloud_first" and self.cloud_usable()
        if mode == "cloud_first" and not want_cloud_first:
            # 面向"只想用云端、本机没有本地模型"的用户：这时如果只报「本地模型服务不可用」，
            # 他们会以为是自己电脑缺东西，其实是云端还没配好。先把原因说清楚。
            self.log("「云端优先」已选中，但云端当前不可用（没填 API Key，或没勾「启用云端增强」）——"
                     "会退回本地识别；本机若没装本地模型，就会识别失败。"
                     "请到「设置 → 云端模型」填好 Key 并勾选启用后重试。")

        # 2) 云端优先
        if want_cloud_first:
            self.on_stage("云端识别")
            cloud_res = self._recognize_cloud(img, max_q)
            cloud_norm = normalize_result(cloud_res.json(), "cloud:" + cloud_res.engine.split(":", 1)[-1], max_q)
            out.cloud_raw = cloud_res.text
            if cloud_res.ok and cloud_norm.get("questions"):
                out.route = "cloud"
            else:
                self.log(f"云端识别未成功（{cloud_res.error or '无有效题目'}），转本地")

        # 3) 本地识别（hybrid / local_only，或云端优先失败后的兜底）
        #    注意：route 此时仍为空字符串表示「还没拿到结果」，必须走本地；
        #    早期版本把 route 默认成 "local" 导致本地识别被整段跳过（已修）。
        if not out.route:
            self.on_stage("本地识别")
            local_res = self._recognize_local(img, max_q)
            local_norm = normalize_result(
                local_res.json(), "local:" + str(self.cfg.get("local.model", "")), max_q
            )
            out.local_raw = local_res.text
            if local_res.ok and local_norm.get("questions"):
                # 云端优先模式下云端先失败了、由本地接手 —— 明确标出来，
                # 这样界面/归档文字稿里能看出"这张图本来是要走云端的"
                out.route = "cloud→local" if want_cloud_first else "local"

        # 4) 兜底判定：是否需要云端复核（纯本地模式绝不联网）
        if self.cloud_usable() and mode != "local_only" and out.route in ("local", "") and mode != "cloud_first":
            need, reason = self._need_cloud(local_norm, local_res, quality)
            if need:
                self.on_stage("云端增强")
                self.log(f"触发云端兜底：{reason}")
                cloud_res = self._recognize_cloud(img, max_q, local_norm)
                cloud_norm = normalize_result(
                    cloud_res.json(), "cloud:" + cloud_res.engine.split(":", 1)[-1], max_q
                )
                out.cloud_raw = cloud_res.text
                if cloud_res.ok and cloud_norm.get("questions"):
                    out.route = "local→cloud"

        # 5) 合并与收尾
        if cloud_norm.get("questions") and local_norm.get("questions"):
            final = merge_results(local_norm, cloud_norm)
        else:
            final = cloud_norm if cloud_norm.get("questions") else local_norm

        questions = final.get("questions", []) if isinstance(final, dict) else []
        # 单题也补上学段/学科（模型有时只在顶层给）
        subject = final.get("subject") or "其他"
        subject_detail = final.get("subject_detail") or subject
        grade = final.get("grade") or "其他"
        for q in questions:
            q["subject"] = subject
            q["subject_detail"] = subject_detail
            q["grade"] = grade
            q["source_image"] = os.path.basename(out.original_path)
            if q["status"] == "unknown":
                # 无法判定状态时按「未做题」归档更安全（不会被误当成错题要求分析）
                q["status"] = "blank"
                q["status_guessed"] = True
        out.questions = questions

        if questions:
            out.confidence = min(q["confidence"] for q in questions) if questions else 0.0
            engine = questions[0].get("engine", "")
            out.engine = engine
        else:
            out.error = out.error or (
                (cloud_res.error if cloud_res and not cloud_res.ok else "")
                or (local_res.error if local_res and not local_res.ok else "")
                or "没有识别出任何题目"
            )
            if mode == "cloud_first" and not want_cloud_first:
                # 别让"只想用云端"的用户看到一句「本地模型服务不可用」却不知道该怎么办
                out.error += "（当前是「云端优先」模式，但云端未配置：请在「设置 → 云端模型」" \
                             "填好 API Key 并勾选「启用云端增强」）"
            out.confidence = 0.0
        out.elapsed = time.time() - t0
        self.log(f"识别完成：{out.summary()}（用时 {out.elapsed:.1f}s）")
        return out

    # -- 识别调用 ---------------------------------------------------------- #
    def _recognize_local(self, img, max_q: int) -> ChatResult:
        prompt = prompts.recognize_prompt(max_q)
        res = self.local.chat(prompt, [img], system=prompts.RECOGNIZE_SYSTEM, json_mode=True, on_log=self.log)
        if res.ok and not extract_json(res.text):
            # 第一次没给出 JSON：用「只输出 JSON」再压一次（小模型偶发）
            self.log("本地模型未按 JSON 输出，正在重试…")
            res2 = self.local.chat(
                prompt + "\n\n" + prompts.RECOGNIZE_RETRY_PROMPT,
                [img],
                system=prompts.RECOGNIZE_SYSTEM,
                json_mode=True,
                on_log=self.log,
            )
            if res2.ok and extract_json(res2.text):
                return res2
        if res.ok:
            self.log(f"本地识别 {res.output_tokens} tokens，{res.tokens_per_sec:.1f} tok/s，"
                     f"用时 {res.elapsed:.1f}s")
        else:
            self.log(f"本地识别失败：{res.error}")
        return res

    def _recognize_cloud(self, img, max_q: int, local_norm: Optional[Dict] = None) -> ChatResult:
        if local_norm and local_norm.get("questions"):
            import json as _json

            prompt = prompts.CLOUD_VERIFY_PROMPT.format(
                local_result=_json.dumps(local_norm, ensure_ascii=False, indent=1)[:6000],
                hint="尽量保留本地已识别的题干，只修正错误项",
            )
        else:
            prompt = prompts.cloud_recognize_prompt(max_q)
        res = self.cloud.chat(prompt, [img], system=prompts.RECOGNIZE_SYSTEM, json_mode=True, on_log=self.log)
        if res.ok:
            self.log(f"云端识别 {res.output_tokens} tokens，用时 {res.elapsed:.1f}s")
        else:
            self.log(f"云端识别失败：{res.error}")
        return res

    # -- 路由判定 ---------------------------------------------------------- #
    def _need_cloud(self, local_norm: Dict, local_res: Optional[ChatResult], quality: Dict) -> tuple:
        """返回 (是否需要云端兜底, 原因)。"""
        run = self.cfg.run()
        thr = float(run.get("confidence_threshold", 0.7) or 0.7)
        qthr = float(run.get("quality_threshold", 0.35) or 0.35)

        if local_res is None or not local_res.ok:
            if run.get("cloud_on_error", True):
                return True, f"本地识别失败（{getattr(local_res, 'error', '未执行')}）"
            return False, ""
        questions = local_norm.get("questions", [])
        if not questions:
            return True, "本地没有识别出题目"
        low = [q for q in questions if q.get("confidence", 0) < thr]
        if low and run.get("fallback_on_low_confidence", True):
            worst = min(q.get("confidence", 0) for q in low)
            return True, f"有 {len(low)} 道题置信度低于 {thr:.2f}（最低 {worst:.2f}）"
        unknown = [q for q in questions if q.get("status") == "unknown"]
        if unknown:
            return True, f"有 {len(unknown)} 道题无法判定对错状态"
        if quality.get("score", 1.0) < qthr and run.get("fallback_on_blur", True):
            return True, f"图片质量分偏低（{quality.get('score', 0):.2f} < {qthr:.2f}）"
        if quality.get("sharpness", 999) < 40 and run.get("fallback_on_blur", True):
            return True, f"图片偏模糊（清晰度 {quality.get('sharpness', 0):.0f}）"
        return False, ""

    # -- 多张批量 ---------------------------------------------------------- #
    def process_batch(self, image_paths: List[str], on_item=None, should_stop=None) -> List[RecognizeOutcome]:
        """按顺序处理多张图片（显存有限，默认串行；``max_concurrency`` 预留）。"""
        results: List[RecognizeOutcome] = []
        total = len(image_paths)
        for i, p in enumerate(image_paths, start=1):
            if should_stop and should_stop():
                self.log("已按用户要求停止")
                break
            self.on_stage(f"第 {i}/{total} 张")
            self.log(f"—— 开始识别第 {i}/{total} 张：{os.path.basename(p)}")
            try:
                outcome = self.process_image(p)
            except Exception as exc:
                outcome = RecognizeOutcome(original_path=str(p), error=f"{type(exc).__name__}: {exc}")
                self.log(f"识别异常：{outcome.error}")
            results.append(outcome)
            if on_item:
                try:
                    on_item(i, total, outcome)
                except Exception:
                    pass
        return results
