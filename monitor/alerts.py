# -*- coding: utf-8 -*-
"""分级预警 + 站外推送（P0 能力）。

舆情的价值在于"第一时间知道"：每次采集任务完成后自动跑规则引擎，
命中即生成预警记录；红色预警经企业微信 webhook / 邮件实时推送。

设计要点：
- 规则可配置：负面声量突增 / 单帖互动超阈值 / 负面率超阈值 / 关键词命中
- 三级预警：红(red) / 橙(orange) / 黄(yellow)
- 表结构自包含（本模块自建表，不改 monitor/db.py）
- 推送配置存 data/alert_config.json（本地文件，不硬编码密钥）
- 同一规则+话题在冷却期内不重复触发
"""
from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import threading
import time
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DATA_DIR, DB_PATH, ensure_dirs
from .db import apply_uid_filter, current_user_id

logger = logging.getLogger("xhs-monitor.alerts")

_lock = threading.Lock()

ALERT_CONFIG_PATH = DATA_DIR / "alert_config.json"

# 预警级别：级别 -> (中文名, 排序权重)
LEVELS = {
    "red": ("红色", 3),
    "orange": ("橙色", 2),
    "yellow": ("黄色", 1),
}

# 规则类型 -> (中文名, 默认级别)
RULE_KINDS = {
    "neg_spike": ("负面声量突增", "red"),
    "hot_post": ("高热负面单帖", "orange"),
    "neg_ratio": ("负面率超阈值", "orange"),
    "keyword": ("敏感词命中", "yellow"),
    # 清博逆向：三层综合预警——关键词+情感+声量突增三层全触发才预警
    "triple": ("三层综合预警", "red"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    level TEXT NOT NULL DEFAULT 'yellow',
    params_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    rule_id INTEGER NOT NULL,
    rule_name TEXT DEFAULT '',
    level TEXT NOT NULL DEFAULT 'yellow',
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    topic_id INTEGER,
    topic_name TEXT DEFAULT '',
    task_id INTEGER,
    post_ids_json TEXT DEFAULT '[]',
    link TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unread',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
CREATE TABLE IF NOT EXISTS alert_push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    alert_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    target TEXT DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_log_alert ON alert_push_log(alert_id);
"""

DEFAULT_RULES = [
    {
        "name": "负面声量突增",
        "kind": "neg_spike",
        "level": "red",
        "params": {
            "baseline_days": 7,
            "multiplier": 2.0,
            "min_absolute": 5,
            "escalate_multiple": 5.0,
        },
    },
    {
        "name": "高热负面单帖",
        "kind": "hot_post",
        "level": "orange",
        "params": {
            "engagement_threshold": 1000,
            "max_notes": 5,
        },
    },
    {
        "name": "负面率超阈值",
        "kind": "neg_ratio",
        "level": "orange",
        "params": {
            "ratio_threshold": 0.3,
            "min_total": 10,
        },
    },
    {
        "name": "敏感词命中",
        "kind": "keyword",
        "level": "yellow",
        "params": {
            "keywords": ["维权", "投诉", "避雷", "跑路", "差评"],
            "max_notes": 10,
        },
    },
    {
        # 清博逆向：三层综合预警（关键词+情感+声量突增，三层全触发）
        "name": "三层综合预警",
        "kind": "triple",
        "level": "red",
        "params": {
            "keywords": ["维权", "投诉", "避雷", "跑路", "差评", "垃圾", "恶心"],
            "neg_ratio_threshold": 0.2,
            "min_total": 10,
            "baseline_days": 7,
            "spike_multiplier": 2.0,
            "min_absolute": 5,
            "max_notes": 5,
        },
    },
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表 + 预置默认规则（幂等）。"""
    ensure_dirs()
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_post_ids(conn)
        for _t in ("alert_rules", "alerts", "alert_push_log"):
            _cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_t})").fetchall()]
            if "user_id" not in _cols:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN user_id INTEGER")
        n = conn.execute("SELECT COUNT(*) c FROM alert_rules").fetchone()["c"]
        if n == 0:
            now = int(time.time())
            for r in DEFAULT_RULES:
                conn.execute(
                    "INSERT INTO alert_rules(name,kind,enabled,level,params_json,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (r["name"], r["kind"], 1, r["level"],
                     json.dumps(r["params"], ensure_ascii=False), now, now),
                )
    logger.info("alerts 表初始化完成")


def _migrate_post_ids(conn: sqlite3.Connection) -> None:
    """历史列 note_ids_json 统一为 post_ids_json，与 notes.post_id 一致。"""
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    except Exception:
        return
    if not cols:
        return
    if "post_ids_json" not in cols and "note_ids_json" in cols:
        conn.execute("ALTER TABLE alerts RENAME COLUMN note_ids_json TO post_ids_json")
        return
    if "post_ids_json" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN post_ids_json TEXT DEFAULT '[]'")
        cols.add("post_ids_json")
    if "note_ids_json" in cols and "post_ids_json" in cols:
        conn.execute(
            "UPDATE alerts SET post_ids_json=note_ids_json"
            " WHERE (post_ids_json IS NULL OR post_ids_json='' OR post_ids_json='[]')"
            " AND note_ids_json IS NOT NULL AND note_ids_json NOT IN ('', '[]')"
        )


# ---------------- 推送配置 ----------------

def _default_push_config() -> Dict[str, Any]:
    return {
        "wecom": {"enabled": False, "webhook": ""},
        "smtp": {
            "enabled": False, "host": "", "port": 587, "use_ssl": False,
            "user": "", "password": "", "from": "", "to": "",
        },
        "cooldown_sec": 3600,
    }


def get_push_config() -> Dict[str, Any]:
    ensure_dirs()
    cfg = _default_push_config()
    try:
        if ALERT_CONFIG_PATH.exists():
            saved = json.loads(ALERT_CONFIG_PATH.read_text(encoding="utf-8"))
            for k in ("wecom", "smtp"):
                if isinstance(saved.get(k), dict):
                    cfg[k].update(saved[k])
            if isinstance(saved.get("cooldown_sec"), int):
                cfg["cooldown_sec"] = saved["cooldown_sec"]
    except Exception as e:
        logger.warning("读取推送配置失败: %s", e)
    return cfg


def get_push_config_masked() -> Dict[str, Any]:
    """给前端展示：密钥类字段脱敏。"""
    cfg = get_push_config()
    masked = json.loads(json.dumps(cfg))
    if masked["wecom"].get("webhook"):
        masked["wecom"]["webhook"] = "***已配置***"
    if masked["smtp"].get("password"):
        masked["smtp"]["password"] = "***已配置***"
    return masked


def save_push_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """保存推送配置。空字符串/脱敏占位不覆盖已有密钥。"""
    cfg = get_push_config()
    for k in ("wecom", "smtp"):
        sub = patch.get(k)
        if not isinstance(sub, dict):
            continue
        for fk, fv in sub.items():
            if fk in ("webhook", "password") and (not fv or fv == "***已配置***"):
                continue  # 不覆盖已有密钥
            cfg[k][fk] = fv
    if isinstance(patch.get("cooldown_sec"), int) and patch["cooldown_sec"] >= 0:
        cfg["cooldown_sec"] = patch["cooldown_sec"]
    ensure_dirs()
    ALERT_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_push_config_masked()


# ---------------- 规则 CRUD ----------------

def _row_to_rule(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except Exception:
        d["params"] = {}
    d["kind_name"] = RULE_KINDS.get(d["kind"], (d["kind"], ""))[0]
    d["level_name"] = LEVELS.get(d["level"], (d["level"], 0))[0]
    return d


def list_rules() -> List[Dict[str, Any]]:
    init_db()
    where, params = [], []
    apply_uid_filter(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _lock, _connect() as conn:
        rows = conn.execute(f"SELECT * FROM alert_rules {clause} ORDER BY id", params).fetchall()
    return [_row_to_rule(r) for r in rows]


def save_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新规则。rule 可含 id（更新）或不含（新增）。"""
    init_db()
    kind = rule.get("kind", "")
    if kind not in RULE_KINDS:
        raise ValueError(f"未知规则类型: {kind}")
    level = rule.get("level", RULE_KINDS[kind][1])
    if level not in LEVELS:
        raise ValueError(f"未知预警级别: {level}")
    params = rule.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params 必须为对象")
    now = int(time.time())
    with _lock, _connect() as conn:
        uw_w, uw_p = [], []
        apply_uid_filter(uw_w, uw_p)
        uw_clause = f" AND {' AND '.join(uw_w)}" if uw_w else ""
        if rule.get("id"):
            conn.execute(
                "UPDATE alert_rules SET name=?,kind=?,enabled=?,level=?,params_json=?,"
                f"updated_at=? WHERE id=?{uw_clause}",
                (rule.get("name") or RULE_KINDS[kind][0], kind,
                 1 if rule.get("enabled", True) else 0, level,
                 json.dumps(params, ensure_ascii=False), now, int(rule["id"]), *uw_p),
            )
            rid = int(rule["id"])
        else:
            cur = conn.execute(
                "INSERT INTO alert_rules(user_id,name,kind,enabled,level,params_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (current_user_id(), rule.get("name") or RULE_KINDS[kind][0], kind,
                 1 if rule.get("enabled", True) else 0, level,
                 json.dumps(params, ensure_ascii=False), now, now),
            )
            rid = cur.lastrowid
        row = conn.execute(
            f"SELECT * FROM alert_rules WHERE id=?{uw_clause}", (rid, *uw_p)).fetchone()
    if not row:
        raise ValueError("规则不存在或无权访问")
    return _row_to_rule(row)


def delete_rule(rule_id: int) -> bool:
    init_db()
    where, params = ["id=?"], [rule_id]
    apply_uid_filter(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM alert_rules WHERE {' AND '.join(where)}", params)
    return cur.rowcount > 0


def set_rule_enabled(rule_id: int, enabled: bool) -> bool:
    init_db()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE alert_rules SET enabled=?,updated_at=? WHERE id=?",
            (1 if enabled else 0, int(time.time()), rule_id),
        )
    return cur.rowcount > 0


# ---------------- 预警记录 ----------------

def _row_to_alert(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    raw = d.pop("post_ids_json", None)
    if raw is None:
        raw = d.pop("note_ids_json", "[]")
    else:
        d.pop("note_ids_json", None)
    try:
        d["post_ids"] = json.loads(raw or "[]")
    except Exception:
        d["post_ids"] = []
    d["level_name"] = LEVELS.get(d["level"], (d["level"], 0))[0]
    return d


def list_alerts(status: Optional[str] = None, level: Optional[str] = None,
                limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    init_db()
    cond, args = [], []
    apply_uid_filter(cond, args)
    if status:
        cond.append("status=?")
        args.append(status)
    if level:
        cond.append("level=?")
        args.append(level)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    with _lock, _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM alerts {where}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM alerts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return {"total": total, "items": [_row_to_alert(r) for r in rows]}


def get_alert(alert_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    uw_w, uw_p = [], []
    apply_uid_filter(uw_w, uw_p)
    uw_clause = f" AND {' AND '.join(uw_w)}" if uw_w else ""
    with _lock, _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM alerts WHERE id=?{uw_clause}", (alert_id, *uw_p)).fetchone()
        if not row:
            return None
        alert = _row_to_alert(row)
        post_ids = alert.get("post_ids") or []
        notes = []
        if post_ids:
            ph = ",".join("?" for _ in post_ids)
            nrows = conn.execute(
                f"SELECT post_id,title,author,liked_count,collected_count,"
                f"comment_count,share_count,sentiment,note_url,publish_time,post_status"
                f" FROM notes WHERE post_id IN ({ph})",
                post_ids,
            ).fetchall()
            notes = [dict(n) for n in nrows]
        alert["notes"] = notes
        logs = conn.execute(
            "SELECT * FROM alert_push_log WHERE alert_id=? ORDER BY created_at DESC",
            (alert_id,),
        ).fetchall()
        alert["push_logs"] = [dict(l) for l in logs]
    return alert


def set_alert_status(alert_id: int, status: str) -> bool:
    if get_alert(alert_id) is None:
        return False
    if status not in ("unread", "read", "handled"):
        raise ValueError(f"未知状态: {status}")
    init_db()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE alerts SET status=?,updated_at=? WHERE id=?",
            (status, int(time.time()), alert_id),
        )
    return cur.rowcount > 0


def unread_count() -> int:
    init_db()
    uw_w, uw_p = [], []
    apply_uid_filter(uw_w, uw_p)
    uw_clause = f" AND {' AND '.join(uw_w)}" if uw_w else ""
    with _lock, _connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) c FROM alerts WHERE status='unread'{uw_clause}", uw_p).fetchone()["c"]


def _create_alert(conn: sqlite3.Connection, rule: Dict[str, Any], level: str,
                  title: str, summary: str, topic_id: Optional[int],
                  topic_name: str, task_id: Optional[int],
                  post_ids: List[str], link: str) -> int:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO alerts(user_id,rule_id,rule_name,level,title,summary,topic_id,"
        "topic_name,task_id,post_ids_json,link,status,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule.get("user_id"), rule["id"], rule["name"], level, title, summary, topic_id, topic_name,
         task_id, json.dumps(post_ids, ensure_ascii=False), link,
         "unread", now, now),
    )
    return cur.lastrowid


def create_alert(level: str, title: str, message: str,
                 topic_id: Optional[int] = None) -> int:
    """系统告警（无规则，如签名漂移检测）。level: critical/high/medium。"""
    init_db()
    import time as _time
    now = int(_time.time())
    with _lock, _connect() as conn:
        # 系统规则占位（rule_id=0）
        cur = conn.execute(
            "INSERT INTO alerts(rule_id,rule_name,level,title,summary,topic_id,"
            "topic_name,task_id,post_ids_json,link,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (0, "系统", level, title, message, topic_id, "",
             None, "[]", "", "unread", now, now))
        conn.commit()
        return cur.lastrowid


# ---------------- 规则引擎 ----------------

def _task_window_notes(conn: sqlite3.Connection, topic_id: int,
                       start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """本轮采集入库的帖子（按采集时间落在任务窗口内）。"""
    rows = conn.execute(
        "SELECT post_id,title,desc,author,liked_count,collected_count,"
        "comment_count,share_count,sentiment,note_url,publish_time,collected_at"
        " FROM notes WHERE topic_id=? AND collected_at>=? AND collected_at<=?",
        (topic_id, start_ts, end_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def _baseline_daily_avg(conn: sqlite3.Connection, topic_id: int,
                        days: int) -> float:
    """基线：过去 N 天按发布时间的日均负面帖子数（无数据按 0 计）。"""
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        "SELECT CAST(publish_time/86400 AS INTEGER) d, COUNT(*) c FROM notes"
        " WHERE topic_id=? AND sentiment='negative' AND publish_time>=?"
        " GROUP BY d",
        (topic_id, since),
    ).fetchall()
    total = sum(r["c"] for r in rows)
    return total / days if days else 0.0


def _in_cooldown(conn: sqlite3.Connection, rule_id: int, topic_id: int,
                 cooldown_sec: int) -> bool:
    row = conn.execute(
        "SELECT created_at FROM alerts WHERE rule_id=? AND topic_id=?"
        " ORDER BY created_at DESC LIMIT 1",
        (rule_id, topic_id),
    ).fetchone()
    return bool(row and (int(time.time()) - row["created_at"] < cooldown_sec))


def _eval_neg_spike(rule: Dict[str, Any], notes: List[Dict[str, Any]],
                    conn: sqlite3.Connection,
                    topic_id: int) -> Optional[Tuple[str, str, str, List[str], str]]:
    p = rule["params"]
    neg = [n for n in notes if n.get("sentiment") == "negative"]
    baseline = _baseline_daily_avg(conn, topic_id, int(p.get("baseline_days", 7)))
    mult = float(p.get("multiplier", 2.0))
    min_abs = int(p.get("min_absolute", 5))
    if not (len(neg) >= min_abs and len(neg) >= baseline * mult):
        return None
    level = rule["level"]
    esc = float(p.get("escalate_multiple", 5.0))
    if baseline > 0 and len(neg) / baseline >= esc:
        level = "red"
    top = sorted(neg, key=lambda n: (n.get("liked_count", 0)
                                     + n.get("comment_count", 0)),
                 reverse=True)[:3]
    ratio = (len(neg) / baseline) if baseline else 0
    title = f"负面声量突增：本轮 {len(neg)} 条（基线日均 {baseline:.1f}）"
    summary = (f"本轮采集到负面帖子 {len(neg)} 条，达到基线日均 {baseline:.1f} 条的"
               f"{ratio:.1f} 倍（阈值 {mult} 倍，且不少于 {min_abs} 条）。")
    link = (top[0].get("note_url") or "") if top else ""
    return level, title, summary, [n["post_id"] for n in top], link


def _eval_hot_post(rule: Dict[str, Any],
                   notes: List[Dict[str, Any]]) -> Optional[Tuple[str, str, str, List[str], str]]:
    p = rule["params"]
    th = int(p.get("engagement_threshold", 1000))
    cands = []
    for n in notes:
        if n.get("sentiment") != "negative":
            continue
        eng = (n.get("liked_count", 0) or 0) + (n.get("collected_count", 0) or 0) \
            + (n.get("comment_count", 0) or 0) + (n.get("share_count", 0) or 0)
        if eng >= th:
            cands.append((eng, n))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    cands = cands[: int(p.get("max_notes", 5))]
    eng, top = cands[0]
    title = f"高热负面单帖：{top.get('title') or '（无标题）'}"[:60]
    summary = (f"「{top.get('title') or '（无标题）'}」作者 {top.get('author') or '-'}，"
               f"互动量 {eng}（点赞{top.get('liked_count', 0)}/"
               f"收藏{top.get('collected_count', 0)}/"
               f"评论{top.get('comment_count', 0)}/分享{top.get('share_count', 0)}），"
               f"超过阈值 {th}。")
    return (rule["level"], title, summary,
            [n["post_id"] for _, n in cands], top.get("note_url") or "")


def _eval_neg_ratio(rule: Dict[str, Any],
                    notes: List[Dict[str, Any]]) -> Optional[Tuple[str, str, str, List[str], str]]:
    p = rule["params"]
    if len(notes) < int(p.get("min_total", 10)):
        return None
    neg = [n for n in notes if n.get("sentiment") == "negative"]
    ratio = len(neg) / len(notes)
    if ratio < float(p.get("ratio_threshold", 0.3)):
        return None
    top = sorted(neg, key=lambda n: (n.get("liked_count", 0)
                                     + n.get("comment_count", 0)),
                 reverse=True)[:3]
    th = float(p.get("ratio_threshold", 0.3))
    title = f"负面率超阈值：{ratio:.0%}（{len(neg)}/{len(notes)}）"
    summary = (f"本轮采集 {len(notes)} 条帖子，其中负面 {len(neg)} 条，"
               f"占比 {ratio:.1%}，超过阈值 {th:.0%}。")
    link = (top[0].get("note_url") or "") if top else ""
    return rule["level"], title, summary, [n["post_id"] for n in top], link


def _eval_keyword(rule: Dict[str, Any],
                  notes: List[Dict[str, Any]]) -> Optional[Tuple[str, str, str, List[str], str]]:
    kws = [k for k in (rule["params"].get("keywords") or []) if k]
    if not kws:
        return None
    hits = []
    for n in notes:
        if n.get("sentiment") != "negative":
            continue
        text = f"{n.get('title') or ''} {n.get('desc') or ''}"
        matched = [k for k in kws if k in text]
        if matched:
            hits.append((n, matched))
    if not hits:
        return None
    hits = hits[: int(rule["params"].get("max_notes", 10))]
    words = sorted({w for _, ms in hits for w in ms})
    title = f"敏感词命中：{len(hits)} 条负面帖含「{'/'.join(words[:4])}」"
    first_title = (hits[0][0].get("title") or "")[:40]
    summary = (f"{len(hits)} 条负面帖子命中敏感词（{'、'.join(words)}），"
               f"首条：「{first_title}」。")
    return (rule["level"], title, summary,
            [n["post_id"] for n, _ in hits],
            hits[0][0].get("note_url") or "")


def _eval_triple(rule: Dict[str, Any], notes: List[Dict[str, Any]],
                conn: sqlite3.Connection,
                topic_id: int) -> Optional[Tuple[str, str, str, List[str], str]]:
    """清博逆向：三层综合预警。

    第一层：关键词匹配（帖子含敏感词）
    第二层：情感阈值（负面率超阈值）
    第三层：声量突增（负面帖子数超基线倍数）
    三层全部触发才生成红色预警，降低单层误报。
    """
    p = rule["params"]
    # 第一层：关键词匹配
    kws = [k for k in (p.get("keywords") or []) if k]
    kw_hits = []
    for n in notes:
        text = f"{n.get('title') or ''} {n.get('desc') or ''}"
        if any(k in text for k in kws):
            kw_hits.append(n)
    if not kw_hits:
        return None
    # 第二层：情感阈值
    if len(notes) < int(p.get("min_total", 10)):
        return None
    neg = [n for n in notes if n.get("sentiment") == "negative"]
    ratio = len(neg) / len(notes)
    if ratio < float(p.get("neg_ratio_threshold", 0.2)):
        return None
    # 第三层：声量突增
    baseline = _baseline_daily_avg(conn, topic_id, int(p.get("baseline_days", 7)))
    mult = float(p.get("spike_multiplier", 2.0))
    min_abs = int(p.get("min_absolute", 5))
    if not (len(neg) >= min_abs and len(neg) >= baseline * mult):
        return None
    # 三层全触发
    top = sorted(kw_hits, key=lambda n: (n.get("liked_count", 0)
                                         + n.get("comment_count", 0)),
                 reverse=True)[: int(p.get("max_notes", 5))]
    words = sorted({w for n in kw_hits for w in kws
                    if w in f"{n.get('title') or ''} {n.get('desc') or ''}"})
    title = f"三层综合预警：关键词+负面{len(neg)}条+声量突增"
    summary = (f"三层条件全部触发："
               f"①{len(kw_hits)} 条帖子命中敏感词（{'/'.join(words[:4])}）；"
               f"②负面率 {ratio:.0%}（阈值 {float(p.get('neg_ratio_threshold', 0.2)):.0%}）；"
               f"③负面帖子 {len(neg)} 条，达基线日均 {baseline:.1f} 条的"
               f"{(len(neg) / baseline if baseline else 0):.1f} 倍（阈值 {mult} 倍）。")
    link = (top[0].get("note_url") or "") if top else ""
    return ("red", title, summary, [n["post_id"] for n in top], link)


def _evaluate_rule(rule: Dict[str, Any], notes: List[Dict[str, Any]],
                   conn: sqlite3.Connection,
                   topic_id: int) -> Optional[Tuple[str, str, str, List[str], str]]:
    kind = rule["kind"]
    if kind == "neg_spike":
        return _eval_neg_spike(rule, notes, conn, topic_id)
    if kind == "hot_post":
        return _eval_hot_post(rule, notes)
    if kind == "neg_ratio":
        return _eval_neg_ratio(rule, notes)
    if kind == "keyword":
        return _eval_keyword(rule, notes)
    if kind == "triple":
        return _eval_triple(rule, notes, conn, topic_id)
    return None


def evaluate_after_task(task_id: int) -> List[Dict[str, Any]]:
    """采集任务完成后调用：跑全部启用规则，命中则生成预警；红色自动推送。

    返回新生成的预警列表。主构建者请在任务 status 变为 done 后调用本函数。
    """
    from . import db as dbmod
    init_db()
    task = dbmod.get_task(task_id)
    if not task:
        logger.warning("任务不存在: %s", task_id)
        return []
    topic_id = task.get("topic_id")
    if not topic_id:
        return []
    topic = dbmod.get_topic(topic_id) or {}
    topic_name = topic.get("name", "")
    start_ts = task.get("started_at") or (int(time.time()) - 86400)
    end_ts = task.get("finished_at") or int(time.time())

    cfg = get_push_config()
    cooldown = int(cfg.get("cooldown_sec", 3600))
    created: List[Dict[str, Any]] = []

    # 注意：list_rules() 内部会取 _lock，不可与下面的持锁区嵌套（threading.Lock 不可重入）
    rules = [r for r in list_rules() if r["enabled"]]
    with _lock, _connect() as conn:
        notes = _task_window_notes(conn, topic_id, start_ts, end_ts)
        if not notes:
            logger.info("任务 %s 本轮无新增帖子，跳过预警评估", task_id)
            return []
        for rule in rules:
            if _in_cooldown(conn, rule["id"], topic_id, cooldown):
                logger.info("规则 %s 在冷却期内，跳过", rule["name"])
                continue
            try:
                hit = _evaluate_rule(rule, notes, conn, topic_id)
            except Exception as e:
                logger.exception("规则 %s 评估异常: %s", rule["name"], e)
                continue
            if not hit:
                continue
            level, title, summary, post_ids, link = hit
            aid = _create_alert(conn, rule, level, title, summary, topic_id,
                                topic_name, task_id, post_ids, link)
            alert = {"id": aid, "rule_id": rule["id"], "rule_name": rule["name"],
                     "level": level, "title": title, "summary": summary,
                     "topic_id": topic_id, "topic_name": topic_name,
                     "task_id": task_id, "post_ids": post_ids, "link": link,
                     "status": "unread"}
            created.append(alert)
            logger.info("生成预警 [%s] %s", level, title)
            if level == "red":
                _push_red(conn, alert, cfg)
    return created


# ---------------- 站外推送 ----------------

def _log_push(conn: sqlite3.Connection, alert_id: int, channel: str,
              target: str, ok: bool, detail: str) -> None:
    conn.execute(
        "INSERT INTO alert_push_log(alert_id,channel,target,status,detail,"
        "created_at) VALUES(?,?,?,?,?,?)",
        (alert_id, channel, target, "ok" if ok else "failed",
         detail[:2000], int(time.time())),
    )


def _push_red(conn: sqlite3.Connection, alert: Dict[str, Any],
              cfg: Dict[str, Any]) -> None:
    """红色预警自动推送：企业微信 webhook + 邮件（按配置启用情况）。"""
    wecom = cfg.get("wecom", {})
    if wecom.get("enabled") and wecom.get("webhook"):
        ok, detail = push_wecom(wecom["webhook"], alert)
        _log_push(conn, alert["id"], "wecom",
                  _mask_url(wecom["webhook"]), ok, detail)
    smtp = cfg.get("smtp", {})
    if smtp.get("enabled") and smtp.get("host"):
        ok, detail = push_email(smtp, alert)
        _log_push(conn, alert["id"], "email", smtp.get("to", ""), ok, detail)


def _mask_url(url: str) -> str:
    if len(url) <= 24:
        return "***"
    return url[:16] + "..." + url[-8:]


def _alert_markdown(alert: Dict[str, Any]) -> str:
    level_name = LEVELS.get(alert.get("level"), ("", 0))[0]
    created = alert.get("created_at", int(time.time()))
    lines = [
        f"### ⚠️【{level_name}预警】{alert.get('rule_name', '')}",
        f"> {alert.get('title', '')}",
        "",
        alert.get("summary", ""),
        "",
        f"话题：{alert.get('topic_name', '-')}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(created))}",
    ]
    if alert.get("link"):
        lines.append(f"[查看原帖]({alert['link']})")
    lines.append("请登录 Chat Cortech 预警中心查看详情。")
    return "\n".join(lines)


def push_wecom(webhook: str, alert: Dict[str, Any],
               timeout: int = 15) -> Tuple[bool, str]:
    """企业微信群机器人 webhook 推送，3 次重试。返回 (成功, 说明)。"""
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": _alert_markdown(alert)},
    }).encode("utf-8")
    last_err = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                webhook, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(body) if body else {}
                if data.get("errcode") == 0:
                    return True, "企业微信推送成功"
                last_err = f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1 + attempt)
    return False, f"3 次重试均失败：{last_err}"


def push_email(smtp_cfg: Dict[str, Any], alert: Dict[str, Any],
               timeout: int = 20) -> Tuple[bool, str]:
    """SMTP 邮件推送，2 次重试。返回 (成功, 说明)。"""
    level_name = LEVELS.get(alert.get("level"), ("", 0))[0]
    subject = f"【{level_name}预警】{alert.get('title', '')}"[:100]
    body = _alert_markdown(alert).replace("### ", "").replace("> ", "")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.get("from") or smtp_cfg.get("user", "")
    msg["To"] = smtp_cfg.get("to", "")
    last_err = ""
    for attempt in range(2):
        try:
            host = smtp_cfg.get("host", "")
            port = int(smtp_cfg.get("port", 587))
            if smtp_cfg.get("use_ssl") or port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=timeout)
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
            with server:
                try:
                    server.starttls()
                except Exception:
                    pass
                if smtp_cfg.get("user"):
                    server.login(smtp_cfg["user"], smtp_cfg.get("password", ""))
                server.send_message(msg)
            return True, "邮件推送成功"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1 + attempt)
    return False, f"2 次重试均失败：{last_err}"


def test_push(channel: str) -> Tuple[bool, str]:
    """推送配置页"测试"按钮：发一条测试预警。channel: wecom | email。"""
    cfg = get_push_config()
    test_alert = {
        "id": 0, "rule_name": "推送测试", "level": "yellow",
        "title": "这是一条推送测试消息",
        "summary": "如果你收到这条消息，说明推送通道配置正确。",
        "topic_name": "（测试）", "link": "",
        "created_at": int(time.time()),
    }
    if channel == "wecom":
        wh = cfg["wecom"].get("webhook", "")
        if not cfg["wecom"].get("enabled") or not wh:
            return False, "企业微信推送未启用或未配置 webhook"
        return push_wecom(wh, test_alert)
    if channel == "email":
        smtp = cfg["smtp"]
        if not smtp.get("enabled") or not smtp.get("host"):
            return False, "邮件推送未启用或未配置 SMTP"
        return push_email(smtp, test_alert)
    return False, f"未知通道: {channel}"


def manual_push(alert_id: int, channel: str) -> Tuple[bool, str]:
    """预警详情页手动重新推送（红/橙/黄均可）。"""
    alert = get_alert(alert_id)
    if not alert:
        return False, "预警不存在"
    cfg = get_push_config()
    if channel == "wecom":
        wh = cfg["wecom"].get("webhook", "")
        if not cfg["wecom"].get("enabled") or not wh:
            return False, "企业微信推送未启用或未配置 webhook"
        ok, detail = push_wecom(wh, alert)
    elif channel == "email":
        smtp = cfg["smtp"]
        if not smtp.get("enabled") or not smtp.get("host"):
            return False, "邮件推送未启用或未配置 SMTP"
        ok, detail = push_email(smtp, alert)
    else:
        return False, f"未知通道: {channel}"
    with _lock, _connect() as conn:
        if channel == "wecom":
            target = _mask_url(cfg["wecom"]["webhook"])
        else:
            target = cfg["smtp"].get("to", "")
        _log_push(conn, alert_id, channel, target, ok, detail)
    return ok, detail
