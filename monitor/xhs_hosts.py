# -*- coding: utf-8 -*-
"""小红书主机与 Cookie 域名。

只覆盖本仓库已经在用的域名，不推测没有出现过的接口主机。

大陆和香港打开网页时，通常都是 www.xiaohongshu.com，Cookie 域名是
.xiaohongshu.com。浏览器里如果另外存了 .xiaohongshu.cn 或 .xiaohongshu.hk
的登录 Cookie，导入和采集也会读。签名请求仍然打到 edith.xiaohongshu.com。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

# 网页 Origin / Referer。采集器请求头一直用这个。
WEB_ORIGIN = "https://www.xiaohongshu.com"
WEB_REFERER = "https://www.xiaohongshu.com/"

# 网页 direct-API。与采集器现有端点一致。
API_ORIGIN = "https://edith.xiaohongshu.com"

# 读浏览器 Cookie 时要问的地址。
# edith 上的 host-only Cookie 不会出现在只查询 www 的结果里。
COOKIE_READ_URLS = (
    "https://www.xiaohongshu.com",
    "https://www.xiaohongshu.cn",
    "https://www.xiaohongshu.hk",
    "https://edith.xiaohongshu.com",
    "https://www.rednote.com",
    "https://webapi.rednote.com",
)

# 这些注册域上的 Cookie 才算小红书登录态。
COOKIE_HOST_MARKERS = (
    "xiaohongshu.com",
    "xiaohongshu.cn",
    "xiaohongshu.hk",
    "rednote.com",
)


def host_is_xhs(host: str) -> bool:
    """host / domain / host_key 是否属于上面列出的小红书域。"""
    text = (host or "").lower().strip().lstrip(".")
    if not text:
        return False
    return any(text == marker or text.endswith("." + marker) for marker in COOKIE_HOST_MARKERS)


def cookie_preference(domain: str) -> int:
    """同名 Cookie 时 .com 优先于 .cn、.hk、rednote。默认大陆采集用这个顺序。"""
    return cookie_preference_for_region(domain, "cn")


def cookie_preference_for_region(domain: str, region: str = "cn") -> int:
    """按采集地区决定同名 Cookie 的优先级。

    大陆：.com > .cn > .hk > rednote（与原来一致）。
    香港：.hk > .com > .cn > rednote。
    海外 RED：rednote > .com。登录态不跨站共用，所以 rednote 必须压过 .com。
    """
    text = (domain or "").lower()
    area = (region or "cn").strip().lower()
    if area in ("hk", "hongkong", "香港"):
        if "xiaohongshu.hk" in text:
            return 5
        if "xiaohongshu.com" in text:
            return 4
        if "xiaohongshu.cn" in text:
            return 2
        if "rednote.com" in text:
            return 0
        return -1
    if area in ("overseas", "rednote", "global", "海外"):
        if "rednote.com" in text:
            return 5
        if "xiaohongshu.com" in text:
            return 2
        if "xiaohongshu.hk" in text:
            return 1
        if "xiaohongshu.cn" in text:
            return 1
        return -1
    if "xiaohongshu.com" in text:
        return 3
    if "xiaohongshu.cn" in text:
        return 2
    if "xiaohongshu.hk" in text:
        return 1
    if "rednote.com" in text:
        return 0
    return -1


def merge_cookie_rows(rows: Iterable[Dict[str, Any]],
                      region: str = "cn") -> List[Dict[str, Any]]:
    """合并多域 Cookie。空域名保留；其它域名必须是小红书。同名取偏好更高的。"""
    best: Dict[str, tuple] = {}
    order: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not name:
            continue
        domain = str(row.get("domain") or row.get("host_key") or "")
        if domain and not host_is_xhs(domain):
            continue
        rank = cookie_preference_for_region(domain, region) if domain else 3
        prev = best.get(name)
        if prev is None:
            order.append(name)
            best[name] = (rank, row)
        elif rank >= prev[0]:
            best[name] = (rank, row)
    return [best[name][1] for name in order]


async def collect_context_cookies(ctx, region: str = "cn") -> List[Dict[str, Any]]:
    """从 Playwright 上下文读取 .com / .cn / .hk / edith / rednote 上会带上的 Cookie。"""
    rows: List[Dict[str, Any]] = []
    try:
        got = await ctx.cookies(list(COOKIE_READ_URLS))
        if got:
            rows.extend(got)
    except Exception:  # noqa: BLE001
        for url in COOKIE_READ_URLS:
            try:
                got = await ctx.cookies(url)
            except Exception:  # noqa: BLE001
                continue
            if got:
                rows.extend(got)
    return merge_cookie_rows(rows, region=region)
