# -*- coding: utf-8 -*-
"""P0 能力：事件自动聚类。

把碎片帖子自动聚成"事件"：TF-IDF + jieba 纯本地聚类（无 AI 可跑），
有 AI 时给事件起标题、写一句话摘要。

事件持久化到 monitor.db 的 events / event_notes 表（本模块首次调用时
自行建表，不改 db.py）。发布时间用北京时间分桶做声量曲线。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import ai_chat, config, db

logger = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    topic_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    sentiment TEXT NOT NULL DEFAULT '{}',
    volume TEXT NOT NULL DEFAULT '[]',
    note_count INTEGER NOT NULL DEFAULT 0,
    start_time INTEGER,
    end_time INTEGER,
    ai_mode INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_notes (
    event_id INTEGER NOT NULL,
    user_id INTEGER,
    post_id TEXT NOT NULL,
    PRIMARY KEY (event_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic_id);
CREATE INDEX IF NOT EXISTS idx_event_notes_event ON event_notes(event_id);
"""

NOTE_COLS = ("post_id, title, desc, author, publish_time, liked_count,"
             " comment_count, sentiment, note_url, tags, source_keyword")


def _db_path() -> str:
    return str(config.DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables() -> None:
    config.ensure_dirs()
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)
        for _t in ("events", "event_notes"):
            _cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_t})").fetchall()]
            if "user_id" not in _cols:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN user_id INTEGER")


# ---------------- 分词 / TF-IDF ----------------

# 领域词：从话题关键词动态加载（通用化，不 hardcode 品牌词）
_jieba_ready = False


def _load_domain_words() -> None:
    """从所有话题的关键词里提取领域词，加入 jieba 自定义词典。"""
    global _jieba_ready
    if _jieba_ready:
        return
    try:
        import jieba
        from . import db
        words = set()
        for t in db.list_topics():
            for kw in (t.get("keywords") or "").replace("，", ",").split(","):
                kw = kw.strip()
                if len(kw) >= 2:
                    words.add(kw)
                    # 长词也拆出 2-4 字的子串作为候选
                    for i in range(len(kw)):
                        for j in range(i + 2, min(i + 5, len(kw) + 1)):
                            words.add(kw[i:j])
        for w in words:
            jieba.add_word(w)
    except Exception:  # noqa: BLE001
        pass
    _jieba_ready = True


def _tokenize(text: str) -> List[str]:
    try:
        import jieba
    except Exception:  # noqa: BLE001
        return []
    _load_domain_words()
    out = []
    for w in jieba.cut(text or ""):
        w = w.strip()
        if len(w) < 2 or w.isdigit():
            continue
        out.append(w)
    return out


def _tfidf_vectors(docs: List[List[str]]) -> Tuple[List[Dict[str, float]], List[float], Dict[str, float]]:
    n = len(docs)
    df = Counter()
    for toks in docs:
        df.update(set(toks))
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vecs, norms = [], []
    for toks in docs:
        tf = Counter(toks)
        total = len(toks) or 1
        v = {t: (c / total) * idf[t] for t, c in tf.items()}
        vecs.append(v)
        norms.append(math.sqrt(sum(x * x for x in v.values())) or 1.0)
    return vecs, norms, idf


def _cluster(vecs: List[Dict[str, float]], norms: List[float],
             threshold: float) -> List[List[int]]:
    """并查集聚类。倒排索引预过滤：无公共词的帖子对余弦必为 0，直接跳过。"""
    n = len(vecs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    posting: Dict[str, List[int]] = defaultdict(list)
    for i, v in enumerate(vecs):
        for t in v:
            posting[t].append(i)
    seen = set()
    for lst in posting.values():
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                a, b = lst[x], lst[y]
                if a > b:
                    a, b = b, a
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                va, vb = vecs[a], vecs[b]
                dot = sum(va[t] * vb[t] for t in va.keys() & vb.keys())
                if dot >= threshold * norms[a] * norms[b]:
                    union(a, b)
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _top_keywords(docs: List[List[str]], idf: Dict[str, float],
                  top_n: int = 6) -> List[str]:
    agg = Counter()
    for toks in docs:
        tf = Counter(toks)
        total = len(toks) or 1
        for t, c in tf.items():
            agg[t] += (c / total) * idf.get(t, 1.0)
    return [w for w, _ in agg.most_common(top_n)]


def _volume_curve(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for m in members:
        d = datetime.fromtimestamp(m["publish_time"], TZ).strftime("%Y-%m-%d")
        buckets[d][0] += 1
        if m.get("sentiment") == "negative":
            buckets[d][1] += 1
    return [{"date": d, "count": c, "negative": ng}
            for d, (c, ng) in sorted(buckets.items())]


def _engagement(m: Dict[str, Any]) -> int:
    return (m.get("liked_count") or 0) + (m.get("comment_count") or 0) * 3


# ---------------- AI 标题/摘要（可选） ----------------

def _ai_title_summary(keywords: List[str],
                      members: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """有 AI 时给事件起标题、写一句话摘要；失败返回 (None, "") 走规则标题。"""
    samples = [(m.get("title") or "(无标题)")[:30] for m in members[:6]]
    prompt = (
        "你是舆情分析师。根据以下同一事件的帖子标题和关键词，给事件起一个"
        "不超过20字的标题，并写一句话摘要（不超过60字）。"
        "只返回 JSON：{\"title\": \"...\", \"summary\": \"...\"}\n"
        f"关键词：{'、'.join(keywords[:6])}\n帖子标题：\n"
        + "\n".join(f"- {s}" for s in samples)
    )
    try:
        provider = ai_chat._mode_cache.get("provider")
        client, p = ai_chat._openai_client(provider)
        resp = client.chat.completions.create(
            model=p["model"],
            messages=[
                {"role": "system", "content": "你是舆情分析师，只返回 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        content = (resp.choices[0].message.content or "").strip()
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None, ""
        data = json.loads(content[start:end + 1])
        title = str(data.get("title") or "").strip()[:30]
        summary = str(data.get("summary") or "").strip()[:120]
        return (title or None), summary
    except Exception as e:  # noqa: BLE001
        logger.warning("事件 AI 命名失败：%s", type(e).__name__)
        return None, ""


# ---------------- 数据读取 ----------------

def _fetch_notes(topic_id: int, since: int) -> List[Dict[str, Any]]:
    """取时间窗内有发布时间的帖子（无发布时间的不参与聚类，声量曲线无法落位）。"""
    uw_sql, uw_params = db.uid_sql()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {NOTE_COLS} FROM notes"
            " WHERE topic_id=? AND publish_time IS NOT NULL AND publish_time>=?"
            f"{uw_sql}"
            " ORDER BY publish_time DESC LIMIT 2000",
            (topic_id, since, *uw_params),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- 对外接口 ----------------

def generate_events(topic_id: int, days: int = 14, min_cluster: int = 3,
                    sim_threshold: float = 0.28, use_ai: bool = True,
                    max_events: int = 30) -> Dict[str, Any]:
    """手动触发：对指定话题近 days 天帖子做事件聚类，结果替换该话题旧事件。"""
    _ensure_tables()
    if not db.get_topic(int(topic_id)):
        return {"ok": False, "error": "话题不存在"}
    since = int((datetime.now(TZ) - timedelta(days=days)).timestamp())
    notes = _fetch_notes(int(topic_id), since)
    if not notes:
        return {"ok": True, "events": 0, "notes": 0,
                "message": f"近 {days} 天无可聚类帖子（需有发布时间）"}

    docs = [_tokenize(f"{n.get('title') or ''} {n.get('desc') or ''}")
            for n in notes]
    pairs = [(n, d) for n, d in zip(notes, docs) if d]
    skipped = len(notes) - len(pairs)
    if not pairs:
        return {"ok": True, "events": 0, "notes": 0,
                "message": "帖子均无有效文本可聚类"}

    vecs, norms, idf = _tfidf_vectors([d for _, d in pairs])
    groups = [g for g in _cluster(vecs, norms, sim_threshold)
              if len(g) >= min_cluster]
    groups.sort(key=len, reverse=True)
    groups = groups[:max_events]

    ai_ok = False
    if use_ai:
        try:
            mode, _ = ai_chat.check_ai_ready()
            ai_ok = (mode == "ai")
        except Exception:  # noqa: BLE001
            ai_ok = False

    built = []
    for g in groups:
        members = [pairs[i][0] for i in g]
        member_docs = [pairs[i][1] for i in g]
        kw = _top_keywords(member_docs, idf)
        title = "、".join(kw[:3]) or "未命名事件"
        summary, ai_mode = "", 0
        if ai_ok:
            t, s = _ai_title_summary(kw, members)
            if t:
                title, summary, ai_mode = t, s, 1
        senti = Counter(m.get("sentiment") or "neutral" for m in members)
        times = [m["publish_time"] for m in members]
        built.append({
            "title": title, "summary": summary, "keywords": kw,
            "sentiment": {k: senti.get(k, 0)
                          for k in ("negative", "neutral", "positive")},
            "volume": _volume_curve(members),
            "note_count": len(members),
            "note_ids": [m["post_id"] for m in members],
            "start_time": min(times), "end_time": max(times),
            "ai_mode": ai_mode,
        })

    now = int(time.time())
    with _lock, _connect() as conn:
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM events WHERE topic_id=?", (int(topic_id),)).fetchall()]
        if old_ids:
            q = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM event_notes WHERE event_id IN ({q})", old_ids)
            conn.execute("DELETE FROM events WHERE topic_id=?", (int(topic_id),))
        for e in built:
            cur = conn.execute(
                "INSERT INTO events (user_id, topic_id, title, summary, keywords, sentiment,"
                " volume, note_count, start_time, end_time, ai_mode, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (current_user_id(), int(topic_id), e["title"], e["summary"],
                 json.dumps(e["keywords"], ensure_ascii=False),
                 json.dumps(e["sentiment"], ensure_ascii=False),
                 json.dumps(e["volume"], ensure_ascii=False),
                 e["note_count"], e["start_time"], e["end_time"],
                 e["ai_mode"], now),
            )
            eid = cur.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO event_notes (user_id, event_id, post_id) VALUES (?,?,?)",
                [(current_user_id(), eid, nid) for nid in e["note_ids"]],
            )
    msg = f"近 {days} 天 {len(notes)} 条帖子，聚出 {len(built)} 个事件"
    if skipped:
        msg += f"（{skipped} 条无有效文本未参与）"
    if ai_ok:
        msg += "，已用 AI 命名"
    return {"ok": True, "events": len(built), "notes": len(notes), "message": msg}


def list_events(topic_id: Optional[int] = None, page: int = 1,
                page_size: int = 20) -> Dict[str, Any]:
    _ensure_tables()
    _w, _p = [], []
    db.apply_uid_filter(_w, _p)
    if topic_id:
        _w.append("topic_id=?"); _p.append(int(topic_id))
    where, params = ((f"WHERE {' AND '.join(_w)}", _p) if _w else ("", []))
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT id, topic_id, title, summary, keywords, sentiment, note_count,"
            f" start_time, end_time, ai_mode, created_at FROM events {where}"
            " ORDER BY note_count DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["keywords"] = json.loads(d["keywords"] or "[]")
            d["sentiment"] = json.loads(d["sentiment"] or "{}")
            items.append(d)
        return {"total": total, "page": page, "page_size": page_size, "items": items}


def get_event(event_id: int) -> Optional[Dict[str, Any]]:
    """事件详情：声量曲线、情感分布、关键词、时间范围、关键帖子（按互动排序 Top8）。"""
    _ensure_tables()
    uw_w, uw_p = [], []
    db.apply_uid_filter(uw_w, uw_p)
    uw_and = f" AND {' AND '.join(uw_w)}" if uw_w else ""
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM events WHERE id=?{uw_and}", (event_id, *uw_p)).fetchone()
        if not row:
            return None
        ev = dict(row)
        ev["keywords"] = json.loads(ev["keywords"] or "[]")
        ev["sentiment"] = json.loads(ev["sentiment"] or "{}")
        ev["volume"] = json.loads(ev["volume"] or "[]")
        note_ids = [r["post_id"] for r in conn.execute(
            f"SELECT post_id FROM event_notes WHERE event_id=?{uw_and}", (event_id, *uw_p)).fetchall()]
        notes: List[Dict[str, Any]] = []
        if note_ids:
            q = ",".join("?" * len(note_ids))
            notes = [dict(r) for r in conn.execute(
                "SELECT post_id, title, author, publish_time, liked_count,"
                f" comment_count, sentiment, note_url FROM notes WHERE post_id IN ({q})",
                note_ids,
            ).fetchall()]
        notes.sort(key=_engagement, reverse=True)
        ev["key_notes"] = notes[:8]
        ev["all_note_count"] = len(note_ids)
        return ev


def delete_event(event_id: int) -> bool:
    _ensure_tables()
    if get_event(event_id) is None:
        return False
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM event_notes WHERE event_id=?", (event_id,))
        cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        return cur.rowcount > 0


def run_event_clustering_job(topic_id: Optional[int] = None,
                             days: int = 14) -> Dict[str, Any]:
    """定时任务入口：对指定话题（或全部话题）跑事件聚类。供 scheduler.py 调用。"""
    _ensure_tables()
    tids = [int(topic_id)] if topic_id else [t["id"] for t in db.list_topics()]
    items = []
    for tid in tids:
        try:
            r = generate_events(tid, days=days)
            items.append({"topic_id": tid, **r})
        except Exception as e:  # noqa: BLE001
            logger.exception("事件聚类任务失败 topic=%s", tid)
            items.append({"topic_id": tid, "ok": False, "error": str(e)})
    return {"items": items}
