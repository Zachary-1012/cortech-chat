# -*- coding: utf-8 -*-
"""P0 能力：事件聚类 / 竞品对标 / 分级预警。

全部基于本地已入库数据计算，零外部依赖。
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import db, trends

TZ = timezone(timedelta(hours=8))


# ---------------- 分级预警 ----------------

def alert_level(ratio: float, yesterday_neg: int) -> Dict[str, str]:
    """四级：critical 紧急 / high 严重 / medium 关注 / normal 正常"""
    if yesterday_neg >= 50 or (ratio >= 5 and yesterday_neg >= 10):
        return {"level": "critical", "label": "紧急",
                "color": "#d4380d",
                "advice": "负面声量爆表，建议立即启动应急响应，2小时内出具说明口径"}
    if yesterday_neg >= 20 or (ratio >= 3 and yesterday_neg >= 5):
        return {"level": "high", "label": "严重",
                "color": "#fa541c",
                "advice": "负面明显抬头，建议今日内研判原因并准备回应"}
    if ratio >= 2 and yesterday_neg >= 5:
        return {"level": "medium", "label": "关注",
                "color": "#faad14",
                "advice": "负面有所上升，保持观察，暂无需应急"}
    return {"level": "normal", "label": "正常", "color": "#52c41a", "advice": ""}


def tiered_alerts(topic_id: Optional[int] = None) -> Dict:
    """分级预警：每个话题的预警等级 + 建议"""
    topics = ([db.get_topic(int(topic_id))] if topic_id
              else db.list_topics())
    items = []
    for t in topics:
        if not t:
            continue
        s = trends.spike_alert(t["id"])
        lv = alert_level(s["ratio"] if isinstance(s["ratio"], (int, float)) else 0,
                         s["yesterday_neg"])
        items.append({
            "topic_id": t["id"], "topic_name": t["name"],
            "yesterday": s["yesterday"],
            "yesterday_neg": s["yesterday_neg"],
            "avg7_neg": s["avg7_neg"], "ratio": s["ratio"],
            **lv,
        })
    # 按严重程度排序
    order = {"critical": 0, "high": 1, "medium": 2, "normal": 3}
    items.sort(key=lambda x: order[x["level"]])
    actives = [x for x in items if x["level"] != "normal"]
    return {"items": items, "has_alert": bool(actives),
            "summary": f"共 {len(items)} 个话题，"
                       f"{len([x for x in actives if x['level'] in ('critical','high')])} 个严重及以上"}


# ---------------- 事件聚类 ----------------

def _note_keywords(title: str, desc: str, top_n: int = 5) -> List[str]:
    try:
        import jieba.analyse
        text = f"{title} {desc}"
        words = jieba.analyse.extract_tags(text, topK=top_n)
        # 过滤太短/无意义的词
        return [w for w in words if len(w) >= 2][:top_n]
    except Exception:  # noqa: BLE001
        return []


def cluster_events(topic_id: Optional[int] = None, days: int = 14,
                   min_cluster: int = 2) -> Dict:
    """负面事件聚类：按共享关键词把近期负面帖子聚成事件。

    算法：取近 N 天负面帖子 → 每条提 top5 关键词 → 共享≥2 个关键词的归为一类
    → 输出事件列表（关键词/帖子数/代表标题/时间范围）。
    """
    since = int((datetime.now(TZ) - timedelta(days=days)).timestamp())
    page = db.list_posts(topic_id=topic_id, sentiment="negative", page=1,
                         page_size=500)
    notes = page.get("items", []) if isinstance(page, dict) else page
    notes = [n for n in notes if (n.get("publish_time") or 0) >= since]
    if not notes:
        return {"clusters": [], "total": 0,
                "message": "近 {} 天无负面帖子可聚类".format(days)}

    # 每条帖子的关键词集合
    kw_of = {}
    for n in notes:
        kws = set(_note_keywords(n.get("title") or "", n.get("desc") or ""))
        if kws:
            kw_of[n["post_id"]] = kws
    by_id = {n["post_id"]: n for n in notes}

    # 并查集合并：共享≥2关键词则同类
    parent = {nid: nid for nid in kw_of}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    ids = list(kw_of)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if len(kw_of[ids[i]] & kw_of[ids[j]]) >= 2:
                union(ids[i], ids[j])

    groups = defaultdict(list)
    for nid in ids:
        groups[find(nid)].append(nid)

    clusters = []
    for members in groups.values():
        if len(members) < min_cluster:
            continue
        all_kw = Counter()
        for m in members:
            all_kw.update(kw_of[m])
        top_kw = [w for w, _ in all_kw.most_common(4)]
        members_sorted = sorted(
            (by_id[m] for m in members),
            key=lambda n: n.get("publish_time") or 0, reverse=True)
        times = [n.get("publish_time") for n in members_sorted if n.get("publish_time")]
        clusters.append({
            "keywords": top_kw,
            "event_title": "、".join(top_kw[:3]),
            "count": len(members),
            "sample_titles": [(n.get("title") or "(无标题)")[:40]
                              for n in members_sorted[:3]],
            "sample_urls": [n.get("note_url") or "" for n in members_sorted[:3]],
            "earliest": (datetime.fromtimestamp(min(times), TZ).strftime("%Y-%m-%d")
                         if times else ""),
            "latest": (datetime.fromtimestamp(max(times), TZ).strftime("%Y-%m-%d")
                       if times else ""),
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return {"clusters": clusters, "total": len(notes),
            "message": f"近 {days} 天 {len(notes)} 条负面，聚出 {len(clusters)} 个事件"}


# ---------------- 竞品对标 ----------------

def compare_topics(topic_a: int, topic_b: int, days: int = 7) -> Dict:
    """竞品对标：两个话题并排对比（声量/情感/负面率/热词重合）"""
    ta, tb = db.get_topic(topic_a), db.get_topic(topic_b)
    if not ta or not tb:
        return {"ok": False, "error": "话题不存在"}

    def _stats(tid):
        tr = trends.get_trends(tid, days)
        vol = sum(d["count"] for d in tr["volume"])
        neg = sum(d["negative"] for d in tr["sentiment"])
        total = sum(d["negative"] + d["neutral"] + d["positive"]
                    for d in tr["sentiment"])
        words = [w["word"] for w in tr["hot_words"][:15]]
        return {"volume": vol, "negative": neg, "total": total,
                "neg_ratio": round(neg / total * 100, 1) if total else 0,
                "hot_words": words}

    sa, sb = _stats(topic_a), _stats(topic_b)
    overlap = [w for w in sa["hot_words"] if w in sb["hot_words"]][:10]
    return {
        "ok": True, "days": days,
        "a": {"topic_id": topic_a, "topic_name": ta["name"], **sa},
        "b": {"topic_id": topic_b, "topic_name": tb["name"], **sb},
        "hot_word_overlap": overlap,
        "verdict": _compare_verdict(ta["name"], sa, tb["name"], sb),
    }


def _compare_verdict(na, sa, nb, sb) -> str:
    parts = []
    if sa["volume"] != sb["volume"]:
        winner = na if sa["volume"] > sb["volume"] else nb
        parts.append(f"声量：{winner}更高（{max(sa['volume'], sb['volume'])} vs "
                     f"{min(sa['volume'], sb['volume'])}）")
    if sa["neg_ratio"] != sb["neg_ratio"]:
        worse = na if sa["neg_ratio"] > sb["neg_ratio"] else nb
        parts.append(f"负面率：{worse}更高（{sa['neg_ratio']}% vs {sb['neg_ratio']}%）")
    return "；".join(parts) if parts else "两话题声量与负面率接近"
