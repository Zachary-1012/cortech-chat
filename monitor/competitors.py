# -*- coding: utf-8 -*-
"""竞品对标看板：本品牌 vs 竞品的声量 / 情感对比。

设计原则（口径一致）：
- 每个竞品 = 站内一条普通话题（复用采集引擎、情感模型、发布时间聚合），
  本模块只维护「竞品元信息 → topic_id」的映射表，不复制任何采集/分析逻辑。
- 趋势统计直接调用 trends.volume_trend / trends.sentiment_trend，
  与站内话题页的数字口径天然一致。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import db
from .db import apply_uid_filter, current_user_id
from .db import _connect
from .trends import _day_bounds, sentiment_trend, volume_trend

logger = logging.getLogger("xhs.competitors")

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    topic_id INTEGER NOT NULL UNIQUE,
    is_self INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


def _ensure() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        _cols = [r["name"] for r in conn.execute("PRAGMA table_info(competitors)").fetchall()]
        if "user_id" not in _cols:
            conn.execute("ALTER TABLE competitors ADD COLUMN user_id INTEGER")


def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "keywords": r["keywords"],
        "topic_id": r["topic_id"],
        "is_self": bool(r["is_self"]),
        "created_at": r["created_at"],
    }


def list_competitors() -> List[Dict[str, Any]]:
    """竞品列表（含本方标记）。"""
    _ensure()
    where, params = [], []
    apply_uid_filter(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM competitors {clause} ORDER BY is_self DESC, id", params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_competitor(cid: int) -> Optional[Dict[str, Any]]:
    _ensure()
    where, params = ["id=?"], [cid]
    apply_uid_filter(where, params)
    with _connect() as conn:
        r = conn.execute(f"SELECT * FROM competitors WHERE {' AND '.join(where)}", params).fetchone()
    return _row_to_dict(r) if r else None


def create_competitor(name: str, keywords: str, is_self: bool = False,
                      max_notes: int = 50) -> Dict[str, Any]:
    """新增竞品：同步创建一条站内话题（纯手动触发，不自动定时）。"""
    name = (name or "").strip()
    keywords = (keywords or "").strip()
    if not name:
        raise ValueError("请填写竞品名称")
    if not keywords:
        raise ValueError("请填写竞品关键词（用于采集，如：SKP,北京SKP）")
    _ensure()
    topic_id = db.create_topic(
        name=f"竞品对标·{name}", keywords=keywords,
        max_notes=max_notes, enable_comments=1, enabled=0, engine="cortech_direct")
    try:
        uw_w, uw_p = [], []
        apply_uid_filter(uw_w, uw_p)
        uw_clause = f"WHERE {' AND '.join(uw_w)}" if uw_w else ""
        with _lock, _connect() as conn:
            if is_self:
                conn.execute(f"UPDATE competitors SET is_self=0 {uw_clause}", uw_p)
            cur = conn.execute(
                "INSERT INTO competitors (user_id, name, keywords, topic_id, is_self, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (current_user_id(), name, keywords, topic_id, 1 if is_self else 0, int(time.time())))
            cid = cur.lastrowid
    except Exception:
        # 映射写入失败时回滚话题，避免孤儿话题
        try:
            db.delete_topic(topic_id)
        except Exception:
            pass
        raise
    logger.info("新增竞品 %s（topic_id=%s）", name, topic_id)
    return get_competitor(cid)  # type: ignore[return-value]


def update_competitor(cid: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """更新竞品：名称/关键词同步回话题；is_self 互斥。"""
    comp = get_competitor(cid)
    if not comp:
        raise ValueError("竞品不存在")
    _ensure()
    name = fields.get("name")
    keywords = fields.get("keywords")
    is_self = fields.get("is_self")
    uw_w, uw_p = [], []
    apply_uid_filter(uw_w, uw_p)
    uw_and = f" AND {' AND '.join(uw_w)}" if uw_w else ""
    with _lock, _connect() as conn:
        if is_self:
            conn.execute(f"UPDATE competitors SET is_self=0" + (f" WHERE {' AND '.join(uw_w)}" if uw_w else ""), uw_p)
            conn.execute(f"UPDATE competitors SET is_self=1 WHERE id=?{uw_and}", (cid, *uw_p))
        sets, params = [], []
        if isinstance(name, str) and name.strip():
            sets.append("name=?"); params.append(name.strip())
        if isinstance(keywords, str) and keywords.strip():
            sets.append("keywords=?"); params.append(keywords.strip())
        if sets:
            conn.execute(
                f"UPDATE competitors SET {', '.join(sets)} WHERE id=?{uw_and}",
                (*params, cid, *uw_p))
    comp = get_competitor(cid) or comp
    # 同步话题侧的名称与关键词
    topic_fields: Dict[str, Any] = {}
    if isinstance(name, str) and name.strip():
        topic_fields["name"] = f"竞品对标·{name.strip()}"
    if isinstance(keywords, str) and keywords.strip():
        topic_fields["keywords"] = keywords.strip()
    if topic_fields:
        db.update_topic(comp["topic_id"], topic_fields)
    return comp


def delete_competitor(cid: int) -> bool:
    """删除竞品：同步删除其映射的话题（话题下的帖子随话题删除逻辑走）。"""
    comp = get_competitor(cid)
    if not comp:
        return False
    uw_w, uw_p = [], []
    apply_uid_filter(uw_w, uw_p)
    uw_and = f" AND {' AND '.join(uw_w)}" if uw_w else ""
    with _lock, _connect() as conn:
        conn.execute(f"DELETE FROM competitors WHERE id=?{uw_and}", (cid, *uw_p))
    try:
        db.delete_topic(comp["topic_id"])
    except Exception as e:
        logger.warning("删除竞品话题失败（topic_id=%s）：%s", comp["topic_id"], e)
    return True


def crawl_competitor(cid: int, account_id: Optional[int] = None,
                     aggressive: bool = False) -> Dict[str, Any]:
    """对竞品发起一次采集（复用站内采集链路）。"""
    from . import crawler_driver  # 延迟导入，避免循环
    comp = get_competitor(cid)
    if not comp:
        raise ValueError("竞品不存在")
    task_id = crawler_driver.trigger_crawl(
        comp["topic_id"], account_id=account_id, aggressive=aggressive)
    return {"task_id": task_id, "competitor_id": cid, "topic_id": comp["topic_id"]}


def _top_posts(topic_id: int, days: int, limit: int = 5) -> List[Dict[str, Any]]:
    s, e = _day_bounds(days)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT post_id, title, author, liked_count, comment_count, note_url,"
            " sentiment, publish_time FROM notes"
            " WHERE topic_id=? AND publish_time>=? AND publish_time<=?"
            " ORDER BY (liked_count + comment_count * 3) DESC LIMIT ?",
            (topic_id, s, e, limit)).fetchall()
    return [dict(r) for r in rows]


def _entry_stats(comp: Dict[str, Any], days: int) -> Dict[str, Any]:
    """单个竞品（含本方）的聚合指标：口径与站内话题页完全一致。"""
    tid = comp["topic_id"]
    vol = volume_trend(tid, days)
    sent = sentiment_trend(tid, days)
    total = sum(p["count"] for p in vol)
    neg = sum(p["negative"] for p in sent)
    neu = sum(p["neutral"] for p in sent)
    pos = sum(p["positive"] for p in sent)
    neg_ratio = round(neg / total, 3) if total else 0.0
    # 日均声量
    avg_daily = round(total / days, 1) if days else 0.0
    return {
        "id": comp["id"],
        "name": comp["name"],
        "is_self": comp["is_self"],
        "topic_id": tid,
        "volume_total": total,
        "avg_daily": avg_daily,
        "neg_ratio": neg_ratio,
        "sentiment_dist": {"negative": neg, "neutral": neu, "positive": pos},
        "volume_trend": vol,
        "sentiment_trend": sent,
        "top_posts": _top_posts(tid, days),
    }


def dashboard(days: int = 30) -> Dict[str, Any]:
    """对标看板数据：本方排首位，其余按声量倒序；另附负面率排行。"""
    days = max(1, min(365, int(days or 30)))
    comps = list_competitors()
    entries = [_entry_stats(c, days) for c in comps]
    self_entry = next((x for x in entries if x["is_self"]), None)
    others = sorted([x for x in entries if not x["is_self"]],
                    key=lambda x: x["volume_total"], reverse=True)
    ordered = ([self_entry] if self_entry else []) + others
    neg_ranking = sorted(entries, key=lambda x: x["neg_ratio"], reverse=True)
    return {
        "days": days,
        "entries": ordered,
        "neg_ranking": [{"name": x["name"], "is_self": x["is_self"],
                         "neg_ratio": x["neg_ratio"],
                         "volume_total": x["volume_total"]} for x in neg_ranking],
        "periods": [p["period"] for p in volume_trend(entries[0]["topic_id"], days)] if entries else [],
    }


def _rule_commentary(data: Dict[str, Any]) -> str:
    """无 AI 时的规则版一句话点评：本方 vs 竞品均值。"""
    entries = data.get("entries", [])
    if not entries:
        return "暂无竞品数据，请先添加竞品并采集。"
    self_e = next((x for x in entries if x["is_self"]), None)
    others = [x for x in entries if not x["is_self"]]
    days = data.get("days", 30)
    if not self_e or not others:
        names = "、".join(x["name"] for x in entries)
        return f"当前对标 {names}，近{days}天共 {sum(x['volume_total'] for x in entries)} 条声量。"
    avg_vol = sum(x["volume_total"] for x in others) / len(others)
    avg_neg = sum(x["neg_ratio"] for x in others) / len(others)
    parts = [f"近{days}天{self_e['name']}声量 {self_e['volume_total']} 条"]
    if avg_vol > 0:
        diff = (self_e["volume_total"] - avg_vol) / avg_vol
        parts.append(f"较竞品均值{'高' if diff >= 0 else '低'} {abs(diff):.0%}")
    else:
        parts.append("竞品暂无声量")
    parts.append(f"负面率 {self_e['neg_ratio']:.1%}（竞品均值 {avg_neg:.1%}）")
    if self_e["neg_ratio"] > avg_neg + 0.02:
        parts.append("负面率偏高，建议优先跟进高热负面帖")
    elif self_e["neg_ratio"] < avg_neg - 0.02:
        parts.append("口碑优于竞品，可提炼正面点做内容")
    else:
        parts.append("口碑与竞品基本持平")
    leader = max(others, key=lambda x: x["volume_total"])
    if leader["volume_total"] > self_e["volume_total"]:
        parts.append(f"声量最高的是{leader['name']}（{leader['volume_total']} 条）")
    return "；".join(parts) + "。"


def commentary(days: int = 30) -> Dict[str, Any]:
    """关键差异点 AI 一句话点评；无 AI 时用规则生成。"""
    data = dashboard(days)
    rule_text = _rule_commentary(data)
    from . import ai_chat
    mode, _ = ai_chat.check_ai_ready()
    if mode != "ai":
        return {"mode": "rule", "provider": None, "model": None,
                "text": rule_text, "notice": "当前无可用 AI，已用规则生成点评"}
    provider = ai_chat._mode_cache.get("provider")
    client, p = ai_chat._openai_client(provider)
    lines = []
    for x in data["entries"]:
        d = x["sentiment_dist"]
        lines.append(
            f"{x['name']}{'（本方）' if x['is_self'] else ''}：声量{x['volume_total']}条，"
            f"负面率{x['neg_ratio']:.1%}（负{d['negative']}/"
            f"中{d['neutral']}/正{d['positive']}）")
    prompt = (
        "你是商业地产舆情顾问。用一句话（不超过80字）点评以下商场舆情对标数据的关键差异，"
        "直接给结论，可带一条行动建议，不要复述数字表格。\n"
        + "\n".join(lines))
    try:
        # 品牌名从配置读取（通用化）
        from .site import get_brand
        brand = get_brand()
        resp = client.chat.completions.create(
            model=p["model"],
            messages=[
                {"role": "system", "content": f"你是{brand['name']}运营顾问，说话简洁，一针见血。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("对标点评 AI 调用失败：%s", type(e).__name__)
        return {"mode": "rule", "provider": None, "model": None,
                "text": rule_text,
                "notice": f"AI 调用失败（{type(e).__name__}），已用规则生成点评"}
    if not text:
        return {"mode": "rule", "provider": None, "model": None,
                "text": rule_text, "notice": "模型返回空，已用规则生成点评"}
    return {"mode": "ai", "provider": p["name"], "model": p["model"], "text": text}
