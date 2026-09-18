# -*- coding: utf-8 -*-
"""本地后端适配验证：Ollama / LM Studio / 本地 OpenAI 兼容，都不需要显卡。

覆盖三件事：
1. **Ollama 协议**（``/api/chat``）：图片是裸 base64 数组、参数在 ``options`` 里、
   ``think``/``format`` 字段、``/api/tags`` 模型列表；以及两个实测踩过的坑：
   - ``think=false`` 时答案落在 ``message.thinking``、``content`` 为空 → 必须能从 thinking 取回；
   - 模型先回一段非 JSON 的"话痨"文本 → 必须自动追问"只输出 JSON"并重试一次成功。
2. **LM Studio / 本地 OpenAI 兼容**：``/v1/models`` 列表、``/v1/chat/completions``、
   图片以 ``data:image/jpeg;base64,`` 发送、走完整流水线（``local_only`` 模式）。
3. **纯本地保证**：同时配好云端 + 本地，``local_only`` 模式下**云端一个请求都收不到**（用真 HTTP 计数断言）。

用法::

    python tools/verify_local_backends.py
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from mock_cloud_server import DEFAULT_RECOGNIZE as DEFAULT_CLOUD_RECOGNIZE  # noqa: E402
from mock_cloud_server import MockCloud          # noqa: E402
from mock_ollama_server import MockOllama        # noqa: E402

from exerarchive import config, gdiplus, imageops, logging_setup  # noqa: E402
from exerarchive.pipeline import Pipeline        # noqa: E402
from exerarchive.vlm import ModelClient          # noqa: E402

OLLAMA_PORT = 11499
OPENAI_PORT = 8902
RESULTS = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"　{detail}" if detail else ""))
    RESULTS.append((label, bool(cond)))
    return bool(cond)


def make_cfg(local_base: str, kind: str, model: str, mode: str = "local_only",
             cloud_base: str = "") -> config.Config:
    cfg = config.Config({})
    cfg.set("run.mode", mode)
    cfg.set("run.auto_preprocess", False)
    cfg.set("local.kind", kind)
    cfg.set("local.base_url", local_base)
    cfg.set("local.model", model)
    cfg.set("local.auto_start", False)      # 绝不去拉真实服务
    cfg.set("local.timeout", 30)
    cfg.set("local.max_image_side", 900)
    cfg.set("local.jpeg_quality", 85)
    if cloud_base:
        cfg.set("cloud.enabled", True)
        cfg.set("cloud.profiles", [{"name": "假云端", "base_url": cloud_base,
                                    "model": "mock-vl", "api_key": "sk-mock",
                                    "supports_vision": True, "timeout": 30}])
        cfg.set("cloud.active", 0)
    return cfg


def make_image(path: str) -> str:
    if gdiplus.available():
        w, h, px = gdiplus.draw_text_bitmap(
            800, 500, font_size=30,
            lines=["1. 计算三阶行列式 D 的值", "学生作答：D = 7"])
        imageops.Image(w, h, px).save(path)
    else:
        imageops.Image(200, 100, bytearray(b"\xff" * (200 * 100 * 3))).save(path)
    return path


# --------------------------------------------------------------------------- #
def test_ollama(ollama: MockOllama, img: str) -> None:
    print("\n一、Ollama 协议（local.kind=ollama）")
    cfg = make_cfg(ollama.base_url, "ollama", ollama.model)
    client = ModelClient("local", cfg)

    check("服务探活（/api/version）", client.manager.alive())
    models = client.manager.list_models()
    check("模型列表（/api/tags）", ollama.model in models and "llama3.2:3b" in models,
          "、".join(models))

    ollama.clear()
    res = client.chat("识别这张图", [img], system="你是识别引擎", json_mode=True, timeout=30)
    req = ollama.last_post()
    body = req.get("body", {})
    msgs = body.get("messages", [])
    check("请求打到 /api/chat", req.get("path", "").endswith("/api/chat"), req.get("path", ""))
    check("system 消息已发送", msgs and msgs[0].get("role") == "system")
    images = msgs[-1].get("images") or []
    check("图片是**裸 base64 数组**（不是 data URI）",
          bool(images) and not images[0].startswith("data:"), f"{len(images)} 张")
    check("参数放在 options 里",
          isinstance(body.get("options"), dict) and "temperature" in body["options"],
          str(body.get("options")))
    check("format=json 已带上", body.get("format") == "json")
    check("think 字段已显式传 false（本机配置默认关闭思考链）",
          body.get("think") is False, str(body.get("think")))
    check("识别结果解析成功", (res.json() or {}).get("questions") is not None)
    check("token 统计来自 Ollama 字段", res.prompt_tokens == 1513 and res.output_tokens == 488,
          f"{res.prompt_tokens}/{res.output_tokens}")

    # 坑 1：答案只在 thinking 字段
    print("\n  坑 1：think=false 时答案落在 thinking、content 为空")
    ollama.empty_content = True
    ollama.clear()
    res2 = client.chat("识别这张图", [img], json_mode=True, timeout=30)
    check("content 为空时仍判为成功", res2.ok, res2.error)
    check("能从 thinking 字段取回 JSON",
          (res2.json() or {}).get("questions") is not None)
    check("thinking 原文被保留（便于排查）", bool(res2.thinking))
    ollama.empty_content = False

    # 坑 2：先回话痨文本，需自动追问重试
    print("\n  坑 2：模型先回非 JSON 文本 → 自动追问“只输出 JSON”")
    cfg2 = make_cfg(ollama.base_url, "ollama", ollama.model)
    pipe = Pipeline(cfg2, on_log=lambda m: None)
    saved = ollama._server.recognize
    ollama._server.recognize = {"subject": "数学", "subject_detail": "线性代数", "grade": "大学",
                                "questions": [{"index": 1, "type": "解答题", "status": "wrong",
                                               "stem": "计算三阶行列式 D 的值", "student_answer": "7",
                                               "correct_answer": "-3",
                                               "knowledge_points": ["行列式"], "confidence": 0.9}]}
    ollama.plain_text = True
    ollama.clear()
    out = pipe.process_image(img, skip_preprocess=True)
    posts = [r for r in ollama.requests if r.get("method") == "POST"]
    check("第一次非 JSON 后自动重试（共 2 次请求）", len(posts) >= 2, f"{len(posts)} 次")
    if len(posts) >= 2:
        retry_txt = json.dumps(posts[-1]["body"], ensure_ascii=False)
        check("重试请求里带上了“只输出 JSON”的追问",
              "JSON" in retry_txt and ("只输出" in retry_txt or "第一个字符" in retry_txt))
    check("重试后拿到结构化结果", out.question_count == 1 and out.route == "local",
          f"{out.question_count} 题 route={out.route}")
    ollama.plain_text = False
    ollama._server.recognize = saved

    # 服务故障：要能报错不崩
    print("\n  服务故障")
    ollama.fail_times = 2
    res3 = client.chat("识别这张图", [img], timeout=30)
    check("Ollama 返回 500 时不抛异常、错误可读", (not res3.ok) and "HTTP 500" in (res3.error or ""),
          (res3.error or "")[:40])
    ollama.fail_times = 0


def test_lmstudio(cloud: MockCloud, img: str) -> None:
    print("\n二、LM Studio / 本地 OpenAI 兼容（local.kind=lmstudio）")
    cfg = make_cfg(cloud.base_url, "lmstudio", "mock-vl")
    client = ModelClient("local", cfg)
    check("服务探活（/v1/models）", client.manager.alive())
    models = client.manager.list_models()
    check("模型列表来自 /v1/models", "mock-vl" in models, "、".join(models))

    cloud.clear()
    res = client.chat("识别这张图", [img], system="你是识别引擎", json_mode=True, timeout=30)
    req = cloud.last_post()
    body = req.get("body", {})
    check("请求打到 /v1/chat/completions", req.get("path", "").endswith("/chat/completions"),
          req.get("path", ""))
    content = body.get("messages", [{}])[-1].get("content")
    imgs = [c for c in content if c.get("type") == "image_url"] if isinstance(content, list) else []
    check("图片以 data:image/jpeg;base64 发送", bool(imgs) and
          imgs[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
    check("识别结果解析成功", (res.json() or {}).get("questions") is not None)

    # 走完整流水线（local_only）
    expected = len(DEFAULT_CLOUD_RECOGNIZE["questions"])
    expected_status = [q["status"] for q in DEFAULT_CLOUD_RECOGNIZE["questions"]]
    pipe = Pipeline(cfg, on_log=lambda m: None)
    out = pipe.process_image(img, skip_preprocess=True)
    check("LM Studio 走完整流水线：route=local", out.route == "local", out.route)
    check(f"识别题数与假服务返回一致（{expected} 道）", out.question_count == expected,
          f"{out.question_count} 道")
    check("每题状态解析正确（含错题/正确/空白）",
          [q["status"] for q in out.questions] == expected_status,
          str([q["status"] for q in out.questions]))
    check("引擎标记为 local:*", str(out.engine).startswith("local:"), out.engine)

    # openai_compatible 同一条代码路径
    cfg2 = make_cfg(cloud.base_url, "openai_compatible", "mock-vl")
    out2 = Pipeline(cfg2, on_log=lambda m: None).process_image(img, skip_preprocess=True)
    check("local.kind=openai_compatible 同样可用", out2.question_count == expected,
          f"{out2.question_count} 道")


def test_local_only_guarantee(ollama: MockOllama, cloud: MockCloud, img: str) -> None:
    print("\n三、纯本地保证（local_only + 云端已配置 → 云端一个请求都不该收到）")
    cfg = make_cfg(ollama.base_url, "ollama", ollama.model,
                   mode="local_only", cloud_base=cloud.base_url)
    check("云端在配置上“可用”（有 Key、已启用）", cfg.cloud_enabled())
    cloud.clear()
    out = Pipeline(cfg, on_log=lambda m: None).process_image(img, skip_preprocess=True)
    cloud_hits = [r for r in cloud.requests if r.get("method") == "POST"]
    check("识别成功且 route=local", out.question_count == 1 and out.route == "local",
          f"route={out.route}")
    check("云端收到 0 个推理请求", len(cloud_hits) == 0, f"{len(cloud_hits)} 个")

    print("\n  对照：换成 hybrid 且本地失败 → 应当自动转云端")
    cfg2 = make_cfg(ollama.base_url, "ollama", ollama.model,
                    mode="hybrid", cloud_base=cloud.base_url)
    ollama.fail_times = 5          # 让本地连续报错
    cloud.clear()
    out2 = Pipeline(cfg2, on_log=lambda m: None).process_image(img, skip_preprocess=True)
    cloud_hits2 = [r for r in cloud.requests if r.get("method") == "POST"]
    check("本地失败后确实调用了云端", len(cloud_hits2) >= 1, f"{len(cloud_hits2)} 个请求")
    # 这里的"云端"是 OpenAI 假服务，默认返回 3 道题（见 mock_cloud_server.DEFAULT_RECOGNIZE）
    check("最终仍拿到结果且 route=local→cloud",
          out2.question_count >= 1 and out2.route == "local→cloud",
          f"{out2.question_count} 题 route={out2.route}")
    check("结果来自云端引擎", str(out2.engine).startswith("cloud:"), out2.engine)
    ollama.fail_times = 0

    print("\n  反向对照：云端挂了、本地正常 → 应当由本地接手（route=cloud→local）")
    cfg3 = make_cfg(ollama.base_url, "ollama", ollama.model,
                    mode="cloud_first", cloud_base=cloud.base_url)
    cloud.fail_times = 5
    out3 = Pipeline(cfg3, on_log=lambda m: None).process_image(img, skip_preprocess=True)
    check("云端失败后本地接手，仍拿到结果",
          out3.question_count >= 1 and out3.route == "cloud→local",
          f"{out3.question_count} 题 route={out3.route}")
    check("结果来自本地引擎", str(out3.engine).startswith("local:"), out3.engine)
    cloud.fail_times = 0


def main() -> int:
    logging_setup.setup()
    tmp_dir = os.path.join(ROOT, "verify", "_backends")
    os.makedirs(tmp_dir, exist_ok=True)
    img = make_image(os.path.join(tmp_dir, "题.png"))

    ollama = MockOllama(port=OLLAMA_PORT).start()
    cloud = MockCloud(port=OPENAI_PORT).start()
    print(f"假 Ollama：{ollama.base_url}　假 OpenAI：{cloud.base_url}")
    try:
        test_ollama(ollama, img)
        test_lmstudio(cloud, img)
        test_local_only_guarantee(ollama, cloud, img)
    finally:
        ollama.stop()
        cloud.stop()
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = [lbl for lbl, ok in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"本地后端适配验证：{passed}/{len(RESULTS)} 项通过")
    for lbl in failed:
        print("  ✗", lbl)
    print(f"RESULT: {'OK' if not failed else 'FAILED'} ({passed}/{len(RESULTS)} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
