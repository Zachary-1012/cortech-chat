# -*- coding: utf-8 -*-
"""全网资讯采集：NewsItem 统一模型 + 采集器接口（adapter 化）。

v1 两种采集器：
  - googlenews_rss (kind="search"): Google News RSS 关键词搜索（免费、无需 key）
  - rss           (kind="rss")   : 通用 RSS 订阅（feedparser）

去重：按 URL 精确去重 + 标题相似度（difflib >= 0.85）去重。
情感：复用 monitor.sentiment.analyze。
版权合规：只收录标题/来源/时间/链接 + 概括性摘要，不整段复制原文。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("xhs-monitor.news")


@dataclass
class NewsItem:
    url: str
    title: str = ""
    source: str = ""            # 来源媒体
    publish_time: Optional[int] = None  # 媒体发布时间（北京时间秒）
    publish_time_raw: str = ""  # 原始时间字符串备查
    summary: str = ""           # 概括性摘要（转述，不复制原文）
    keyword: str = ""           # 命中关键词

    def dedup_key(self) -> str:
        return self.url.strip()


class NewsAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self, keywords: List[str], url: str = "",
                max_items: int = 50) -> List[NewsItem]:
        """抓取一批资讯。keywords：逗号分隔关键词；url：rss 订阅地址（kind=rss 用）。"""


_REGISTRY: Dict[str, NewsAdapter] = {}


def register(adapter: NewsAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def get(name: str) -> NewsAdapter:
    if name not in _REGISTRY:
        raise KeyError(f"未知资讯采集器: {name}（已注册：{sorted(_REGISTRY)}）")
    return _REGISTRY[name]


def available() -> List[str]:
    return sorted(_REGISTRY)


def import_all() -> None:
    from . import googlenews_rss, rss  # noqa: F401  (注册副作用)


# 兼容：旧 import 路径 monitor.news.base.http_client 仍可用
from ..netutil import http_client  # noqa: E402,F401
