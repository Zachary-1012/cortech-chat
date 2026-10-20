"""话题时间线：按发布时间分档的趋势分析（方案A：知微式三层）。

三层：趋势图 → 点档位看帖子列表（发布时间升序）→ 点帖子看详情。
粒度自适应：跨度 <30 天按天，30-90 天按周，>90 天按月；可手动切换。
无发布时间的帖子单独放「时间未知」档，不混入时间轴。

长尾处理（对标 trends.py / influence.py 的对数标准化思路）：
- 每档统计同时给平均互动和中位数互动（避免头部拉高误导）
- 帖子按互动量分三档：头部（Top 20%）、腰部（中间 30%）、尾部（后 50%）
  分档用分位数，不用固定阈值，适配不同量级话题
"""

import datetime
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from . import db


def _engagement(p: Dict[str, Any]) -> int:
    """帖子互动量：点赞+收藏+评论。"""
    return (p.get("liked_count") or 0) + (p.get("collected_count") or 0) + (p.get("comment_count") or 0)


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return float(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2)


def _bucket_key(ts: int, granularity: str) -> str:
    """返回档位标识。"""
    dt = datetime.datetime.fromtimestamp(ts)
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    # month
    return dt.strftime("%Y-%m")


def _bucket_label(key: str, granularity: str) -> str:
    """档位显示标签。"""
    if granularity == "week":
        return f"{key} 周"
    if granularity == "month":
        return key.replace("-", "年") + "月"
    return key


def _classify_tiers(posts: List[Dict[str, Any]]) -> None:
    """按互动量分位数给帖子打 tier 标签：head（头部 Top20%）/ mid（腰部）/ tail（尾部后50%）。

    就地修改 posts 列表，每个帖子加 "tier" 和 "engagement" 字段。
    """
    if not posts:
        return
    engs = sorted(_engagement(p) for p in posts)
    n = len(engs)
    # 分位数阈值：P50 和 P80
    p50 = engs[int(n * 0.5)] if n > 1 else engs[0]
    p80 = engs[int(n * 0.8)] if n > 1 else engs[0]
    for p in posts:
        e = _engagement(p)
        p["engagement"] = e
        if e >= p80 and n > 2:
            p["tier"] = "head"
        elif e >= p50:
            p["tier"] = "mid"
        else:
            p["tier"] = "tail"


def get_timeline(topic_id: int, granularity: str = "auto",
                 region: Optional[str] = None) -> Dict[str, Any]:
    """话题时间线。

    返回：
    {
        "granularity": "week",           # 实际使用的粒度
        "span_days": 93,                 # 有时间数据的跨度天数
        "buckets": [                     # 按时间从早到晚
            {
                "key": "2026-W40", "label": "2026-W40 周",
                "start": 1727654400, "end": 1728259199,
                "count": 5, "negative_count": 2, "is_peak": True,
                "avg_engagement": 120.5,      # 平均互动（会被头部拉高）
                "median_engagement": 45.0,    # 中位数互动（长尾友好）
                "tier_counts": {"head": 1, "mid": 2, "tail": 2},
                "posts": [ {id, title, ..., tier, engagement}, ... ]  # 发布时间升序
            }, ...
        ],
        "unknown": {                     # 无发布时间
            "count": 58,
            "posts": [ ... ]
        }
    }
    """
    if granularity not in ("auto", "day", "week", "month"):
        granularity = "auto"

    # 取该话题全部帖子（不分页）
    res = db.list_posts(topic_id=topic_id, page=1, page_size=100000, region=region)
    notes = res.get("items", [])

    timed: List[Dict[str, Any]] = []
    untimed: List[Dict[str, Any]] = []
    for n in notes:
        if n.get("publish_time"):
            timed.append(n)
        else:
            untimed.append(n)

    # 自适应粒度
    effective = granularity
    span_days = 0
    if timed:
        times = sorted(n["publish_time"] for n in timed)
        span_days = (times[-1] - times[0]) // 86400
        if granularity == "auto":
            if span_days < 30:
                effective = "day"
            elif span_days <= 90:
                effective = "week"
            else:
                effective = "month"

    # 全话题先做一次 tier 分档（跨档可比）
    _classify_tiers(timed)
    _classify_tiers(untimed)

    # 分档
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in timed:
        buckets[_bucket_key(n["publish_time"], effective)].append(n)

    def _post_dict(p: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": p.get("id"),
            "post_id": p.get("post_id"),
            "title": p.get("title") or "(无标题)",
            "author": p.get("author") or "",
            "publish_time": p.get("publish_time"),
            "liked_count": p.get("liked_count") or 0,
            "collected_count": p.get("collected_count") or 0,
            "comment_count": p.get("comment_count") or 0,
            "engagement": p.get("engagement") or 0,
            "tier": p.get("tier") or "tail",
            "sentiment": p.get("sentiment") or "neutral",
            "note_url": p.get("note_url") or "",
        }

    result_buckets = []
    for key in sorted(buckets.keys()):
        posts = sorted(buckets[key], key=lambda x: x.get("publish_time") or 0)
        neg = sum(1 for p in posts if p.get("sentiment") == "negative")
        times = [p["publish_time"] for p in posts]
        engs = [_engagement(p) for p in posts]
        tier_counts = {"head": 0, "mid": 0, "tail": 0}
        for p in posts:
            tier_counts[p.get("tier") or "tail"] += 1
        result_buckets.append({
            "key": key,
            "label": _bucket_label(key, effective),
            "start": min(times),
            "end": max(times),
            "count": len(posts),
            "negative_count": neg,
            "is_peak": False,
            "avg_engagement": round(sum(engs) / len(engs), 1) if engs else 0,
            "median_engagement": round(_median([float(e) for e in engs]), 1),
            "tier_counts": tier_counts,
            "posts": [_post_dict(p) for p in posts],
        })

    # 峰值标记：帖子数最多的档
    if result_buckets:
        peak = max(result_buckets, key=lambda b: b["count"])
        peak["is_peak"] = True

    return {
        "topic_id": topic_id,
        "granularity": effective,
        "span_days": span_days,
        "total": len(notes),
        "timed_count": len(timed),
        "buckets": result_buckets,
        "unknown": {
            "count": len(untimed),
            "posts": [_post_dict(p) for p in untimed],
        },
    }
