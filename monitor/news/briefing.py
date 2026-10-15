# -*- coding: utf-8 -*-
"""monitor/news/briefing.py — 简报「全网资讯动态」一节的数据接口。

主构建者接线方式（monitor/reports.py）：
    from .news import briefing as news_briefing
    ...
    "web_news": news_briefing.briefing_section(days),
"""
from __future__ import annotations

from typing import Dict, List

from .. import db

_MAX_ITEMS = 200  # 单节聚合上限，7/14 天窗口足够


def briefing_section(days: int = 7) -> Dict:
    """返回「全网资讯动态」一节的数据。时间口径：发布时间（publish_time）。"""
    res = db.list_news(days=days, page=1, page_size=_MAX_ITEMS)
    items: List[Dict] = res["items"]
    total: int = res["total"]

    by_sentiment = {"negative": 0, "neutral": 0, "positive": 0}
    by_source: Dict[str, int] = {}
    for it in items:
        s = it.get("sentiment") or "neutral"
        by_sentiment[s] = by_sentiment.get(s, 0) + 1
        src = (it.get("source") or "").strip() or "未知来源"
        by_source[src] = by_source.get(src, 0) + 1

    top_sources = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
    negatives = [it for it in items if it.get("sentiment") == "negative"][:5]
    featured = negatives or items[:5]
    top_items = [{
        "title": it["title"],
        "source": it.get("source") or "",
        "publish_time": it.get("publish_time"),
        "publish_time_raw": it.get("publish_time_raw") or "",
        "url": it["url"],
        "summary": (it.get("summary") or "")[:120],
        "sentiment": it.get("sentiment") or "neutral",
    } for it in featured]

    if total:
        src_text = "、".join(f"{s}({c})" for s, c in top_sources)
        text = (f"近{days}天全网资讯 {total} 条，其中负面 {by_sentiment['negative']} 条、"
                f"正面 {by_sentiment['positive']} 条；主要来源：{src_text}。")
    else:
        text = f"近{days}天暂无全网资讯数据。"
    return {
        "total": total,
        "by_sentiment": by_sentiment,
        "top_sources": [{"source": s, "count": c} for s, c in top_sources],
        "top_items": top_items,
        "text": text,
    }
