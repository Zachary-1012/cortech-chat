# -*- coding: utf-8 -*-
"""monitor/news/rss.py — 通用 RSS 订阅采集器（kind="rss"）。

url 字段为 RSS 地址（多个用换行分隔）。用 feedparser 解析，
title/summary/link/published/author，摘要只做概括转述（截断 200 字）。
"""
from __future__ import annotations

import logging
import time
from typing import List

from .base import NewsAdapter, NewsItem, register

logger = logging.getLogger("xhs-monitor.news.rss")


class GenericRSSAdapter(NewsAdapter):
    name = "rss"

    def collect(self, keywords: List[str], url: str = "",
                max_items: int = 50) -> List[NewsItem]:
        import feedparser
        items: List[NewsItem] = []
        for feed_url in [u.strip() for u in url.splitlines() if u.strip()]:
            try:
                fp = feedparser.parse(feed_url)
            except Exception as e:
                logger.warning("RSS 拉取失败 url=%r: %s", feed_url, e)
                continue
            src_name = getattr(fp.feed, "title", "") or ""
            for e in fp.entries[:max_items]:
                link = getattr(e, "link", "") or ""
                if not link:
                    continue
                title = getattr(e, "title", "") or ""
                raw_summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                raw_pub = getattr(e, "published", "") or getattr(e, "updated", "") or ""
                pt = None
                if getattr(e, "published_parsed", None):
                    pt = int(time.mktime(e.published_parsed))
                elif getattr(e, "updated_parsed", None):
                    pt = int(time.mktime(e.updated_parsed))
                author = getattr(e, "author", "") or ""
                hit = [k for k in keywords if k.strip() and k.strip() in
                       (title + raw_summary)]
                items.append(NewsItem(
                    url=link.strip(),
                    title=title.strip(),
                    source=author.strip() or src_name.strip(),
                    publish_time=pt,
                    publish_time_raw=raw_pub.strip(),
                    summary=(raw_summary.strip()[:200]),
                    keyword=hit[0] if hit else "",
                ))
        return items[:max_items]


register(GenericRSSAdapter())
