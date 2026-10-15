# -*- coding: utf-8 -*-
"""小红书影响力分：套用新榜指数公式（新榜官方白皮书 v4.1）。

指标映射（小红书）：
  R  总互动   = 周期内该作者所有帖子互动（赞+藏+评+转）之和
  Rm 最高单篇 = 单篇最高互动
  Ra 平均互动 = 帖均互动
  Rh 近期热度 = 近7天帖子互动之和（映射新榜"头条"的新鲜度含义）
  Z  赞藏比   = 总点赞 / (总收藏+1)

标准化（对数归一，解决头部碾压）：
  X' = ln(X+1) / ln(n·X̄+1) × 1000
  n = days/30（周期因子；Z 为比值，n=1）
  X̄ 为小红书量级基准常数（可调，见 _REF）

加权（与新榜一致）：
  Cortech = 0.75·R' + 0.05·Rm' + 0.10·Ra' + 0.05·Rh' + 0.05·Z'
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import _connect, apply_uid_filter, uid_sql

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

# 小红书量级基准常数（对标新榜 R̄=800000 等，按小红书达人量级缩小）
_REF = {"R": 20000.0, "Rm": 5000.0, "Ra": 2000.0, "Rh": 5000.0, "Z": 3.0}
_WEIGHTS = {"R": 0.75, "Rm": 0.05, "Ra": 0.10, "Rh": 0.05, "Z": 0.05}


def _day_bounds(days: int):
    today = datetime.now(TZ).date()
    end = today
    start = end - timedelta(days=days - 1)
    s = int(datetime(start.year, start.month, start.day, tzinfo=TZ).timestamp())
    e = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=TZ).timestamp())
    return s, e


def _norm(x: float, ref: float, n: float = 1.0) -> float:
    """新榜式对数标准化。"""
    return math.log(x + 1) / math.log(n * ref + 1) * 1000


def _score_metrics(R: float, Rm: float, Ra: float, Rh: float, Z: float,
                   days: int) -> Dict[str, float]:
    n = max(days / 30.0, 1 / 30.0)
    Rp = _norm(R, _REF["R"], n)
    Rmp = _norm(Rm, _REF["Rm"], n)
    Rap = _norm(Ra, _REF["Ra"], n)
    Rhp = _norm(Rh, _REF["Rh"], n)
    Zp = _norm(Z, _REF["Z"], 1.0)
    score = (_WEIGHTS["R"] * Rp + _WEIGHTS["Rm"] * Rmp + _WEIGHTS["Ra"] * Rap
             + _WEIGHTS["Rh"] * Rhp + _WEIGHTS["Z"] * Zp)
    return {"R": round(Rp, 1), "Rm": round(Rmp, 1), "Ra": round(Rap, 1),
            "Rh": round(Rhp, 1), "Z": round(Zp, 1), "cortech_score": round(score, 1)}


def _fetch_author_notes(s: int, e: int, topic_id: Optional[int],
                        author_ids: Optional[List[str]] = None):
    where: List[str] = ["publish_time>=?", "publish_time<=?"]
    params: List[Any] = [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    if author_ids:
        where.append(f"author_id IN ({','.join('?' * len(author_ids))})")
        params += list(author_ids)
    apply_uid_filter(where, params)
    with _connect() as conn:
        return conn.execute(
            f"""SELECT author_id, author, fans_count, publish_time,
                       COALESCE(liked_count,0) AS liked,
                       COALESCE(collected_count,0) AS collected,
                       COALESCE(comment_count,0) AS commented,
                       COALESCE(share_count,0) AS shared
                FROM notes WHERE {' AND '.join(where)}""", params).fetchall()


def _aggregate(rows, e: int, days: int) -> Dict[str, Dict[str, Any]]:
    """按作者聚合互动指标。"""
    recent_cut = e - 7 * 86400
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        aid = r["author_id"] or r["author"] or "unknown"
        eng = r["liked"] + r["collected"] + r["commented"] + r["shared"]
        d = agg.setdefault(aid, {
            "author_id": aid, "nickname": r["author"] or "",
            "fans_count": r["fans_count"] or 0,
            "notes": 0, "R": 0, "Rm": 0, "likes": 0, "collects": 0, "Rh": 0})
        d["notes"] += 1
        d["R"] += eng
        d["Rm"] = max(d["Rm"], eng)
        d["likes"] += r["liked"]
        d["collects"] += r["collected"]
        if (r["publish_time"] or 0) >= recent_cut:
            d["Rh"] += eng
    out = {}
    for aid, d in agg.items():
        Ra = d["R"] / d["notes"] if d["notes"] else 0
        Z = d["likes"] / (d["collects"] + 1)
        metrics = _score_metrics(d["R"], d["Rm"], Ra, d["Rh"], Z, days)
        out[aid] = {**d, "Ra": round(Ra, 1), "Z_raw": round(Z, 2), **metrics}
    return out


def author_influence(topic_id: Optional[int] = None, days: int = 30,
                     top_n: int = 50) -> List[Dict[str, Any]]:
    """达人影响力榜：周期内发帖作者按 cortech_score 排序。"""
    s, e = _day_bounds(days)
    agg = _aggregate(_fetch_author_notes(s, e, topic_id), e, days)
    ranked = sorted(agg.values(), key=lambda x: x["cortech_score"], reverse=True)
    return ranked[:top_n]


def score_for_authors(author_ids: List[str], days: int = 30) -> Dict[str, float]:
    """给指定作者批量算影响力分（达人库列表用）。"""
    if not author_ids:
        return {}
    s, e = _day_bounds(days)
    agg = _aggregate(_fetch_author_notes(s, e, None, author_ids), e, days)
    return {aid: d["cortech_score"] for aid, d in agg.items()}


def brand_sov(topic_id: int, days: int = 30) -> Dict[str, Any]:
    """SOV（声量份额）：品牌声量 / 品类总声量。
    品牌声量 = 该话题帖子数；品类总声量 = 同期全部话题帖子数。"""
    s, e = _day_bounds(days)
    uw_sql, uw_params = uid_sql()
    with _connect() as conn:
        brand_volume = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE topic_id=? AND publish_time>=? AND publish_time<=?"
            f"{uw_sql}",
            (topic_id, s, e, *uw_params)).fetchone()[0]
        category_volume = conn.execute(
            f"SELECT COUNT(*) FROM notes WHERE publish_time>=? AND publish_time<=?{uw_sql}",
            (s, e, *uw_params)).fetchone()[0]
    sov = round(brand_volume / category_volume, 4) if category_volume else 0.0
    return {"topic_id": topic_id, "days": days,
            "brand_volume": brand_volume, "category_volume": category_volume,
            "sov": sov, "sov_pct": f"{sov:.1%}"}
