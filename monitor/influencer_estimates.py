# -*- coding: utf-8 -*-
"""达人预估：用已采集笔记自算曝光、阅读、互动率、CPE、CPM、报价、粉丝质量和更新节奏。

不调用千瓜、蒲公英或任何付费数据接口。数字全部来自本库笔记的
点赞 / 收藏 / 评论 / 分享和作者粉丝数。公式写在 FORMULAS 里，接口原样返回。

公开口径（2024–2025 年千瓜、蒲公英投放复盘里反复出现的区间，取中位）：
- 图文点赞约占阅读 2%–6%，中位 3.5%
- 收藏约占阅读 0.6%–2.0%，中位 1.2%
- 评论约占阅读 0.2%–1.0%，中位 0.5%
- 曝光到阅读的点击率约 40%–70%，中位 55%
- 粉丝口径互动率基准约 3%（中位达人近笔记互动 / 粉丝）
报价带是图文单篇的公开行情区间，再按互动率相对基准上下修，限制在 0.6–1.8 倍。
视频笔记占多数时再乘 1.25。网页端没有分享数时，分享不计入互动。
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from .db import _connect, apply_uid_filter

FORMULA_VERSION = "cortech-estimate-2026.1"

LIKE_PER_READ = 0.035
COLLECT_PER_READ = 0.012
COMMENT_PER_READ = 0.005
READ_PER_EXPOSURE = 0.55
BENCHMARK_ER = 0.03
QUOTE_FACTOR_MIN = 0.6
QUOTE_FACTOR_MAX = 1.8
VIDEO_QUOTE_FACTOR = 1.25
NOTE_WINDOW = 30

# (粉丝下限含, 上限不含, 图文单篇报价低, 报价高) 单位人民币
QUOTE_TIERS = (
    (0, 1_000, 200, 800),
    (1_000, 5_000, 500, 2_000),
    (5_000, 10_000, 800, 3_000),
    (10_000, 50_000, 2_000, 8_000),
    (50_000, 100_000, 5_000, 20_000),
    (100_000, 500_000, 15_000, 60_000),
    (500_000, 1_000_000, 40_000, 120_000),
    (1_000_000, 10**12, 80_000, 300_000),
)

CATEGORIES = (
    ("美妆", ("美妆", "口红", "护肤", "彩妆", "粉底", "防晒", "面膜")),
    ("时尚", ("穿搭", "时尚", "ootd", "服装", "女装", "男装")),
    ("美食", ("美食", "探店", "餐厅", "食谱", "咖啡", "甜品")),
    ("旅行", ("旅行", "旅游", "酒店", "景点", "机票")),
    ("母婴", ("母婴", "宝宝", "育儿", "孕期", "亲子")),
    ("家居", ("家居", "装修", "收纳", "家具", "软装")),
    ("数码", ("数码", "手机", "电脑", "测评", "耳机")),
    ("健身", ("健身", "运动", "瑜伽", "减肥", "跑步")),
    ("教育", ("教育", "学习", "考研", "留学", "课程")),
    ("情感", ("情感", "恋爱", "心理", "治愈")),
    ("本地生活", ("同城", "香港", "港岛", "本地", "周末")),
)

FORMULAS: Dict[str, Any] = {
    "version": FORMULA_VERSION,
    "sample": f"每个达人取最近 {NOTE_WINDOW} 条未删除笔记；没有笔记时只给粉丝档位报价，置信度为样本不足。",
    "reads": (
        f"单篇预估阅读 = 有数据的信号取中位数："
        f"点赞/{LIKE_PER_READ}、收藏/{COLLECT_PER_READ}、评论/{COMMENT_PER_READ}。"
        "达人阅读取这些单篇阅读的中位数。"
    ),
    "exposure": (
        f"预估曝光 = 预估阅读 / {READ_PER_EXPOSURE}。"
        "0.55 是曝光到阅读点击率的公开区间中位。"
    ),
    "engagement": "单篇互动 = 点赞 + 收藏 + 评论 +（仅当网页端返回了分享数时加上分享）。",
    "engagement_rate": (
        f"互动率 = 单篇互动中位数 / 粉丝数。基准 {BENCHMARK_ER:.0%}，"
        "用来修正报价，不拿它冒充平台后台的阅读率。"
    ),
    "quote": (
        "报价区间先查粉丝档位（图文单篇公开行情），"
        f"再乘以 clamp(互动率/{BENCHMARK_ER}, {QUOTE_FACTOR_MIN}, {QUOTE_FACTOR_MAX})。"
        f"视频笔记占多数时再乘 {VIDEO_QUOTE_FACTOR}。没有粉丝数时不修正，置信度下降。"
    ),
    "cpe": "CPE = 报价中位 / 单篇互动中位数。互动为 0 时不计算。",
    "cpm": "CPM = 报价中位 / (预估曝光中位数 / 1000)。曝光为 0 时不计算。",
    "fan_quality": (
        "粉丝质量分 = 100 - 可疑粉丝分。可疑分来自已采集笔记："
        "粉丝过万且互动率低于 0.2%（+30）；"
        "赞评比超过 80 或有赞无评（+25）；"
        "至少 3 篇时单篇互动占全部 85% 以上（+15）；"
        "粉丝 5 万以上但没有任何可核验笔记（+20）。"
        "这不是付费的粉丝画像接口，只标出笔记对不上粉丝量的情况。"
    ),
    "category": "内容类目 = 笔记标题、正文、标签和达人领域标签在本地类目词表里的命中次数。",
    "cadence": (
        "发布节奏 = 样本笔记发布时间的间隔中位数。"
        "≤1.5 天日更，≤4 天高频，≤8 天周更，≤16 天半月更，≤35 天月更，否则低频。"
    ),
    "confidence": (
        "置信度：≥8 篇高（0.82），≥3 篇中（0.58），≥1 篇低（0.36），0 篇样本不足（0.15）。"
        "网页端缺分享数再减 0.04，没有粉丝数再减 0.08。结果限制在 0.05–0.95。"
    ),
}


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def note_engagement(note: Dict[str, Any]) -> int:
    shares = _i(note.get("share_count")) if note.get("share_count_available") else 0
    return (_i(note.get("liked_count")) + _i(note.get("collected_count"))
            + _i(note.get("comment_count")) + shares)


def estimate_reads(note: Dict[str, Any]) -> float:
    signals: List[float] = []
    likes = _i(note.get("liked_count"))
    collects = _i(note.get("collected_count"))
    comments = _i(note.get("comment_count"))
    if likes > 0:
        signals.append(likes / LIKE_PER_READ)
    if collects > 0:
        signals.append(collects / COLLECT_PER_READ)
    if comments > 0:
        signals.append(comments / COMMENT_PER_READ)
    return _median(signals)


def _quote_tier(fans: int) -> tuple:
    fans = max(0, fans)
    for lo, hi, low, high in QUOTE_TIERS:
        if lo <= fans < hi:
            return low, high
    return QUOTE_TIERS[-1][2], QUOTE_TIERS[-1][3]


def _categories(author: Dict[str, Any], notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blob_parts: List[str] = []
    tags = author.get("domain_tags") or []
    if isinstance(tags, list):
        blob_parts.extend(str(t) for t in tags)
    for note in notes:
        blob_parts.append(str(note.get("title") or ""))
        blob_parts.append(str(note.get("desc") or ""))
        blob_parts.append(str(note.get("tags") or ""))
    blob = " ".join(blob_parts).lower()
    scored = []
    for name, words in CATEGORIES:
        hits = sum(blob.count(word.lower()) for word in words)
        if hits:
            scored.append({"category": name, "hits": hits})
    scored.sort(key=lambda row: row["hits"], reverse=True)
    return scored


def _cadence(notes: List[Dict[str, Any]]) -> Dict[str, Any]:
    times = sorted(_i(n.get("publish_time")) for n in notes if _i(n.get("publish_time")) > 0)
    if len(times) < 2:
        return {"label": "样本不足", "median_gap_days": None, "posts_per_week": None,
                "sample_notes": len(times)}
    gaps = [times[i] - times[i - 1] for i in range(1, len(times))]
    median_days = _median(gaps) / 86400
    span_days = max((times[-1] - times[0]) / 86400, 1)
    per_week = round(len(times) / span_days * 7, 2)
    if median_days <= 1.5:
        label = "日更"
    elif median_days <= 4:
        label = "高频"
    elif median_days <= 8:
        label = "周更"
    elif median_days <= 16:
        label = "半月更"
    elif median_days <= 35:
        label = "月更"
    else:
        label = "低频"
    return {"label": label, "median_gap_days": round(median_days, 1),
            "posts_per_week": per_week, "sample_notes": len(times)}


def _fan_quality(author: Dict[str, Any], notes: List[Dict[str, Any]],
                 engagement_rate: Optional[float]) -> Dict[str, Any]:
    fans = _i(author.get("fans_count"))
    suspicious = 0
    reasons: List[str] = []
    if fans >= 10_000 and engagement_rate is not None and engagement_rate < 0.002:
        suspicious += 30
        reasons.append("粉丝过万，但笔记互动率低于 0.2%")
    likes = sum(_i(n.get("liked_count")) for n in notes)
    comments = sum(_i(n.get("comment_count")) for n in notes)
    if likes >= 200 and comments == 0:
        suspicious += 25
        reasons.append("有点赞但没有评论")
    elif likes >= 200 and comments > 0 and likes / comments > 80:
        suspicious += 25
        reasons.append("赞评比超过 80")
    engs = [note_engagement(n) for n in notes]
    total = sum(engs)
    if len(engs) >= 3 and total > 0 and max(engs) / total > 0.85:
        suspicious += 15
        reasons.append("互动集中在单篇")
    if fans >= 50_000 and _i(author.get("notes_count")) == 0 and not notes:
        suspicious += 20
        reasons.append("粉丝超过 5 万，但没有可核验笔记")
    suspicious = min(100, suspicious)
    return {"fan_quality": 100 - suspicious, "suspicious_fan_score": suspicious,
            "reasons": reasons}


def _confidence(notes: List[Dict[str, Any]], fans: int) -> Dict[str, Any]:
    n = len(notes)
    if n >= 8:
        level, score = "high", 0.82
    elif n >= 3:
        level, score = "medium", 0.58
    elif n >= 1:
        level, score = "low", 0.36
    else:
        level, score = "insufficient", 0.15
    if notes and any(not n.get("share_count_available") for n in notes):
        score -= 0.04
    if fans <= 0:
        score -= 0.08
    score = round(max(0.05, min(0.95, score)), 2)
    labels = {"high": "高", "medium": "中", "low": "低", "insufficient": "样本不足"}
    return {"level": level, "label": labels[level], "score": score, "sample_notes": n}


def estimate_from_notes(author: Dict[str, Any],
                        notes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """纯函数。notes 已是该达人要纳入的样本。"""
    fans = _i(author.get("fans_count"))
    reads = [estimate_reads(n) for n in notes]
    reads = [v for v in reads if v > 0]
    engs = [float(note_engagement(n)) for n in notes]
    median_reads = _median(reads)
    median_eng = _median([v for v in engs if v > 0]) if any(engs) else 0.0
    median_exposure = median_reads / READ_PER_EXPOSURE if median_reads else 0.0
    er = (median_eng / fans) if fans > 0 and median_eng else None
    low, high = _quote_tier(fans)
    if er is None:
        factor = 1.0
        factor_note = "没有粉丝数或没有互动，报价不按互动率修正"
    else:
        factor = min(QUOTE_FACTOR_MAX, max(QUOTE_FACTOR_MIN, er / BENCHMARK_ER))
        factor_note = f"互动率 {er:.2%} / 基准 {BENCHMARK_ER:.0%} = {factor:.2f}"
    video_n = sum(1 for n in notes if (n.get("media_type") or "") == "video")
    video_factor = VIDEO_QUOTE_FACTOR if notes and video_n * 2 >= len(notes) else 1.0
    quote_low = int(round(low * factor * video_factor))
    quote_high = int(round(high * factor * video_factor))
    quote_mid = int(round((quote_low + quote_high) / 2))
    cpe = round(quote_mid / median_eng, 2) if median_eng else None
    cpm = round(quote_mid / (median_exposure / 1000), 2) if median_exposure else None
    cats = _categories(author, notes)
    quality = _fan_quality(author, notes, er)
    return {
        "formula_version": FORMULA_VERSION,
        "estimated_reads": int(round(median_reads)),
        "estimated_exposure": int(round(median_exposure)),
        "median_engagement": int(round(median_eng)),
        "engagement_rate": round(er, 4) if er is not None else None,
        "engagement_rate_pct": (f"{er:.2%}" if er is not None else None),
        "cpe": cpe,
        "cpm": cpm,
        "quote": {
            "low": quote_low,
            "high": quote_high,
            "mid": quote_mid,
            "currency": "CNY",
            "unit": "图文单篇" if video_factor == 1 else "视频单篇（按视频系数）",
            "factor": round(factor, 2),
            "video_factor": video_factor,
            "factor_note": factor_note,
        },
        "fan_quality": quality["fan_quality"],
        "suspicious_fan_score": quality["suspicious_fan_score"],
        "fan_quality_reasons": quality["reasons"],
        "category": cats[0]["category"] if cats else "未分类",
        "categories": cats[:5],
        "cadence": _cadence(notes),
        "confidence": _confidence(notes, fans),
        "inputs": {
            "fans_count": fans,
            "sample_notes": len(notes),
            "like_per_read": LIKE_PER_READ,
            "collect_per_read": COLLECT_PER_READ,
            "comment_per_read": COMMENT_PER_READ,
            "read_per_exposure": READ_PER_EXPOSURE,
        },
    }


def _load_notes(author_ids: List[str], region: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    if not author_ids:
        return {}
    where = [f"author_id IN ({','.join('?' * len(author_ids))})",
             "IFNULL(post_status,'active')!='deleted'"]
    params: List[Any] = list(author_ids)
    if region:
        from .xhs_region import normalize_region
        where.append("IFNULL(region,'cn')=?")
        params.append(normalize_region(region))
    apply_uid_filter(where, params)
    sql = (
        "SELECT author_id, title, desc, tags, media_type, publish_time, region,"
        " liked_count, collected_count, comment_count, share_count, share_count_available"
        f" FROM notes WHERE {' AND '.join(where)}"
        " ORDER BY publish_time DESC, id DESC"
    )
    grouped: Dict[str, List[Dict[str, Any]]] = {aid: [] for aid in author_ids}
    with _connect() as conn:
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["share_count_available"] = bool(item.get("share_count_available"))
            bucket = grouped.setdefault(item["author_id"], [])
            if len(bucket) < NOTE_WINDOW:
                bucket.append(item)
    return grouped


def estimates_for(author_ids: List[str],
                  authors: Optional[Dict[str, Dict[str, Any]]] = None,
                  region: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    authors = authors or {}
    notes = _load_notes(author_ids, region=region)
    out: Dict[str, Dict[str, Any]] = {}
    for aid in author_ids:
        out[aid] = estimate_from_notes(authors.get(aid) or {"author_id": aid},
                                       notes.get(aid) or [])
    return out


def estimate_author(author_id: str, author: Optional[Dict[str, Any]] = None,
                    region: Optional[str] = None) -> Dict[str, Any]:
    found = estimates_for([author_id],
                          {author_id: author} if author else None,
                          region=region)
    est = found.get(author_id) or estimate_from_notes(author or {}, [])
    est["formulas"] = FORMULAS
    return est
