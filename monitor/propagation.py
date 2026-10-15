# -*- coding: utf-8 -*-
"""P0 能力：传播路径图谱（三层结构，对标知微事见逆向结论）。

第一层 timeline：传播趋势时间线（按小时/天统计帖子数、互动量）
第二层 key nodes：关键传播节点（三类：传播源头/放大器/桥梁）
第三层 portrait/graph：人群画像（粉丝量级分布、地域分布）+ 传播关系有向图

基础建边逻辑：
1. 取事件下所有帖子，按 publish_time 升序
2. 两两计算标题相似度（jieba 分词 + 余弦），> 0.7 视为传播关系
3. 边方向：早发 → 晚发（传播方向）

纯手写邻接表，不引入 networkx。
"""
from __future__ import annotations

import datetime
import logging
import math
import sqlite3
from collections import Counter, defaultdict, deque
from typing import Any, Dict, List, Tuple

from . import config
from .db import uid_sql
from .event_cluster import _tokenize, _tfidf_vectors, _cosine

logger = logging.getLogger(__name__)

SIM_THRESHOLD = 0.7

# 粉丝量级划分（与达人库一致）
FAN_TIERS = [
    ("百万+", 1_000_000, float("inf")),
    ("10万+", 100_000, 1_000_000),
    ("1万+", 10_000, 100_000),
    ("1万以下", 0, 10_000),
]


def _db_path() -> str:
    return str(config.DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _get_event_notes(event_id: int) -> List[Dict[str, Any]]:
    """取事件下所有帖子，按发布时间升序。"""
    uw_sql, uw_params = uid_sql()
    with _connect() as conn:
        note_ids = [r["post_id"] for r in conn.execute(
            f"SELECT post_id FROM event_notes WHERE event_id=?{uw_sql}", (event_id, *uw_params)).fetchall()]
        if not note_ids:
            return []
        q = ",".join("?" * len(note_ids))
        rows = conn.execute(
            "SELECT post_id, title, desc, author, author_id, fans_count, publish_time,"
            " liked_count, collected_count, comment_count, share_count,"
            " sentiment, note_url, ip_location FROM notes"
            f" WHERE post_id IN ({q}){uw_sql}",
            (*note_ids, *uw_params),
        ).fetchall()
        notes = [dict(r) for r in rows]
        notes.sort(key=lambda n: (n.get("publish_time") or 0))
        return notes


def _engagement(n: Dict[str, Any]) -> int:
    return (n.get("liked_count") or 0) + (n.get("comment_count") or 0) * 2 + (n.get("collected_count") or 0)


def _classify_key_roles(notes: List[Dict[str, Any]],
                       edges: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """三类关键节点（对标知微事见逆向结论）。

    - 传播源头：出度最大 + 时间最早（事件的起点）
    - 放大器：粉丝数 × 出度（把声量放大的账号）
    - 桥梁：中介中心度最高（连接不同传播分支的节点）
    """
    roles: Dict[str, List[str]] = {"source": [], "amplifier": [], "bridge": []}
    if not notes:
        return roles

    by_id = {n["post_id"]: n for n in notes}
    out_deg: Dict[str, int] = {n["post_id"]: 0 for n in notes}
    adj: Dict[str, List[str]] = {n["post_id"]: [] for n in notes}
    for e in edges:
        s, t = e["source"], e["target"]
        if s in adj and t in adj:
            adj[s].append(t)
            out_deg[s] += 1

    # 1. 传播源头：出度最大者中取时间最早
    if edges:
        max_out = max(out_deg.values())
        cands = [nid for nid, d in out_deg.items() if d == max_out]
        cands.sort(key=lambda nid: (by_id[nid].get("publish_time") or 0))
        roles["source"] = cands[:1]
    else:
        # 无传播边时，最早发布的为源头
        earliest = min(notes, key=lambda n: (n.get("publish_time") or 0))
        roles["source"] = [earliest["post_id"]]

    # 2. 放大器：粉丝数 × 出度，Top2
    scored = sorted(
        notes,
        key=lambda n: -(n.get("fans_count") or 0) * (out_deg[n["post_id"]] + 1),
    )
    roles["amplifier"] = [n["post_id"] for n in scored[:2]
                          if out_deg[n["post_id"]] > 0 or (n.get("fans_count") or 0) >= 10000]

    # 3. 桥梁：中介中心度（Brandes 算法，手写）
    between = _betweenness(list(by_id.keys()), adj)
    if between:
        top = max(between.values())
        if top > 0:
            roles["bridge"] = sorted(
                [nid for nid, b in between.items() if b == top],
                key=lambda nid: (by_id[nid].get("publish_time") or 0),
            )[:2]

    return roles


def _betweenness(nodes: List[str], adj: Dict[str, List[str]]) -> Dict[str, float]:
    """Brandes 中介中心度（有向图）。事件帖子数通常 < 200，直接算可行。"""
    cb: Dict[str, float] = {v: 0.0 for v in nodes}
    for s in nodes:
        stack: List[str] = []
        pred: Dict[str, List[str]] = {w: [] for w in nodes}
        sigma: Dict[str, float] = {w: 0.0 for w in nodes}
        sigma[s] = 1.0
        dist: Dict[str, int] = {w: -1 for w in nodes}
        dist[s] = 0
        q: deque = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in adj.get(v, []):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta: Dict[str, float] = {w: 0.0 for w in nodes}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                cb[w] += delta[w]
    return cb


def get_timeline(event_id: int, granularity: str = "day") -> Dict[str, Any]:
    """第一层：传播趋势时间线。

    按小时/天统计帖子数与互动量，输出：
    {"granularity": "day", "points": [{"bucket": "2026-10-14", "count": 5, "engagement": 120}]}
    """
    notes = _get_event_notes(event_id)
    if granularity not in ("hour", "day"):
        granularity = "day"
    buckets: Dict[str, List[int]] = defaultdict(lambda: [0, 0])  # count, engagement
    for n in notes:
        ts = n.get("publish_time")
        if not ts:
            continue
        dt = datetime.datetime.fromtimestamp(ts)
        key = dt.strftime("%Y-%m-%d %H:00") if granularity == "hour" else dt.strftime("%Y-%m-%d")
        buckets[key][0] += 1
        buckets[key][1] += _engagement(n)
    points = [
        {"bucket": k, "count": v[0], "engagement": v[1]}
        for k, v in sorted(buckets.items())
    ]
    return {"granularity": granularity, "points": points}


def _fan_tier(fans: int) -> str:
    for label, lo, hi in FAN_TIERS:
        if lo <= fans < hi:
            return label
    return "1万以下"


def get_portrait(event_id: int) -> Dict[str, Any]:
    """第三层：人群画像。

    参与账号的粉丝量级分布 + 地域分布（IP 属地）。
    注：库里暂无账号认证字段，不虚构认证比例，只用真实数据。
    """
    notes = _get_event_notes(event_id)
    seen: Dict[str, Dict[str, Any]] = {}
    for n in notes:
        aid = n.get("author_id") or n.get("author") or n["post_id"]
        if aid not in seen:
            seen[aid] = {
                "fans": n.get("fans_count") or 0,
                "region": (n.get("ip_location") or "").strip() or "未知",
            }
    tier_counter: Counter = Counter()
    region_counter: Counter = Counter()
    for a in seen.values():
        tier_counter[_fan_tier(a["fans"])] += 1
        region_counter[a["region"]] += 1
    tier_order = [label for label, _, _ in FAN_TIERS]
    fan_tiers = [
        {"label": label, "count": tier_counter.get(label, 0)}
        for label in tier_order
    ]
    regions = [
        {"label": label, "count": count}
        for label, count in region_counter.most_common(8)
    ]
    return {
        "account_count": len(seen),
        "fan_tiers": fan_tiers,
        "regions": regions,
    }


def build_propagation(event_id: int) -> Dict[str, Any]:
    """构建传播路径图。

    返回：
    {
        "nodes": [{"id": post_id, "title": ..., "author": ..., "publish_time": ...,
                   "engagement": int, "sentiment": ..., "is_key": bool}],
        "edges": [{"source": post_id, "target": post_id, "similarity": float}],
        "key_nodes": [post_id, ...],  # 入度最高的 3 个（兼容旧版）
        "key_node_roles": {"source": [...], "amplifier": [...], "bridge": [...]},
        "portrait": {"account_count": int,
                     "fan_tiers": [{"label": ..., "count": ...}],
                     "regions": [{"label": ..., "count": ...}]},
    }
    """
    notes = _get_event_notes(event_id)
    if len(notes) < 2:
        solo_roles = {"source": [n["post_id"] for n in notes],
                      "amplifier": [], "bridge": []}
        return {"nodes": [
            {"id": n["post_id"], "title": n.get("title") or "",
             "author": n.get("author") or "", "publish_time": n.get("publish_time"),
             "engagement": _engagement(n), "sentiment": n.get("sentiment") or "neutral",
             "is_key": True, "role": "source"}
            for n in notes
        ], "edges": [], "key_nodes": [n["post_id"] for n in notes],
            "key_node_roles": solo_roles, "portrait": get_portrait(event_id)}

    # 标题分词 + TF-IDF 向量化
    docs = [_tokenize((n.get("title") or "") + " " + (n.get("desc") or "")[:200]) for n in notes]
    vecs, norms, _ = _tfidf_vectors(docs)

    # 构建边：早发 → 晚发，相似度 > 阈值
    edges: List[Dict[str, Any]] = []
    in_degree: Dict[str, int] = {n["post_id"]: 0 for n in notes}
    n = len(notes)
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine(vecs[i], norms[i], vecs[j], norms[j])
            if sim >= SIM_THRESHOLD:
                src = notes[i]["post_id"]
                tgt = notes[j]["post_id"]
                edges.append({"source": src, "target": tgt, "similarity": round(sim, 3)})
                in_degree[tgt] += 1

    # 关键节点：入度 Top3
    ranked = sorted(in_degree.items(), key=lambda x: -x[1])
    key_nodes = [nid for nid, deg in ranked[:3] if deg > 0]
    if not key_nodes:
        # 无传播边时，取互动量最高的为关键节点
        top = max(notes, key=_engagement)
        key_nodes = [top["post_id"]]
    key_set = set(key_nodes)
    key_roles = _classify_key_roles(notes, edges)

    nodes = []
    for nt in notes:
        nid = nt["post_id"]
        role = None
        for rname, rids in key_roles.items():
            if nid in rids:
                role = rname
                break
        nodes.append({
            "id": nid,
            "title": (nt.get("title") or "")[:40],
            "author": nt.get("author") or "",
            "publish_time": nt.get("publish_time"),
            "engagement": _engagement(nt),
            "sentiment": nt.get("sentiment") or "neutral",
            "is_key": nid in key_set,
            "role": role,
        })

    return {"nodes": nodes, "edges": edges, "key_nodes": key_nodes,
            "key_node_roles": key_roles, "portrait": get_portrait(event_id)}
