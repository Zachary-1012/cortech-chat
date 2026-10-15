# -*- coding: utf-8 -*-
"""对外 HTTP 客户端（共享）。

说明：httpx 解析 NO_PROXY 环境变量中的裸 IPv6 段（如 [fd8b:...::1]）会抛
InvalidURL，这里绕开 env 自动解析、只显式使用 http(s)_proxy。
"""
from __future__ import annotations

import os


def http_client(timeout: float = 20.0):
    import httpx

    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    return httpx.Client(trust_env=False, proxy=proxy or None, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; xhs-monitor/1.0)"})


def public_ip(timeout: float = 10.0) -> str | None:
    """查询本机公网出口 IP（ipify）。失败返回 None。"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with http_client(timeout) as c:
                r = c.get(url)
                if r.status_code == 200 and r.text.strip():
                    return r.text.strip().split()[0]
        except Exception:
            continue
    return None
