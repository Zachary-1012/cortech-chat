# -*- coding: utf-8 -*-
"""P0 能力：达人库。

小红书达人（KOL/KOC）管理：采集时自动入库作者信息，
支持手动添加、打领域标签、记录合作状态。

复用 db.py 的 authors 表，首次调用时加列（不破坏已有数据）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

from . import config
from .db import apply_uid_filter, current_user_id, uid_sql

logger = logging.getLogger(__name__)

SCHEMA_PATCH = """
ALTER TABLE authors ADD COLUMN domain_tags TEXT DEFAULT '[]';
ALTER TABLE authors ADD COLUMN coop_status TEXT DEFAULT 'none';
ALTER TABLE authors ADD COLUMN coop_notes TEXT DEFAULT '';
ALTER TABLE authors ADD COLUMN xiaohongshu_id TEXT DEFAULT '';
"""

COOP_STATUS = ("none", "contacted", "cooperating", "done", "blocked")
COOP_LABEL = {
    "none": "未合作",
    "contacted": "已联系",
    "cooperating": "合作中",
    "done": "已合作",
    "blocked": "拉黑",
}


def _db_path() -> str:
    return str(config.DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns() -> None:
    """给 authors 表加达人库字段（幂等）。"""
    with _connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(authors)").fetchall()}
        for col, ddl in (("domain_tags", "TEXT DEFAULT '[]'"),
                         ("coop_status", "TEXT DEFAULT 'none'"),
                         ("coop_notes", "TEXT DEFAULT ''"),
                         ("xiaohongshu_id", "TEXT DEFAULT ''"),
                         ("region", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                conn.execute(f"ALTER TABLE authors ADD COLUMN {col} {ddl}")
        conn.commit()


def _fans_level(fans: int) -> str:
    if fans >= 1_000_000:
        return "百万+"
    if fans >= 100_000:
        return "10万+"
    if fans >= 10_000:
        return "1万+"
    return "1万以下"


def list_influencers(q: Optional[str] = None,
                     domain: Optional[str] = None,
                     coop_status: Optional[str] = None,
                     min_fans: int = 0,
                     page: int = 1, page_size: int = 20,
                     region: Optional[str] = None) -> Dict[str, Any]:
    """达人列表，支持搜索、领域、合作状态、粉丝量、采集地区筛选。"""
    _ensure_columns()
    where, params = ["fans_count >= ?"], [int(min_fans)]
    apply_uid_filter(where, params)
    region_norm = None
    if region:
        from .xhs_region import normalize_region
        region_norm = normalize_region(region)
        uid = current_user_id()
        if uid is not None:
            where.append(
                "(IFNULL(region,'')=? OR author_id IN ("
                "SELECT author_id FROM notes WHERE IFNULL(region,'cn')=? AND user_id=?))"
            )
            params += [region_norm, region_norm, uid]
        else:
            where.append(
                "(IFNULL(region,'')=? OR author_id IN ("
                "SELECT author_id FROM notes WHERE IFNULL(region,'cn')=?))"
            )
            params += [region_norm, region_norm]
    if q:
        where.append("(nickname LIKE ? OR author_id LIKE ? OR xiaohongshu_id LIKE ?)")
        params += [f"%{q}%"] * 3
    if domain:
        where.append("domain_tags LIKE ?")
        params.append(f'%"{domain}"%')
    if coop_status:
        where.append("coop_status = ?")
        params.append(coop_status)
    w = "WHERE " + " AND ".join(where)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM authors {w}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT author_id, nickname, fans_count, liked_count, notes_count,"
            " ip_location, domain_tags, coop_status, coop_notes, xiaohongshu_id,"
            " region, updated_at FROM authors"
            f" {w} ORDER BY fans_count DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = []
        try:
            from . import influence as _influence
            _scores = _influence.score_for_authors(
                [r["author_id"] for r in rows], days=30)
        except Exception:
            _scores = {}
        for r in rows:
            d = dict(r)
            import json as _json
            try:
                d["domain_tags"] = _json.loads(d["domain_tags"] or "[]")
            except Exception:
                d["domain_tags"] = []
            d["fans_level"] = _fans_level(d["fans_count"] or 0)
            d["coop_label"] = COOP_LABEL.get(d["coop_status"], "未合作")
            d["cortech_score"] = _scores.get(d["author_id"], 0.0)
            items.append(d)
        from .influencer_estimates import FORMULAS, estimates_for
        est = estimates_for(
            [d["author_id"] for d in items],
            {d["author_id"]: d for d in items},
            region=region_norm,
        )
        for d in items:
            d["estimate"] = est.get(d["author_id"])
        return {"total": total, "page": page, "page_size": page_size,
                "region": region_norm or "", "formulas": FORMULAS, "items": items}


def get_influencer(author_id: str) -> Optional[Dict[str, Any]]:
    _ensure_columns()
    with _connect() as conn:
        extra, extra_p = uid_sql()
        r = conn.execute(
            "SELECT author_id, nickname, fans_count, liked_count, notes_count,"
            " ip_location, domain_tags, coop_status, coop_notes, xiaohongshu_id,"
            " region, updated_at FROM authors WHERE author_id=?" + extra,
            (author_id, *extra_p)).fetchone()
        if not r:
            return None
        d = dict(r)
        import json as _json
        try:
            d["domain_tags"] = _json.loads(d["domain_tags"] or "[]")
        except Exception:
            d["domain_tags"] = []
        d["fans_level"] = _fans_level(d["fans_count"] or 0)
        d["coop_label"] = COOP_LABEL.get(d["coop_status"], "未合作")
        from .influencer_estimates import estimate_author
        d["estimate"] = estimate_author(author_id, d)
        return d


def add_influencer(data: Dict[str, Any]) -> Dict[str, Any]:
    """手动添加达人。"""
    _ensure_columns()
    import json as _json
    author_id = (data.get("author_id") or "").strip()
    if not author_id:
        raise ValueError("缺少 author_id（小红书用户ID）")
    tags = data.get("domain_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO authors (user_id, author_id, nickname, fans_count,"
            " liked_count, notes_count, ip_location, domain_tags, coop_status,"
            " coop_notes, xiaohongshu_id, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (current_user_id(), author_id, data.get("nickname") or "",
             int(data.get("fans_count") or 0),
             int(data.get("liked_count") or 0), int(data.get("notes_count") or 0),
             data.get("ip_location") or "", _json.dumps(tags, ensure_ascii=False),
             data.get("coop_status") or "none", data.get("coop_notes") or "",
             data.get("xiaohongshu_id") or "", now),
        )
        conn.commit()
    return get_influencer(author_id) or {}


def update_influencer(author_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """更新达人：打标签、记合作状态等。"""
    _ensure_columns()
    import json as _json
    allowed = {"nickname", "fans_count", "liked_count", "notes_count", "ip_location",
               "domain_tags", "coop_status", "coop_notes", "xiaohongshu_id"}
    sets, params = [], []
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "domain_tags":
                if isinstance(v, str):
                    v = [t.strip() for t in v.split(",") if t.strip()]
                v = _json.dumps(v or [], ensure_ascii=False)
            elif k == "coop_status" and v not in COOP_STATUS:
                raise ValueError(f"合作状态非法: {v}")
            elif k in ("fans_count", "liked_count", "notes_count"):
                v = int(v or 0)
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return get_influencer(author_id)
    sets.append("updated_at=?")
    params.append(int(time.time()))
    params.append(author_id)
    extra, extra_p = uid_sql()
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE authors SET {', '.join(sets)} WHERE author_id=?{extra}",
            (*params, *extra_p))
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get_influencer(author_id)


def delete_influencer(author_id: str) -> bool:
    _ensure_columns()
    extra, extra_p = uid_sql()
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM authors WHERE author_id=?{extra}", (author_id, *extra_p))
        conn.commit()
        return cur.rowcount > 0


def domain_stats() -> List[Dict[str, Any]]:
    """领域标签统计（用于前端筛选器）。"""
    _ensure_columns()
    import json as _json
    from collections import Counter
    c: Counter = Counter()
    extra, extra_p = uid_sql()
    with _connect() as conn:
        for r in conn.execute(
                f"SELECT domain_tags FROM authors WHERE 1=1{extra}", extra_p).fetchall():
            try:
                for t in _json.loads(r["domain_tags"] or "[]"):
                    c[t] += 1
            except Exception:
                continue
    return [{"domain": k, "count": v} for k, v in c.most_common(30)]


def similar_influencers(author_id: str, limit: int = 10,
                        region: Optional[str] = None) -> List[Dict[str, Any]]:
    """相似达人推荐（灰豚逆向：/anchor/detail/likeAnchors）。

    按领域标签重叠度 + 粉丝量级接近度打分，排除自己。
    """
    _ensure_columns()
    import json as _json
    target = get_influencer(author_id)
    if not target:
        return []
    t_tags = set(target.get("domain_tags") or [])
    t_fans = target.get("fans_count") or 0
    t_level = _fans_level(t_fans)
    t_category = ((target.get("estimate") or {}).get("category") or "")
    region_norm = None
    if region:
        from .xhs_region import normalize_region
        region_norm = normalize_region(region)

    import math
    scored = []
    with _connect() as conn:
        extra, extra_p = uid_sql()
        rows = conn.execute(
            "SELECT author_id, nickname, fans_count, liked_count, notes_count,"
            " ip_location, domain_tags, coop_status, coop_notes, xiaohongshu_id,"
            " region, updated_at FROM authors WHERE author_id != ?" + extra,
            (author_id, *extra_p)).fetchall()
        for r in rows:
            d = dict(r)
            try:
                tags = set(_json.loads(d["domain_tags"] or "[]"))
            except Exception:
                tags = set()
            # 领域重叠：Jaccard
            union = t_tags | tags
            jaccard = len(t_tags & tags) / len(union) if union else 0.0
            # 粉丝量级：同量级 1.0，相邻 0.5，否则按对数距离衰减
            fans = d["fans_count"] or 0
            level = _fans_level(fans)
            if level == t_level:
                level_score = 1.0
            else:
                dist = abs(math.log10(max(fans, 1)) - math.log10(max(t_fans, 1)))
                level_score = max(0.0, 1.0 - dist / 2.0)
            score = round(jaccard * 0.7 + level_score * 0.3, 3)
            if score <= 0:
                continue
            d["domain_tags"] = sorted(tags)
            d["fans_level"] = level
            d["coop_label"] = COOP_LABEL.get(d["coop_status"], "未合作")
            d["similarity"] = score
            scored.append(d)
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    picked = scored[: max(limit * 3, limit)]
    if region_norm:
        picked = [d for d in picked
                  if (d.get("region") or "") in ("", region_norm)] or picked
    from .influencer_estimates import estimates_for
    est = estimates_for([d["author_id"] for d in picked],
                        {d["author_id"]: d for d in picked},
                        region=region_norm)
    for d in picked:
        d["estimate"] = est.get(d["author_id"])
        same_cat = t_category and t_category != "未分类" and (
            (d.get("estimate") or {}).get("category") == t_category)
        if same_cat:
            d["similarity"] = round(min(1.0, d["similarity"] + 0.05), 3)
            d["similar_reason"] = "领域接近，内容类目相同"
        else:
            d["similar_reason"] = "领域标签或粉丝量级接近"
    picked.sort(key=lambda x: x["similarity"], reverse=True)
    return picked[:limit]
