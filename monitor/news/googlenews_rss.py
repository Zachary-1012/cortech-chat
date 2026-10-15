# -*- coding: utf-8 -*-
"""monitor/news/googlenews_rss.py — Google News RSS 关键词搜索采集器（kind="search"）。

免费、无需 key。端点 https://news.google.com/rss/search?q=...&hl=zh-CN&gl=CN&ceid=CN:zh
返回 RSS：title（含"标题 - 媒体名"）、link（Google News 文章页）、pubDate、<source> 媒体名。
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import List

from .base import NewsAdapter, NewsItem, register

logger = logging.getLogger("xhs-monitor.news.search")

_GN_BASE = "https://news.google.com/rss/search"

# 系统 CA 证书候选路径：默认 certifi 校验失败时（如公司代理 MITM 环境）回退
_SYSTEM_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL/CentOS
    "/etc/ssl/cert.pem",                   # macOS
)


def _system_ca() -> str | None:
    import os
    for p in _SYSTEM_CA_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _fetch_text(url: str) -> str:
    """拉取 RSS 文本。

    - 跟随跳转（Google News 偶发 302 consent 跳转）。
    - SSL 校验失败时回退到系统 CA（兼容公司代理 MITM 环境）。
    """
    import os
    import ssl
    import httpx

    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    last_err: Exception | None = None
    for verify in (True, _system_ca()):
        if verify is None:
            continue
        try:
            with httpx.Client(trust_env=False, proxy=proxy or None, timeout=20.0,
                              verify=verify,
                              headers={"User-Agent":
                                       "Mozilla/5.0 (compatible; xhs-monitor/1.0)"}) as client:
                resp = client.get(url, follow_redirects=True)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            if isinstance(e, ssl.SSLCertVerificationError) or \
                    "CERTIFICATE_VERIFY_FAILED" in str(e):
                last_err = e
                continue
            raise
    raise last_err  # type: ignore[misc]


def _parse_pubdate(raw: str) -> int | None:
    """'Mon, 14 Oct 2026 05:00:00 GMT' -> UTC 秒；失败返回 None"""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
        return int(dt.timestamp())
    except Exception:
        return None


class GoogleNewsRSSAdapter(NewsAdapter):
    name = "googlenews_rss"

    def collect(self, keywords: List[str], url: str = "",
                max_items: int = 50) -> List[NewsItem]:
        items: List[NewsItem] = []
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            q = urllib.parse.urlencode(
                {"q": kw, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh"})
            try:
                text = _fetch_text(f"{_GN_BASE}?{q}")
            except Exception as e:
                logger.warning("GoogleNews RSS 请求失败 kw=%r: %s", kw, e)
                continue
            items.extend(self._parse(text, kw))
            if len(items) >= max_items:
                break
        return items[:max_items]

    def _parse(self, xml_text: str, kw: str) -> List[NewsItem]:
        import re
        import xml.etree.ElementTree as ET
        out: List[NewsItem] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning("RSS 解析失败 kw=%r: %s", kw, e)
            return out
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            src_el = item.find("source")
            if link_el is None or not link_el.text:
                continue
            raw_title = (title_el.text or "") if title_el is not None else ""
            # "标题 - 媒体名" 拆出媒体名
            media = (src_el.text or "").strip() if src_el is not None else ""
            title = raw_title
            if media and raw_title.endswith(" - " + media):
                title = raw_title[: -(len(media) + 3)].strip()
            pub_raw = (pub_el.text or "").strip() if pub_el is not None else ""
            out.append(NewsItem(
                url=link_el.text.strip(),
                title=title,
                source=media,
                publish_time=_parse_pubdate(pub_raw),
                publish_time_raw=pub_raw,
                summary="",
                keyword=kw,
            ))
        return out


register(GoogleNewsRSSAdapter())
