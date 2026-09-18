# -*- coding: utf-8 -*-
"""数据层：SQLite 本地库 + 字段级加密 + 加密全文检索 + 备份/恢复。

设计
----
- **单文件数据库** ``data/archive.db``（WAL 模式，写入快、掉电不易坏）。
- **字段级加密**：题干、选项、学生作答、标准答案、错题报告等正文用
  :mod:`exerarchive.crypto` 的 ChaCha20+HMAC 加密后以 BLOB 存；学科/题型/状态这类
  用于统计与建目录的「分类字段」保持明文（它们本身不含隐私内容）。
- **加密全文检索**：正文不存明文，但仍要支持关键词搜索 —— 做法是把文本切成
  字符 3-gram，用 HMAC 指纹写进 FTS5 索引表，查询词同样算指纹去匹配，
  命中后再解密逐条校验，因此「库里没有明文，也能搜到」。
- **一键备份/恢复**：整个 data 目录打包成 zip，或者只导出 JSON。

表结构见 :data:`SCHEMA`。所有对外方法都返回普通 dict，方便 GUI/导出模块直接用。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from typing import Dict, Iterable, List, Optional, Sequence

from . import paths
from .crypto import get_cipher
from .logging_setup import get_logger

log = get_logger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT UNIQUE,
    path          TEXT,              -- 原图（导入时复制进 data/images）
    work_path     TEXT,              -- 预处理后的图
    original_name TEXT,
    sha1          TEXT,
    subject       TEXT,
    subject_detail TEXT,
    grade         TEXT,
    status        TEXT,              -- 该图片下的汇总状态（有错题=wrong）
    quality       REAL,
    quality_json  TEXT,
    engine        TEXT,
    route         TEXT,
    preprocess    TEXT,
    error         TEXT,
    source        TEXT,              -- 试卷/练习册来源（自定义标签）
    batch         TEXT,              -- 导入批次
    note_enc      BLOB,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT UNIQUE,       -- 题号，如 Q20260918-0007
    image_id      INTEGER,
    idx           INTEGER DEFAULT 1,
    subject       TEXT,
    subject_detail TEXT,
    grade         TEXT,
    qtype         TEXT,
    status        TEXT,              -- wrong / correct / blank
    difficulty    TEXT,
    knowledge     TEXT,              -- 明文 JSON（知识点名称，用于分类统计）
    confidence    REAL,
    engine        TEXT,
    stem_enc      BLOB,
    options_enc   BLOB,
    answer_enc    BLOB,              -- 学生作答
    steps_enc     BLOB,              -- 学生步骤
    correct_enc   BLOB,              -- 参考答案
    source_image  TEXT,
    archived      INTEGER DEFAULT 0,
    archive_path  TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id   INTEGER UNIQUE,
    uid           TEXT,
    depth         TEXT,              -- local（本地基础版）/ cloud（云端深度版）
    engine        TEXT,
    cause_json    TEXT,              -- 明文：错误原因类型列表（不含题干，便于统计）
    kp_json       TEXT,              -- 明文：知识点名称列表
    body_enc      BLOB,              -- 加密：完整报告 JSON
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT,              -- question / image
    ref_id        INTEGER,
    tag           TEXT,
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_q_image  ON questions(image_id);
CREATE INDEX IF NOT EXISTS idx_q_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_q_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_q_type   ON questions(qtype);
CREATE INDEX IF NOT EXISTS idx_tag_ref  ON tags(kind, ref_id);

-- 加密正文的 3-gram 指纹索引（FTS5 全文检索；内容不可逆，不泄露原文）
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    uid UNINDEXED,
    kind UNINDEXED,
    tokens
);
"""


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """数据仓库（线程安全：内部一把可重入锁 + 每次调用独立游标）。"""

    def __init__(self, db_path=None, encrypt: Optional[bool] = None):
        self.path = str(db_path or paths.db_path())
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        if encrypt is None:
            from .config import load_config

            try:
                encrypt = bool(load_config().get("privacy.encrypt_db", True))
            except Exception:
                encrypt = True
        self.encrypt = bool(encrypt)
        self.cipher = get_cipher() if self.encrypt else None

    # ------------------------------------------------------------------ #
    # 底层工具
    # ------------------------------------------------------------------ #
    def _exec(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _enc(self, text) -> bytes:
        """正文入库：加密开启时返回密文，关闭时返回 UTF-8 明文（字段名不变，切换开关不会读不出数据）。"""
        raw = ("" if text is None else str(text)).encode("utf-8")
        if not self.encrypt:
            return raw
        return self.cipher.encrypt_bytes(raw)

    def _dec(self, blob) -> str:
        if blob is None:
            return ""
        raw = bytes(blob)
        if not raw:
            return ""
        if not self.encrypt:
            return raw.decode("utf-8", errors="replace")
        return self.cipher.decrypt_text(raw)

    def _tokens(self, text: str, sizes=(2, 3)) -> List[str]:
        """统一的 n-gram 指纹计算（加密开启走 HMAC 指纹，关闭走明文 n-gram）。"""
        if self.cipher:
            return self.cipher.index_tokens(text, sizes)
        import re as _re

        t = _re.sub(r"\s+", "", str(text or ""))
        out = set()
        for n in sizes:
            if len(t) >= n:
                out.update(t[i : i + n] for i in range(len(t) - n + 1))
        if not out and t:
            out.add(t)
        return sorted(out)

    def _index(self, uid: str, kind: str, parts: Iterable[str]) -> None:
        """写入/更新检索指纹。"""
        text = " ".join(str(p) for p in parts if p)
        tokens = self._tokens(text, (2, 3))
        self._exec("DELETE FROM search_index WHERE uid=? AND kind=?", (uid, kind))
        self._exec(
            "INSERT INTO search_index(uid, kind, tokens) VALUES(?,?,?)",
            (uid, kind, " ".join(tokens)),
        )

    def _next_uid(self, table: str, prefix: str) -> str:
        """生成可读编号：Q20260918-0007 / IMG20260918-0012。"""
        day = _dt.datetime.now().strftime("%Y%m%d")
        like = f"{prefix}{day}-%"
        rows = self._query(f"SELECT uid FROM {table} WHERE uid LIKE ? ORDER BY uid DESC LIMIT 1", (like,))
        seq = 1
        if rows:
            try:
                seq = int(str(rows[0]["uid"]).split("-")[-1]) + 1
            except Exception:
                seq = len(self._query(f"SELECT id FROM {table}")) + 1
        return f"{prefix}{day}-{seq:04d}"

    # ------------------------------------------------------------------ #
    # 图片
    # ------------------------------------------------------------------ #
    def add_image(self, path: str, work_path: str = "", original_name: str = "",
                  source: str = "", batch: str = "", note: str = "") -> str:
        uid = self._next_uid("images", "IMG")
        sha1 = ""
        try:
            h = hashlib.sha1()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha1 = h.hexdigest()
        except Exception:
            pass
        ts = now_str()
        self._exec(
            """INSERT INTO images(uid, path, work_path, original_name, sha1, source, batch,
               note_enc, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (uid, str(path), str(work_path or ""), original_name or os.path.basename(str(path)),
             sha1, source, batch, self._enc(note), ts, ts),
        )
        return uid

    def image_id(self, uid: str) -> int:
        rows = self._query("SELECT id FROM images WHERE uid=?", (uid,))
        return int(rows[0]["id"]) if rows else 0

    def find_image_by_sha1(self, sha1: str) -> Optional[sqlite3.Row]:
        if not sha1:
            return None
        rows = self._query("SELECT * FROM images WHERE sha1=? LIMIT 1", (sha1,))
        return rows[0] if rows else None

    def update_image(self, uid: str, patch: Dict) -> None:
        if not patch:
            return
        patch = dict(patch)
        patch["updated_at"] = now_str()
        cols = []
        vals = []
        for k, v in patch.items():
            if k == "note":
                cols.append("note_enc=?")
                vals.append(self._enc(v))
            else:
                cols.append(f"{k}=?")
                vals.append(v)
        vals.append(uid)
        self._exec(f"UPDATE images SET {', '.join(cols)} WHERE uid=?", vals)

    def get_image(self, uid: str) -> Optional[Dict]:
        rows = self._query("SELECT * FROM images WHERE uid=?", (uid,))
        if not rows:
            return None
        d = dict(rows[0])
        d["note"] = self._dec(d.pop("note_enc", None))
        return d

    def list_images(self, limit: int = 500, offset: int = 0, batch: str = "") -> List[Dict]:
        sql = "SELECT * FROM images"
        params: List = []
        if batch:
            sql += " WHERE batch=?"
            params.append(batch)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        out = []
        for r in self._query(sql, params):
            d = dict(r)
            d["note"] = self._dec(d.pop("note_enc", None))
            d["question_count"] = self._query(
                "SELECT COUNT(*) AS c FROM questions WHERE image_id=?", (r["id"],)
            )[0]["c"]
            out.append(d)
        return out

    def delete_images(self, uids: Sequence[str], delete_files: bool = True) -> int:
        n = 0
        for uid in uids:
            rows = self._query("SELECT * FROM images WHERE uid=?", (uid,))
            if not rows:
                continue
            img = rows[0]
            qids = [r["uid"] for r in self._query("SELECT uid FROM questions WHERE image_id=?", (img["id"],))]
            for qid in qids:
                self._delete_question_row(qid)
            self._exec("DELETE FROM search_index WHERE uid=?", (uid,))
            self._exec("DELETE FROM images WHERE id=?", (img["id"],))
            if delete_files:
                for key in ("path", "work_path"):
                    p = img[key] if key in img.keys() else ""
                    if p and os.path.exists(p) and str(p).startswith(str(paths.data_dir())):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
            n += 1
        return n

    # ------------------------------------------------------------------ #
    # 题目
    # ------------------------------------------------------------------ #
    def add_question(self, q: Dict, image_id: int = 0, source_image: str = "") -> str:
        uid = q.get("uid") or self._next_uid("questions", "Q")
        ts = now_str()
        knowledge = q.get("knowledge_points") or q.get("knowledge") or []
        self._exec(
            """INSERT INTO questions(uid, image_id, idx, subject, subject_detail, grade, qtype, status,
               difficulty, knowledge, confidence, engine, stem_enc, options_enc, answer_enc, steps_enc,
               correct_enc, source_image, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uid, image_id or q.get("image_id") or 0, int(q.get("index", 1) or 1),
                q.get("subject", ""), q.get("subject_detail", ""), q.get("grade", ""),
                q.get("qtype") or q.get("type", ""), q.get("status", ""),
                q.get("difficulty", ""), json.dumps(knowledge, ensure_ascii=False),
                float(q.get("confidence", 0) or 0), q.get("engine", ""),
                self._enc(q.get("stem", "")), self._enc("\n".join(q.get("options") or [])),
                self._enc(q.get("student_answer", "")), self._enc(q.get("student_steps", "")),
                self._enc(q.get("correct_answer", "")), source_image or q.get("source_image", ""),
                ts, ts,
            ),
        )
        if not self.encrypt:
            # 不加密时正文存在 *_enc 字段里是明文，检索指纹仍统一维护
            pass
        self._index(uid, "question", [
            q.get("stem", ""), " ".join(q.get("options") or []), q.get("student_answer", ""),
            q.get("correct_answer", ""), " ".join(knowledge) if isinstance(knowledge, list) else "",
            q.get("subject_detail", ""), q.get("qtype", ""),
        ])
        return uid

    def _row_to_question(self, r: sqlite3.Row) -> Dict:
        d = dict(r)
        d["stem"] = self._dec(d.get("stem_enc"))
        opts = self._dec(d.get("options_enc"))
        d["options"] = [line for line in opts.split("\n") if line.strip()] if opts else []
        d["student_answer"] = self._dec(d.get("answer_enc"))
        d["student_steps"] = self._dec(d.get("steps_enc"))
        d["correct_answer"] = self._dec(d.get("correct_enc"))
        try:
            d["knowledge_points"] = json.loads(d.get("knowledge") or "[]")
        except Exception:
            d["knowledge_points"] = []
        for k in ("stem_enc", "options_enc", "answer_enc", "steps_enc", "correct_enc"):
            d.pop(k, None)
        d["type"] = d.get("qtype", "")
        d["index"] = d.get("idx", 1)
        return d

    # 查询题目时联表带出图片路径：GUI 预览、归档、导出都要用它拿原图/预处理图
    _Q_SELECT = (
        "SELECT q.*, i.path AS image_path, i.work_path AS work_path, "
        "i.original_name AS original_name, i.source AS source, i.path AS original_path "
        "FROM questions q LEFT JOIN images i ON i.id = q.image_id"
    )

    def list_questions(self, limit: int = 500, offset: int = 0, image_id: Optional[int] = None,
                       status: str = "", subject: str = "", qtype: str = "", grade: str = "",
                       order: str = "q.id DESC") -> List[Dict]:
        sql = self._Q_SELECT + " WHERE 1=1"
        params: List = []
        if image_id:
            sql += " AND q.image_id=?"
            params.append(image_id)
        for col, val in (("status", status), ("subject", subject), ("qtype", qtype), ("grade", grade)):
            if val:
                sql += f" AND q.{col}=?"
                params.append(val)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._row_to_question(r) for r in self._query(sql, params)]

    def get_question(self, uid: str) -> Optional[Dict]:
        rows = self._query(self._Q_SELECT + " WHERE q.uid=?", (uid,))
        if not rows:
            return None
        q = self._row_to_question(rows[0])
        q["tags"] = [r["tag"] for r in self._query(
            "SELECT tag FROM tags WHERE kind='question' AND ref_id=?", (rows[0]["id"],))]
        rep = self.get_report(uid)
        if rep:
            q["report"] = rep
        return q

    def update_question(self, uid: str, patch: Dict) -> None:
        """更新题目（stem/status/学科等），同步维护检索指纹。"""
        rows = self._query("SELECT * FROM questions WHERE uid=?", (uid,))
        if not rows:
            return
        cur = self._row_to_question(rows[0])
        cur.update(patch)
        knowledge = cur.get("knowledge_points") or []
        self._exec(
            """UPDATE questions SET idx=?, subject=?, subject_detail=?, grade=?, qtype=?, status=?,
               difficulty=?, knowledge=?, confidence=?, stem_enc=?, options_enc=?, answer_enc=?,
               steps_enc=?, correct_enc=?, updated_at=? WHERE uid=?""",
            (
                int(cur.get("idx") or cur.get("index") or 1), cur.get("subject", ""),
                cur.get("subject_detail", ""), cur.get("grade", ""), cur.get("qtype", ""),
                cur.get("status", ""), cur.get("difficulty", ""),
                json.dumps(knowledge, ensure_ascii=False), float(cur.get("confidence") or 0),
                self._enc(cur.get("stem", "")), self._enc("\n".join(cur.get("options") or [])),
                self._enc(cur.get("student_answer", "")), self._enc(cur.get("student_steps", "")),
                self._enc(cur.get("correct_answer", "")), now_str(), uid,
            ),
        )
        self._index(uid, "question", [
            cur.get("stem", ""), " ".join(cur.get("options") or []), cur.get("student_answer", ""),
            cur.get("correct_answer", ""), " ".join(knowledge) if isinstance(knowledge, list) else "",
            cur.get("subject_detail", ""), cur.get("qtype", ""),
        ])

    def _delete_question_row(self, uid: str) -> None:
        rows = self._query("SELECT id FROM questions WHERE uid=?", (uid,))
        if rows:
            self._exec("DELETE FROM reports WHERE question_id=?", (rows[0]["id"],))
            self._exec("DELETE FROM tags WHERE kind='question' AND ref_id=?", (rows[0]["id"],))
        self._exec("DELETE FROM questions WHERE uid=?", (uid,))
        self._exec("DELETE FROM search_index WHERE uid=?", (uid,))

    def delete_questions(self, uids: Sequence[str]) -> int:
        n = 0
        for uid in uids:
            if self._query("SELECT id FROM questions WHERE uid=?", (uid,)):
                self._delete_question_row(uid)
                n += 1
        return n

    # ------------------------------------------------------------------ #
    # 标签 / 来源
    # ------------------------------------------------------------------ #
    def add_tag(self, kind: str, ref_id: int, tag: str) -> None:
        tag = (tag or "").strip()
        if not tag:
            return
        exists = self._query(
            "SELECT id FROM tags WHERE kind=? AND ref_id=? AND tag=?", (kind, ref_id, tag)
        )
        if exists:
            return
        self._exec(
            "INSERT INTO tags(kind, ref_id, tag, created_at) VALUES(?,?,?,?)",
            (kind, ref_id, tag, now_str()),
        )

    def remove_tag(self, kind: str, ref_id: int, tag: str) -> None:
        self._exec("DELETE FROM tags WHERE kind=? AND ref_id=? AND tag=?", (kind, ref_id, tag))

    def question_id(self, uid: str) -> int:
        rows = self._query("SELECT id FROM questions WHERE uid=?", (uid,))
        return int(rows[0]["id"]) if rows else 0

    def image_uid_of_question(self, question_uid: str) -> str:
        """由题号反查它所在图片的 uid（界面同步「预处理结果」时用）。"""
        rows = self._query(
            "SELECT i.uid AS iuid FROM questions q LEFT JOIN images i ON i.id=q.image_id WHERE q.uid=?",
            (question_uid,),
        )
        return str(rows[0]["iuid"] or "") if rows and rows[0]["iuid"] else ""

    def all_tags(self, kind: str = "question") -> List[str]:
        return sorted({r["tag"] for r in self._query("SELECT tag FROM tags WHERE kind=?", (kind,))})

    # ------------------------------------------------------------------ #
    # 错题报告
    # ------------------------------------------------------------------ #
    def save_report(self, uid: str, report: Dict, depth: str = "local", engine: str = "") -> None:
        qid = self.question_id(uid)
        if not qid:
            return
        causes = [c.get("type", "") for c in report.get("causes", []) if isinstance(c, dict)]
        kps = [k.get("name", "") if isinstance(k, dict) else str(k)
               for k in report.get("knowledge_points", [])]
        ts = now_str()
        exists = self._query("SELECT id FROM reports WHERE question_id=?", (qid,))
        body = self._enc(json.dumps(report, ensure_ascii=False))
        if exists:
            self._exec(
                """UPDATE reports SET depth=?, engine=?, cause_json=?, kp_json=?, body_enc=?, updated_at=?
                   WHERE question_id=?""",
                (depth, engine, json.dumps(causes, ensure_ascii=False),
                 json.dumps(kps, ensure_ascii=False), body, ts, qid),
            )
        else:
            self._exec(
                """INSERT INTO reports(question_id, uid, depth, engine, cause_json, kp_json, body_enc,
                   created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (qid, uid, depth, engine, json.dumps(causes, ensure_ascii=False),
                 json.dumps(kps, ensure_ascii=False), body, ts, ts),
            )
        self._index(uid, "report", [json.dumps(report, ensure_ascii=False)[:4000]])

    def get_report(self, uid: str) -> Optional[Dict]:
        rows = self._query(
            "SELECT r.*, q.uid AS quid FROM reports r LEFT JOIN questions q ON q.id=r.question_id WHERE r.uid=?",
            (uid,),
        )
        if not rows:
            return None
        r = dict(rows[0])
        body = r.get("body_enc")
        report = {}
        if body is not None and len(bytes(body)):
            try:
                raw = bytes(body)
                text = raw.decode("utf-8", "replace") if not self.encrypt else self.cipher.decrypt_text(raw)
                report = json.loads(text)
            except Exception as exc:
                log.warning("报告解密失败（%s）：%s", uid, exc)
                report = {}
        report["_depth"] = r.get("depth", "")
        report["_engine"] = r.get("engine", "")
        report["_updated_at"] = r.get("updated_at", "")
        report["_causes"] = json.loads(r.get("cause_json") or "[]")
        report["_knowledge"] = json.loads(r.get("kp_json") or "[]")
        return report

    def list_reports(self, limit: int = 500) -> List[Dict]:
        out = []
        for uid in [r["uid"] for r in self._query(
                "SELECT uid FROM reports ORDER BY updated_at DESC LIMIT ?", (limit,))]:
            rep = self.get_report(uid)
            q = self.get_question(uid)
            if rep and q:
                out.append({"uid": uid, "question": q, "report": rep})
        return out

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def search(self, keyword: str = "", subject: str = "", status: str = "", qtype: str = "",
               grade: str = "", tag: str = "", limit: int = 300) -> List[Dict]:
        """全局搜索：关键词（加密字段也能搜）+ 学科/状态/题型/年级/标签过滤。"""
        uids: Optional[List[str]] = None
        terms = [t for t in str(keyword or "").replace("　", " ").split() if t]
        scan_all = False  # 单字查询无法用 n-gram 索引，退回全表解密校验

        if terms:
            cand: Optional[set] = None
            for term in terms:
                if len(term) < 2:
                    scan_all = True
                    continue
                sizes = (2,) if len(term) == 2 else (3,)
                tokens = self._tokens(term, sizes)
                if not tokens:
                    continue
                # FTS5：空格分隔即 AND
                q = " ".join(tokens)
                try:
                    rows = self._query(
                        "SELECT DISTINCT uid FROM search_index WHERE tokens MATCH ?", (q,)
                    )
                except sqlite3.OperationalError:
                    rows = self._query("SELECT DISTINCT uid FROM search_index WHERE tokens LIKE ?",
                                       (f"%{q}%",))
                found = {r["uid"] for r in rows}
                cand = found if cand is None else (cand & found)
            if not scan_all:
                uids = sorted(cand or set())

        sql = self._Q_SELECT + " WHERE 1=1"
        params: List = []
        for col, val in (("subject", subject), ("status", status), ("qtype", qtype), ("grade", grade)):
            if val:
                sql += f" AND q.{col}=?"
                params.append(val)
        if uids is not None:
            if not uids:
                return []
            marks = ",".join("?" * len(uids))
            sql += f" AND q.uid IN ({marks})"
            params += uids
        if tag:
            sql += " AND q.id IN (SELECT ref_id FROM tags WHERE kind='question' AND tag=?)"
            params.append(tag)
        sql += " ORDER BY q.id DESC LIMIT ?"
        # 单字查询需要在内存里逐条解密校验，先多取一些候选再截断
        params.append(max(limit, 3000) if scan_all else limit)

        rows = self._query(sql, params)
        out = [self._row_to_question(r) for r in rows]
        # 指纹命中后必须逐条校验（n-gram 会误命中），保证关键词确实在正文里；
        # 校验范围包含错题报告正文，所以"搜报告里的内容"也能搜到题。
        if terms:
            report_text = self._report_texts([q["uid"] for q in out])
            verified = []
            for q in out:
                blob = " ".join([
                    q.get("stem", ""), " ".join(q.get("options") or []),
                    q.get("student_answer", ""), q.get("correct_answer", ""),
                    q.get("student_steps", ""), " ".join(q.get("knowledge_points") or []),
                    q.get("subject_detail", ""), q.get("qtype", ""), q.get("uid", ""),
                    report_text.get(q["uid"], ""),
                ]).lower()
                if all(t.lower() in blob for t in terms):
                    verified.append(q)
            out = verified[:limit]
        return out

    def _report_texts(self, uids: Sequence[str]) -> Dict[str, str]:
        """批量取报告正文（已解密），供搜索校验使用。"""
        if not uids:
            return {}
        marks = ",".join("?" * len(uids))
        out: Dict[str, str] = {}
        for r in self._query(f"SELECT uid, body_enc FROM reports WHERE uid IN ({marks})", list(uids)):
            body = r["body_enc"]
            if body is None or not len(bytes(body)):
                continue
            try:
                raw = bytes(body)
                out[r["uid"]] = raw.decode("utf-8", "replace") if not self.encrypt else self.cipher.decrypt_text(raw)
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def stats(self) -> Dict:
        def one(sql: str, params: Sequence = ()):
            rows = self._query(sql, params)
            return rows[0][0] if rows else 0

        total = one("SELECT COUNT(*) FROM questions")
        by_status = {r["status"] or "unknown": r["c"] for r in self._query(
            "SELECT status, COUNT(*) AS c FROM questions GROUP BY status")}
        by_subject = {r["subject"] or "未分类": r["c"] for r in self._query(
            "SELECT subject, COUNT(*) AS c FROM questions GROUP BY subject ORDER BY c DESC")}
        by_type = {r["qtype"] or "未分类": r["c"] for r in self._query(
            "SELECT qtype, COUNT(*) AS c FROM questions GROUP BY qtype ORDER BY c DESC")}
        by_grade = {r["grade"] or "未分类": r["c"] for r in self._query(
            "SELECT grade, COUNT(*) AS c FROM questions GROUP BY grade ORDER BY c DESC")}
        # 薄弱知识点：错题里出现次数最多的知识点
        weak: Dict[str, int] = {}
        for r in self._query("SELECT knowledge FROM questions WHERE status='wrong'"):
            try:
                for kp in json.loads(r["knowledge"] or "[]"):
                    weak[str(kp)] = weak.get(str(kp), 0) + 1
            except Exception:
                continue
        causes: Dict[str, int] = {}
        for r in self._query("SELECT cause_json FROM reports"):
            try:
                for c in json.loads(r["cause_json"] or "[]"):
                    causes[str(c)] = causes.get(str(c), 0) + 1
            except Exception:
                continue
        return {
            "images": one("SELECT COUNT(*) FROM images"),
            "questions": total,
            "reports": one("SELECT COUNT(*) FROM reports"),
            "wrong": by_status.get("wrong", 0),
            "correct": by_status.get("correct", 0),
            "blank": by_status.get("blank", 0),
            "by_status": by_status,
            "by_subject": by_subject,
            "by_type": by_type,
            "by_grade": by_grade,
            "weak_knowledge": dict(sorted(weak.items(), key=lambda kv: -kv[1])[:30]),
            "cause_stats": dict(sorted(causes.items(), key=lambda kv: -kv[1])[:20]),
        }

    def wrong_questions(self, limit: int = 1000) -> List[Dict]:
        return [self.get_question(r["uid"]) or {} for r in self._query(
            "SELECT uid FROM questions WHERE status='wrong' ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ #
    # 备份 / 恢复 / 清空
    # ------------------------------------------------------------------ #
    def export_json(self, path=None) -> str:
        """把所有数据导出成 JSON（正文已解密，便于迁移与二次加工）。"""
        data = {
            "exported_at": now_str(),
            "images": self.list_images(limit=100000),
            "questions": [
                self.get_question(q["uid"]) for q in self.list_questions(limit=100000)
            ],
            "stats": self.stats(),
        }
        text = json.dumps(data, ensure_ascii=False, indent=1)
        out = str(path or (paths.backup_dir() / f"export_{_dt.datetime.now():%Y%m%d_%H%M%S}.json"))
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        return out

    def backup_zip(self, zip_path=None, include_images: bool = True) -> str:
        """打包 data 目录（数据库 + 归档 + 可选图片）为一键备份 zip。"""
        zip_path = str(zip_path or (paths.backup_dir() / f"backup_{_dt.datetime.now():%Y%m%d_%H%M%S}.zip"))
        os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
        root = paths.data_dir()
        skip_dirs = set()
        if not include_images:
            skip_dirs = {"images", "preprocessed", "归档", "thumbs"}
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in skip_dirs and d != "backup"]
                for name in files:
                    full = os.path.join(folder, name)
                    try:
                        zf.write(full, os.path.relpath(full, root))
                        count += 1
                    except Exception as exc:
                        log.warning("备份跳过 %s：%s", full, exc)
        log.info("备份完成：%s（%d 个文件）", zip_path, count)
        return zip_path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except Exception:
                pass
            self._conn.close()

    def clear_all(self, keep_files: bool = False) -> None:
        """清空所有数据（危险操作，界面上要二次确认）。"""
        with self._lock:
            for table in ("search_index", "reports", "tags", "questions", "images"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()
            self._conn.execute("VACUUM")
            self._conn.commit()
        if not keep_files:
            for d in (paths.images_dir(), paths.preprocessed_dir(), paths.thumbs_dir()):
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    d.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
