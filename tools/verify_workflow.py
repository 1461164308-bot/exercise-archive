# -*- coding: utf-8 -*-
"""全流程验证：把「假云端」当成引擎，跑通 CLI 与 GUI 的完整业务闭环（不需要显卡）。

与其它验证脚本的分工：
- ``verify_cloud.py``    —— 只管云端适配本身（HTTP 协议层）；
- ``verify_workflow.py`` —— 管**业务闭环**：导入 → 识别 → 入库 → 归档 → 错题分析 →
  搜索 → 统计 → 导出 → 备份 → 标签 → 删除，CLI 与图形界面两条入口都走一遍。

为什么能不用显卡：把运行模式设为 ``cloud_first`` 并指向本机的假云端（
``tools/mock_cloud_server.py``），识别与分析就都由假云端返回**固定的、可断言的**数据；
这样验证的是「数据在系统里怎么流动」，与模型算力无关，也不会打扰你打游戏。

用法::

    python tools/verify_workflow.py            # 全部用例
    python tools/verify_workflow.py --keep     # 跑完不删临时数据目录（排查用）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

from mock_cloud_server import MockCloud  # noqa: E402
from gui_smoke import capture_hwnd       # noqa: E402  窗口截图复用同一套 GDI 代码

from exerarchive import config, imageops, logging_setup, paths  # noqa: E402

PORT = 8901
WORK = os.path.join(ROOT, "verify", "_workflow")
RESULTS = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"　{detail}" if detail else ""))
    RESULTS.append((label, bool(cond)))
    return bool(cond)


def write_config(data_dir: str, cloud: MockCloud, mode: str = "cloud_first") -> str:
    """在临时数据目录里写一份「云端指向假服务」的配置。

    注意：配置文件的真实位置是 ``<EXER_HOME>/data/config.json``（``paths.config_path()``），
    不是 ``<EXER_HOME>/config.json``。
    """
    cfg_dir = os.path.join(data_dir, "data")
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "config.json")
    cfg = config.Config({}, path)
    cfg.set("run.mode", mode)
    cfg.set("run.auto_preprocess", True)
    cfg.set("run.max_questions_per_image", 8)
    cfg.set("local.auto_start", False)          # 绝不碰本地模型/显卡
    cfg.set("cloud.enabled", True)
    cfg.set("cloud.profiles", [{
        "name": "本地假云端", "base_url": cloud.base_url, "model": cloud.model,
        "api_key": "sk-mock", "supports_vision": True,
        "timeout": 30, "max_tokens": 2048, "temperature": 0.2,
    }])
    cfg.set("cloud.active", 0)
    cfg.set("archive.copy_original", True)
    cfg.set("privacy.encrypt_db", True)
    cfg.save()
    return path


def child_env(data_dir: str) -> dict:
    """给子进程（CLI 工具）准备环境变量：数据目录 + UTF-8 输出。"""
    env = os.environ.copy()
    env["EXER_HOME"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------------------- #
# 一、命令行入口
# --------------------------------------------------------------------------- #
def test_cli(cloud: MockCloud, data_dir: str) -> None:
    print("\n一、命令行批量导入（tools/import_cli.py，云端走假服务）")
    samples = os.path.join(ROOT, "samples")
    files = [p for p in sorted(os.listdir(samples)) if p.endswith(".jpg")][:2]
    if not files:
        check("样张存在（可先运行 python tools/make_samples.py）", False)
        return
    src_dir = os.path.join(WORK, "cli_input")
    os.makedirs(src_dir, exist_ok=True)
    for name in files:
        shutil.copy2(os.path.join(samples, name), os.path.join(src_dir, name))

    json_out = os.path.join(WORK, "cli_result.json")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "import_cli.py"), src_dir,
         "--force", "cloud", "--json", json_out, "--quiet"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=child_env(data_dir), cwd=ROOT, timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    print("   CLI 输出尾部：", " / ".join(out.strip().splitlines()[-4:])[:300])
    check("CLI 退出码为 0", proc.returncode == 0, f"returncode={proc.returncode}")
    check("生成了 JSON 结果文件", os.path.exists(json_out))
    summary = {}
    if os.path.exists(json_out):
        summary = json.load(open(json_out, encoding="utf-8"))
        n_q = sum(i.get("questions", 0) for i in summary.get("images", []))
        check(f"识别题数正确（{len(files)} 张 × 3 题 = {3 * len(files)}）",
              n_q == 3 * len(files), f"实际 {n_q}")
        check("统计里有错题", summary.get("stats", {}).get("wrong", 0) >= 1,
              f"错题 {summary.get('stats', {}).get('wrong')}")

    # 数据落盘检查（用独立进程读，确保写盘真的成功了）
    from exerarchive.store import Store
    st = Store(os.path.join(data_dir, "data", "archive.db"))
    questions = st.list_questions(limit=100)
    check("题目已入库", len(questions) >= 3, f"{len(questions)} 道")
    check("题干能从加密库解出来（含关键词）",
          any("函数" in (q.get("stem") or "") for q in questions))
    wrong = [q for q in questions if q.get("status") == "wrong"]
    check("错题已标记", len(wrong) >= 1, f"{len(wrong)} 道")
    reports = st.list_reports()
    check("错题分析报告已生成", len(reports) >= 1, f"{len(reports)} 份")
    if reports:
        rep = reports[0]["report"]
        check("报告六段齐全", all(rep.get(k) for k in
                                  ("restate", "causes", "knowledge_points", "solution_steps",
                                   "final_answer", "avoid_tips", "variants")))
        check("报告来源标记为云端", rep.get("depth") == "cloud", str(rep.get("depth")))
    check("搜索能命中题干（加密检索）",
          len(st.search(keyword="极小值")) >= 1)
    check("按状态过滤可用", len(st.search(status="wrong")) >= 1)

    # 归档目录
    archive_root = os.path.join(data_dir, "data", "归档")
    found = []
    for base, _dirs, fs in os.walk(archive_root):
        for f in fs:
            found.append(os.path.join(base, f))
    check("归档目录已生成文件", len(found) >= 4, f"{len(found)} 个文件")
    kinds = {os.path.splitext(f)[1].lower() for f in found}
    check("归档含图片与 Markdown 文字稿", ".jpg" in kinds and ".md" in kinds, str(sorted(kinds)))
    check("归档目录按 学科/学段/题型/状态 分层",
          any("数学" in f and "错题" in f for f in found),
          next((os.path.relpath(f, archive_root) for f in found if "错题" in f), ""))
    st.close()

    # 命令行自检入口
    proc2 = subprocess.run([sys.executable, "-m", "exerarchive", "--selfcheck", "--no-model"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=child_env(data_dir), cwd=ROOT, timeout=300)
    check("python -m exerarchive --selfcheck 能跑通",
          "结论：" in (proc2.stdout or ""), f"returncode={proc2.returncode}")


# --------------------------------------------------------------------------- #
# 二、图形界面入口
# --------------------------------------------------------------------------- #
def test_gui(cloud: MockCloud, data_dir: str) -> None:
    print("\n二、图形界面全流程（导入 → 识别 → 编辑 → 标签 → 导出 → 备份 → 搜索 → 删除）")
    from exerarchive.ui.app import KP_ALL, MainWindow

    app = MainWindow()
    app.geometry("1400x880+10+10")
    app.update()

    samples = os.path.join(ROOT, "samples")
    files = [os.path.join(samples, f) for f in sorted(os.listdir(samples))
             if f.endswith(".jpg")][:1]
    if not files:
        check("样张存在", False)
        app._on_close()
        return

    # 1) 导入
    app._do_import(files)
    app.update()
    check("导入后待识别列表有 1 张", len(app.pending) == 1)
    check("导入记录已入库", app.store.stats()["images"] == 1)

    # 2) 识别（走假云端）
    cloud.clear()
    app._recognize_worker(app.pending[:1])
    app.update()
    app.refresh_all()
    app.update()
    stats = app.store.stats()
    check("识别出 3 道题", stats["questions"] == 3, f"{stats['questions']} 道")
    check("其中 1 道错题", stats["wrong"] == 1, f"{stats['wrong']} 道")
    check("自动生成了 1 份错题分析", stats["reports"] == 1, f"{stats['reports']} 份")
    check("待识别列表状态更新为已识别",
          app.pending[0].get("status") == "已识别", str(app.pending[0].get("status")))
    check("云端确实收到了带图片的请求",
          any("image_url" in json.dumps(r.get("body", {}), ensure_ascii=False)
              for r in cloud.requests if r.get("method") == "POST"))

    # 3) 界面刷新：题库列表 / 统计卡片 / 图表 / 归档树
    check("题库列表有 3 行", len(app.q_tree.get_children()) == 3)
    check("统计卡片显示 3 道题", app.card_questions.value_var.get() == "3")
    check("学科占比图已绘制", len(app.chart_subject.find_all()) >= 1)
    check("薄弱知识点图已绘制", len(app.chart_weak.find_all()) >= 1)
    check("归档目录树已刷新", len(app.archive_tree.get_children()) >= 1)

    # 3a) 知识点分类与筛选：识别出的知识点要进下拉框，选中后题库列表只剩同类题
    kp_names = [it["name"] for it in app.store.knowledge_points()]
    check("识别出的知识点进了题库统计", bool(kp_names), "、".join(kp_names))
    kp_values = list(app.kp_combo.cget("values"))
    real_kps = [v for v in kp_values if v != KP_ALL]
    check("知识点筛选下拉框已填充", bool(real_kps), "、".join(kp_values))
    check("下拉框默认是「全部知识点」", app.filter_knowledge.get() == KP_ALL,
          app.filter_knowledge.get())
    # 知识点列要真的显示出来（不是空列）
    kp_col = [app.q_tree.set(i, "kp") for i in app.q_tree.get_children()]
    check("题库列表的知识点列有内容", any(v and v != "—" for v in kp_col), "、".join(kp_col))
    # 选中第一个真实知识点 → 只剩带这个知识点的题
    if real_kps:
        target = real_kps[0]
        kp_name = app._kp_map[target]
        app.filter_knowledge.set(target)
        app.refresh_questions()
        app.update()
        kept = app.q_tree.get_children()
        all_have = all(kp_name in app.q_tree.set(i, "kp") for i in kept)
        check(f"按知识点「{kp_name}」筛选后只剩同类题（{len(kept)} 道）", bool(kept) and all_have,
              "、".join(app.q_tree.set(i, "kp") for i in kept))
        check("筛选结果确实变少了", len(kept) < 3, f"{len(kept)} / 3")
        # 换学科时，该学科下没有的知识点要从下拉框里消失、选中项回到「全部」
        app.filter_knowledge.set(target)
        app.filter_subject.set("英语")
        app.refresh_questions()
        app.update()
        check("换学科后知识点下拉框跟着收窄",
              all(v == KP_ALL or app._kp_map[v] in
                  [it["name"] for it in app.store.knowledge_points(subject="英语")]
                  for v in app.kp_combo.cget("values")),
              "、".join(app.kp_combo.cget("values")))
        check("选中的知识点在本学科下不存在时回到「全部知识点」",
              app.filter_knowledge.get() == KP_ALL, app.filter_knowledge.get())
        app.filter_subject.set("")
        app.filter_knowledge.set(KP_ALL)
        app.refresh_questions()
        app.update()
        check("切回「全部知识点」后恢复全部题", len(app.q_tree.get_children()) == 3)

    # 预览画布必须真的显示出图片：这个曾经一直是空白（PhotoImage 建不出来），
    # 但界面能开、列表也有数据，不专门断言就发现不了
    check("预览画布已显示图片", app.preview._photo is not None,
          f"{app.preview._photo.width()}x{app.preview._photo.height()}"
          if app.preview._photo else "无")
    app.load_into_editor()
    app.update()
    check("预处理页载入后画布已显示图片", app.editor.canvas._photo is not None,
          f"{app.editor.image.w}x{app.editor.image.h}" if app.editor.image else "未载入")
    app.nb.select(0)
    app.update()

    # 3b) 错题分析页：数学公式必须排版成图片（而不是显示 e^(2πik/6) 这种纯文本）
    wrong_uid = None
    for item in app.q_tree.get_children():
        q = app.store.get_question(item)
        if q and q.get("status") == "wrong":
            wrong_uid = item
            break
    if wrong_uid:
        app.q_tree.selection_set(wrong_uid)
        app.on_question_select()
        app.update()
        photos = getattr(app.report.text, "_photos", [])
        check(f"报告页把公式排成了图片（{len(photos)} 处）", len(photos) >= 5,
              f"共 {len(photos)} 张公式图")
        report_text = app.report.text.get_text()
        check("报告文本里没有残留 LaTeX 源码", "$" not in report_text and "\\frac" not in report_text)
        check("报告文本里公式转成了可读写法",
              any(ch in report_text for ch in ("×", "²", "−", "-2")), report_text[:40])
        # 截图留证（公式在报告页里的实际观感）。
        # 文件名**故意和 gui_smoke 的不一样**：两者都写 verify/gui-错题分析.png 时，
        # 后跑的 gui_smoke（一键验收的第 7 步）会把这张有公式的报告截图覆盖成空页面，
        # 于是"公式在界面里的样子"这份证据每次验收完都刚好丢掉。
        app.nb.select(2)
        app.update_idletasks()
        app.update()
        time.sleep(0.4)
        app.update()
        shot_path = os.path.join(ROOT, "verify", "gui-错题分析-公式.png")
        if capture_hwnd(app.winfo_id(), shot_path, use_screen=False):
            print(f"   已截图（公式排版）：{shot_path}")
        app.nb.select(0)
        app.update()

    # 4) 选中一道题 → 结果编辑区回填 → 修改并保存
    uid = app.q_tree.get_children()[0]
    app.q_tree.selection_set(uid)
    app.on_question_select()
    app.update()
    check("结果编辑区已回填题干", bool(app.field_widgets["stem"].get("1.0", "end").strip()))
    app.field_vars["difficulty"].set("较难") if "difficulty" in app.field_vars else None
    app.tag_editor.add("期中复习")
    app.save_question_edits()
    app.update()
    q = app.store.get_question(uid)
    check("标签已保存", "期中复习" in (q.get("tags") or []), str(q.get("tags")))
    check("按标签能搜到", len(app.store.search(tag="期中复习")) == 1)

    # 5) 搜索页
    app.search_var.set("极小值")
    app.do_search()
    app.update()
    check("搜索命中错题", len(app.search_tree.get_children()) >= 1)
    check("搜索详情区有内容", len(app.search_text.get_text()) > 50)

    # 6) 导出（docx + pdf + txt）
    out_dir = os.path.join(WORK, "gui_exports")
    os.makedirs(out_dir, exist_ok=True)
    for fmt in ("docx", "pdf", "txt", "md", "html"):
        path = os.path.join(out_dir, f"错题本.{fmt}")
        app.exporter.export(path, status="wrong", fmt=fmt, title="个人错题本")
        size = os.path.getsize(path) if os.path.exists(path) else 0
        check(f"导出 {fmt} 成功", size > 500, f"{size / 1024:.1f} KB")
    pdf = os.path.join(out_dir, "错题本.pdf")
    if os.path.exists(pdf):
        with open(pdf, "rb") as f:
            head = f.read(5)
        check("PDF 文件头正确", head.startswith(b"%PDF"), repr(head))
    docx = os.path.join(out_dir, "错题本.docx")
    if os.path.exists(docx):
        import zipfile

        with zipfile.ZipFile(docx) as zf:
            doc = zf.read("word/document.xml").decode("utf-8")
        check("docx 内含报告正文", "错题还原" in doc or "错误原因" in doc)

    # 7) 预置文档
    for name, fn in (("知识点薄弱清单.md", app.exporter.export_weak_list),
                     ("错题汇总.md", app.exporter.export_summary)):
        path = os.path.join(out_dir, name)
        fn(path)
        check(f"预置文档 {name} 生成", os.path.exists(path) and os.path.getsize(path) > 50)

    # 8) 备份与导出 JSON
    backup = app.store.backup_zip(include_images=True)
    check("备份 zip 生成", os.path.exists(backup) and os.path.getsize(backup) > 1000,
          f"{os.path.getsize(backup) / 1024:.1f} KB")
    dump = app.store.export_json()
    check("JSON 全量导出生成", os.path.exists(dump) and os.path.getsize(dump) > 500)

    # 9) 再次识别同一张图（验证重复导入不会覆盖历史）
    before = app.store.stats()["questions"]
    app._do_import(files)
    app.update()
    app._recognize_worker(app.pending[-1:])
    app.update()
    after = app.store.stats()["questions"]
    check("第二次导入同一张图会新增题目（不覆盖）", after == before + 3,
          f"{before} → {after}")

    # 10) 删除
    uids = app.q_tree.get_children()[:1]
    deleted = app.store.delete_questions(list(uids))
    app.refresh_questions()
    check("删除题目生效", deleted == 1 and app.store.stats()["questions"] == after - 1)

    # 11) 图片列表与归档链路
    check("图片表记录完整", app.store.stats()["images"] == 2)

    app._on_close()


def test_error_paths(cloud: MockCloud, data_dir: str) -> None:
    """云端故障 / 模型返回空题目时的表现：不崩、状态可读、能降级。

    这一段专门验证「出问题了会怎样」——真实使用中网络抖动、Key 过期、
    服务商限流都会走到这些分支，界面必须给出可理解的反馈而不是白屏或崩溃。
    """
    print("\n三、异常与降级（云端 500 / 返回空题目 / 分析失败回退规则版）")
    from exerarchive.analysis import Analyzer
    from exerarchive.pipeline import Pipeline
    from exerarchive.store import Store

    os.environ["EXER_HOME"] = data_dir
    samples = os.path.join(ROOT, "samples")
    files = [os.path.join(samples, f) for f in sorted(os.listdir(samples)) if f.endswith(".jpg")]
    if not files:
        check("样张存在", False)
        return

    cfg = config.load_config()
    # 让"本地不可用"这件事确定化：把本地服务指向一个死端口。
    # 否则本机要是碰巧有 Ollama 在跑，云端失败时程序会正常回退到本地模型
    # （这是对的行为，但会让"应当失败"的断言不成立，测试就不再是确定性的）。
    cfg.set("local.base_url", "http://127.0.0.1:1")
    cfg.set("local.auto_start", False)
    store = Store(encrypt=True)
    pipe = Pipeline(cfg, on_log=lambda m: None)

    # 1) 云端 500：识别应当失败但不抛异常
    cloud.fail_times = 3
    out = pipe.process_image(files[0], skip_preprocess=True)
    check("云端 500 时识别返回失败而非异常",
          (not out.questions) and bool(out.error), (out.error or "")[:60])
    check("错误信息可读（含 HTTP 状态或错误类型）",
          any(k in (out.error or "").upper() for k in ("HTTP", "TIMEOUT", "ERROR", "失败")))

    # 2) 恢复后同一张图能识别成功（批量任务不会因为一次失败全废）
    cloud.fail_times = 0
    out2 = pipe.process_image(files[0], skip_preprocess=True)
    check("服务恢复后识别成功", out2.question_count == 3, f"{out2.question_count} 题")

    # 3) 模型返回空题目列表：应当被判定为"没识别出题目"，而不是写脏数据
    cloud.set_recognize({"subject": "数学", "questions": []})
    out3 = pipe.process_image(files[0], skip_preprocess=True)
    check("模型返回空题目 → 判为失败（随后还尝试了本地兜底）",
          (not out3.questions) and bool(out3.error), (out3.error or "")[:40])
    check("失败时不会写入任何题目", out3.question_count == 0)
    cloud.reset_recognize()

    # 4) 错题分析：云端失败 + 本地模型不可用 → 回退到规则版报告，仍要有六段
    cloud.fail_times = 2
    q = dict(out2.questions[0], uid="QTEST-ERR", source_path=files[0])
    q["status"] = "wrong"
    analyzer = Analyzer(cfg, on_log=lambda m: None)
    report = analyzer.analyze(q, allow_cloud=True)
    check("分析降级为本地规则版（depth=rule）", report.get("depth") == "rule",
          str(report.get("depth")))
    check("降级报告仍然六段齐全",
          all(report.get(k) for k in ("restate", "causes", "knowledge_points", "solution_steps",
                                      "final_answer", "avoid_tips", "variants")))
    check("降级报告有来源标注", bool(report.get("engine")), str(report.get("engine")))
    cloud.fail_times = 0

    # 5) 入库 + 归档：失败的那次不应该写出半条记录
    before = store.stats()["questions"]
    cfg_uid = store.add_image(files[0], original_name="err.png")
    for qq in out2.questions:
        store.add_question(dict(qq), store.image_id(cfg_uid), "err.png")
    after = store.stats()["questions"]
    check("只写入成功那次的结果（+3 题）", after == before + 3, f"{before} → {after}")
    check("失败的识别没有留下空题目记录",
          all((qq.get("stem") or "").strip() for qq in store.list_questions(limit=50)))
    store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="全流程验证（无需显卡）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    args = ap.parse_args()

    logging_setup.setup()
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    data_dir = os.path.join(WORK, "exerdata")
    os.makedirs(data_dir, exist_ok=True)

    cloud = MockCloud(port=PORT).start()
    print(f"假云端已启动：{cloud.base_url}")
    write_config(data_dir, cloud)
    os.environ["EXER_HOME"] = data_dir          # 当前进程（GUI）也用这份临时数据
    os.environ["PYTHONIOENCODING"] = "utf-8"

    try:
        test_cli(cloud, data_dir)
        # GUI 部分换一个干净的数据目录，避免与 CLI 用例相互影响
        gui_dir = os.path.join(WORK, "exerdata_gui")
        os.makedirs(gui_dir, exist_ok=True)
        write_config(gui_dir, cloud)
        os.environ["EXER_HOME"] = gui_dir
        test_gui(cloud, gui_dir)
        # 异常与降级再换一个目录
        err_dir = os.path.join(WORK, "exerdata_err")
        os.makedirs(err_dir, exist_ok=True)
        write_config(err_dir, cloud)
        test_error_paths(cloud, err_dir)
    finally:
        cloud.stop()

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok in RESULTS if ok)
    failed = [label for label, ok in RESULTS if not ok]
    print(f"全流程验证：{passed}/{len(RESULTS)} 项通过")
    if failed:
        print("失败项：")
        for label in failed:
            print("  ✗", label)
    print(f"RESULT: {'OK' if not failed else 'FAILED'} ({passed}/{len(RESULTS)} checks)")
    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
