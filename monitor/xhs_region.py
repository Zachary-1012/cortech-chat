# -*- coding: utf-8 -*-
"""港版 / 海外小红书：地区、请求头、主机，以及 OpenCC 繁简关键词。

研究结论（2026-10，对照公开网页与开源采集器，不调用付费接口）：

大陆
- 网页 https://www.xiaohongshu.com
- 签名 API https://edith.xiaohongshu.com
- Cookie 根域 .xiaohongshu.com（浏览器里也可能有 .xiaohongshu.cn）
- Accept-Language: zh-CN,zh;q=0.9
- 内容以简体中文为主
- 会话要有 a1 和 web_session

香港
- 没有仍在提供检索的独立港版站点。香港打开的官方网页仍是
  www.xiaohongshu.com。
- 部分浏览器会留下 .xiaohongshu.hk 的 Cookie。选「香港」时同名 Cookie
  优先 .hk，其次 .com。签名请求仍发到 edith.xiaohongshu.com：
  不存在可用的 edith.xiaohongshu.hk 检索主机。
- 港版差别主要在语言。App 提供繁体中文。采集请求带
  Accept-Language: zh-HK,zh-Hant;q=0.9,zh;q=0.8,en;q=0.6
- 关键词用 OpenCC 的香港用字（s2hk / hk2s）互转，例如「软件」↔「軟件」，
  而不是台湾用字「軟體」。
- 会话模型和大陆相同，仍然只读搜索，不发笔记。

海外 / RED（Rednote）
- 非中国大陆网络访问 www.xiaohongshu.com 会被带到 www.rednote.com。
- 网页 https://www.rednote.com
- API https://webapi.rednote.com（路径与大陆 /api/sns/web/v1/search/notes 相同）
- 静态与签名脚本域 https://as.rednote.com（对应大陆 fe-static.xhscdn.com）
- Cookie 根域 .rednote.com，和 .xiaohongshu.com 不共用登录态
- 海外会话常见 a1，不一定有 web_session
- Accept-Language: en,zh-HK;q=0.8,zh-Hant;q=0.7,zh;q=0.5
- 内容以繁体中文和英文为主，关键词同样做繁简扩展

采集仍只使用当前用户已经保存、并且通过健康检查和每日配额的账号
（monitor/account_rotation.py）。不登录别人的号，不对小红书做写操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

REGIONS = ("cn", "hk", "overseas")

# 简繁对照里只取字形不同的字，用来给笔记标 hans / hant / mixed。
_PAIRS = (
    ("国", "國"), ("门", "門"), ("书", "書"), ("体", "體"),
    ("后", "後"), ("发", "發"), ("对", "對"), ("开", "開"),
    ("关", "關"), ("东", "東"), ("车", "車"), ("广", "廣"),
    ("汇", "匯"), ("湾", "灣"), ("与", "與"), ("专", "專"),
    ("页", "頁"), ("际", "際"), ("软", "軟"), ("这", "這"),
    ("们", "們"), ("为", "為"), ("从", "從"), ("时", "時"),
    ("会", "會"), ("说", "說"), ("现", "現"), ("当", "當"),
    ("点", "點"), ("经", "經"), ("过", "過"), ("还", "還"),
    ("头", "頭"), ("问", "問"), ("题", "題"), ("产", "產"),
    ("学", "學"), ("习", "習"), ("长", "長"), ("电", "電"),
    ("话", "話"), ("风", "風"), ("云", "雲"), ("买", "買"),
    ("卖", "賣"), ("鸡", "雞"), ("个", "個"), ("来", "來"),
    ("观", "觀"), ("众", "眾"), ("艺", "藝"), ("医", "醫"),
    ("务", "務"), ("场", "場"), ("网", "網"), ("红", "紅"),
    ("里", "裡"), ("台", "臺"), ("面", "麵"), ("只", "隻"),
)
_HANS = {a for a, _ in _PAIRS}
_HANT = {b for _, b in _PAIRS}

_OPENCC = None


@dataclass(frozen=True)
class RegionProfile:
    id: str
    label: str
    web_origin: str
    api_origin: str
    accept_language: str
    requires_web_session: bool
    signing_asset_host: str
    summary: str


_PROFILES: Dict[str, RegionProfile] = {
    "cn": RegionProfile(
        id="cn",
        label="大陆",
        web_origin="https://www.xiaohongshu.com",
        api_origin="https://edith.xiaohongshu.com",
        accept_language="zh-CN,zh;q=0.9",
        requires_web_session=True,
        signing_asset_host="https://fe-static.xhscdn.com",
        summary="简体内容。网页 www.xiaohongshu.com，签名 API edith.xiaohongshu.com。",
    ),
    "hk": RegionProfile(
        id="hk",
        label="香港",
        web_origin="https://www.xiaohongshu.com",
        api_origin="https://edith.xiaohongshu.com",
        accept_language="zh-HK,zh-Hant;q=0.9,zh;q=0.8,en;q=0.6",
        requires_web_session=True,
        signing_asset_host="https://fe-static.xhscdn.com",
        summary=("香港仍打开 www.xiaohongshu.com，Cookie 可能在 .xiaohongshu.hk。"
                 "请求带 zh-HK，关键词按香港用字做繁简扩展。"),
    ),
    "overseas": RegionProfile(
        id="overseas",
        label="海外 RED",
        web_origin="https://www.rednote.com",
        api_origin="https://webapi.rednote.com",
        accept_language="en,zh-HK;q=0.8,zh-Hant;q=0.7,zh;q=0.5",
        requires_web_session=False,
        signing_asset_host="https://as.rednote.com",
        summary=("海外镜像 www.rednote.com，API webapi.rednote.com，"
                 "Cookie 在 .rednote.com，与大陆登录态不共用。"),
    ),
}

REGION_LABEL = {k: v.label for k, v in _PROFILES.items()}


def normalize_region(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    aliases = {
        "hongkong": "hk", "hong-kong": "hk", "hk": "hk", "香港": "hk", "港版": "hk",
        "rednote": "overseas", "red": "overseas", "global": "overseas",
        "overseas": "overseas", "intl": "overseas", "international": "overseas",
        "海外": "overseas",
        "cn": "cn", "mainland": "cn", "大陆": "cn", "內地": "cn", "内地": "cn",
    }
    if text in _PROFILES:
        return text
    return aliases.get(text, "cn")


def region_profile(value: Optional[str]) -> RegionProfile:
    return _PROFILES[normalize_region(value)]


def list_regions() -> List[Dict[str, str]]:
    rows = []
    for key in REGIONS:
        p = _PROFILES[key]
        rows.append({
            "id": p.id,
            "label": p.label,
            "web_origin": p.web_origin,
            "api_origin": p.api_origin,
            "accept_language": p.accept_language,
            "requires_web_session": p.requires_web_session,
            "signing_asset_host": p.signing_asset_host,
            "summary": p.summary,
        })
    return rows


def _opencc():
    """OpenCC（opencc-python-reimplemented，纯 Python，字典与 OpenCC 相同）。"""
    global _OPENCC
    if _OPENCC is None:
        import opencc
        _OPENCC = {
            "s2t": opencc.OpenCC("s2t"),
            "t2s": opencc.OpenCC("t2s"),
            "s2hk": opencc.OpenCC("s2hk"),
            "hk2s": opencc.OpenCC("hk2s"),
        }
    return _OPENCC


def to_simplified(text: str, region: str = "cn") -> str:
    cc = _opencc()
    if normalize_region(region) == "hk":
        return cc["hk2s"].convert(text or "")
    return cc["t2s"].convert(text or "")


def to_traditional(text: str, region: str = "cn") -> str:
    cc = _opencc()
    if normalize_region(region) in ("hk", "overseas"):
        return cc["s2hk"].convert(text or "")
    return cc["s2t"].convert(text or "")


def keyword_variants(word: str, region: str = "cn") -> List[str]:
    """原词、简体、繁体（香港/海外用香港用字，并保留一条通用繁体）。"""
    word = (word or "").strip()
    if not word:
        return []
    region = normalize_region(region)
    vals = [word, to_simplified(word, region), to_traditional(word, region)]
    if region == "overseas":
        vals.append(_opencc()["s2t"].convert(word))
    out: List[str] = []
    seen = set()
    for item in vals:
        item = (item or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def expand_keywords(words: List[str], region: str = "cn", limit: int = 24) -> List[str]:
    """按地区把关键词扩成简体+繁体。超出 limit 时截断，避免把配额打满。"""
    out: List[str] = []
    seen = set()
    for word in words:
        for item in keyword_variants(word, region):
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


def detect_script(text: str) -> str:
    """用对照字表判断正文是简体、繁体还是混排。没有差异字时返回 unknown。"""
    hans = sum(1 for ch in text or "" if ch in _HANS)
    hant = sum(1 for ch in text or "" if ch in _HANT)
    if hans and hant:
        return "mixed"
    if hant:
        return "hant"
    if hans:
        return "hans"
    return "unknown"
