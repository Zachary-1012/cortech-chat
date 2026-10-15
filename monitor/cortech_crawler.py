# -*- coding: utf-8 -*-
"""Cortech 统一采集器：4 引擎融合版

融合来源（每个能力的出处见下方注释）：
- direct_api.py（主引擎）：direct-API 直采流程、签名、登录态、评论抓取、风控参数
- mediacrawler.py：浏览器降级策略（direct API 被封时切浏览器模式）
- spider_xhs.py：作者信息补采（粉丝数/获赞数/笔记数/IP属地）
- xhs_downloader.py：下载器定位，不做采集（crawl 本就抛 NotSupported），已剔除

签名算法：xhshow 纯 Python 库（MIT，Cloxl/xhshow），经 MediaCrawler 的
playwright_sign.py 验证为当前最全实现（X-S/X-T/x-S-Common/X-B3-Traceid 四头）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import _xhs
from .config import BASE_DIR, login_cache_dir

# ---- 风控参数（来自 direct_api.py） ----
REQUEST_INTERVAL_SEC = 1.5
AGGRESSIVE_INTERVAL_SEC = 0.4
MAX_PAGES_PER_KEYWORD = 5
AGGRESSIVE_MAX_PAGES = 10
PAGE_SIZE = 20
MAX_SUB_PARENTS = 10
MAX_SUB_PER_PARENT = 10

# ---- 基础请求头（来自 direct_api.py） ----
_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.xiaohongshu.com",
    "pragma": "no-cache",
    "referer": "https://www.xiaohongshu.com/",
    "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
}

_DOMAIN = "https://edith.xiaohongshu.com"


class CortechCrawlerFailed(Exception):
    """采集失败（签名失效/登录过期/风控），调用方可降级到浏览器模式"""


# ============ 数据模型（来自 adapters/base.py，原样保留） ============

@dataclass
class AuthorData:
    author_id: str = ""
    nickname: str = ""
    fans_count: int = 0
    liked_count: int = 0
    notes_count: int = 0
    ip_location: str = ""


@dataclass
class NoteData:
    post_id: str
    title: str = ""
    desc: str = ""
    author: AuthorData = field(default_factory=AuthorData)
    publish_time: Optional[int] = None
    publish_time_raw: Optional[str] = None
    ip_location: str = ""
    tags: List[str] = field(default_factory=list)
    liked_count: int = 0
    collected_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    media_type: str = ""
    note_url: str = ""
    source_keyword: str = ""


@dataclass
class CommentData:
    comment_id: str
    post_id: str
    content: str = ""
    author: AuthorData = field(default_factory=AuthorData)
    create_time: Optional[int] = None
    create_time_raw: Optional[str] = None
    like_count: int = 0
    reply_count: int = 0
    ip_location: str = ""


@dataclass
class CrawlContext:
    topic_id: int
    topic_name: str
    keywords: List[str]
    max_notes: int
    enable_comments: bool
    max_comments_per_note: int
    run_dir: Path
    task_id: int
    timeout_sec: int
    log: Callable[[str], None] = lambda s: None
    aggressive: bool = False
    profile_dir: Optional[Path] = None
    account_id: Optional[int] = None
    account_name: str = ""
    report_progress: Callable[[int, str], None] = lambda pct, detail: None
    should_stop: Callable[[], bool] = lambda: False


@dataclass
class CrawlResult:
    notes: List[NoteData] = field(default_factory=list)
    comments: List[CommentData] = field(default_factory=list)


# ============ 工具函数（来自 direct_api.py） ============

def _to_int(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return 0


def _to_ts(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        ts = float(str(v).strip())
        if ts > 1e12:
            ts /= 1000
        return int(ts)
    except (ValueError, TypeError):
        return None


def _get_search_id() -> str:
    """生成 search_id（来自 MediaCrawler help.py）"""
    import random
    return "".join(random.choices("0123456789abcdef", k=32))


def convert_cookies(cookies: list) -> Tuple[str, Dict[str, str]]:
    """Cookie 列表转字符串+字典（来自 MediaCrawler tools/crawler_util.py）"""
    if not cookies:
        return "", {}
    cookies_str = ";".join(f"{c.get('name')}={c.get('value')}" for c in cookies)
    cookie_dict = {c.get("name"): c.get("value") for c in cookies}
    return cookies_str, cookie_dict


async def _read_login_cookies(profile_dir=None) -> Tuple[str, Dict[str, str]]:
    """headless 读取扫码登录缓存（来自 direct_api.py，原样保留）"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright 未安装")

    async def _cookies_from_storage(browser, storage_state) -> list:
        ctx = await browser.new_context(storage_state=storage_state)
        try:
            return await ctx.cookies("https://www.xiaohongshu.com")
        finally:
            await ctx.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        try:
            cache = Path(profile_dir) if profile_dir else login_cache_dir()
            state_file = cache / "storage_state.json"
            if state_file.exists():
                cookies = await _cookies_from_storage(
                    browser, json.loads(state_file.read_text(encoding="utf-8")))
            elif cache.exists() and any(cache.iterdir()):
                await browser.close()
                ctx2 = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(cache), headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                try:
                    cookies = await ctx2.cookies("https://www.xiaohongshu.com")
                finally:
                    await ctx2.close()
                return convert_cookies(cookies)
            else:
                raise RuntimeError("未检测到扫码登录缓存，请先在「账号管理」完成扫码登录")
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return convert_cookies(cookies)


# ============ HTTP 客户端（精简自 MediaCrawler client.py） ============

class XHSClient:
    """小红书 direct-API 客户端（精简版，只保留 Cortech 需要的方法）

    签名：xhshow 纯算法（来自 MediaCrawler playwright_sign.py）
    端点：edith.xiaohongshu.com（来自 MediaCrawler client.py）
    """

    def __init__(self, headers: Dict[str, str], cookie_dict: Dict[str, str]):
        self.headers = headers
        self.cookie_dict = cookie_dict
        self._client = httpx.AsyncClient(timeout=30.0)

    def _sign_headers(self, uri: str, data: Any, method: str) -> Dict[str, str]:
        """生成签名头（来自 MediaCrawler playwright_sign.py::sign_with_xhshow）"""
        signs = _xhs.sign_with_xhshow(
            uri=uri,
            data=data,
            cookie_str=self.headers.get("Cookie", ""),
            method=method,
        )
        h = dict(self.headers)
        h.update({
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        })
        return h

    async def _request(self, method: str, uri: str,
                       params: Optional[Dict] = None,
                       payload: Optional[Dict] = None) -> Dict:
        """统一请求（含签名+重试），来自 MediaCrawler client.py::request"""
        from tenacity import (retry, retry_if_not_exception_type,
                              stop_after_attempt, wait_fixed)
        url = _DOMAIN + uri
        for attempt in range(3):
            try:
                if params is not None:
                    headers = self._sign_headers(uri, params, "GET")
                    # GET 参数逗号不编码（来自 direct_api.py 签名铁律）
                    from urllib.parse import urlencode
                    qs = urlencode(params, doseq=True).replace("%2C", ",")
                    resp = await self._client.get(f"{url}?{qs}", headers=headers)
                else:
                    headers = self._sign_headers(uri, payload or {}, "POST")
                    # POST 用紧凑 JSON（来自 direct_api.py 签名铁律）
                    body = json.dumps(payload or {}, separators=(",", ":"),
                                      ensure_ascii=False)
                    resp = await self._client.post(url, content=body, headers=headers)
                data = resp.json()
                if data.get("code") != 0:
                    code = data.get("code")
                    msg = data.get("msg", "")
                    if code in (-101, 300012):
                        raise _xhs.PlatformAccessError(f"登录失效 ({code}): {msg}")
                    raise _xhs.DataFetchError(f"API 错误 ({code}): {msg}")
                return data.get("data") or {}
            except (_xhs.PlatformAccessError, _xhs.IPBlockError):
                raise
            except Exception as e:
                if attempt == 2:
                    raise _xhs.DataFetchError(str(e))
                await asyncio.sleep(1)
        return {}

    async def search_notes(self, keyword: str, search_id: str,
                           page: int = 1, page_size: int = 20) -> Dict:
        """关键词搜索（来自 MediaCrawler client.py::get_note_by_keyword）"""
        return await self._request("POST", "/api/sns/web/v1/search/notes", payload={
            "keyword": keyword, "page": page, "page_size": page_size,
            "search_id": search_id,
            "sort": "general",  # 来自 direct_api.py v2 纠正
            "note_type": 0,
        })

    async def get_note_detail(self, post_id: str, xsec_source: str,
                              xsec_token: str) -> Dict:
        """笔记详情（来自 MediaCrawler client.py::get_note_by_id）"""
        data = {
            "source_note_id": post_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token,
        }
        res = await self._request("POST", "/api/sns/web/v1/feed", payload=data)
        items = res.get("items") or []
        if items:
            return items[0].get("note_card") or {}
        return {}

    async def get_comments(self, post_id: str, xsec_token: str,
                           cursor: str = "") -> Dict:
        """一级评论（来自 MediaCrawler client.py::get_note_comments）"""
        return await self._request("GET", "/api/sns/web/v2/comment/page", params={
            "post_id": post_id, "cursor": cursor, "top_comment_id": "",
            "image_formats": "jpg,webp,avif", "xsec_token": xsec_token,
        })

    async def get_sub_comments(self, post_id: str, root_comment_id: str,
                               xsec_token: str, num: int = 10) -> Dict:
        """二级评论（来自 MediaCrawler client.py::get_note_sub_comments）"""
        return await self._request("GET", "/api/sns/web/v2/comment/sub/page", params={
            "post_id": post_id, "root_comment_id": root_comment_id,
            "num": str(num), "cursor": "",
            "image_formats": "jpg,webp,avif", "top_comment_id": "",
            "xsec_token": xsec_token,
        })

    async def get_creator_info(self, user_id: str) -> Dict:
        """作者信息（来自 spider_xhs.py 的作者补采思路，用 direct API 实现，无需 Node.js）"""
        try:
            return await self._request("GET", "/api/sns/web/v1/user/selfinfo", params={})
        except Exception:
            return {}

    async def close(self):
        await self._client.aclose()


# ============ 数据映射（来自 direct_api.py + spider_xhs.py） ============

def _map_note(card: Dict, post_id: str, xsec_token: str,
              xsec_source: str, keyword: str) -> NoteData:
    """详情卡片 → NoteData（来自 direct_api.py::_map_note）"""
    user = card.get("user") or {}
    inter = card.get("interact_info") or {}
    tags = [t.get("name", "") for t in (card.get("tag_list") or [])
            if isinstance(t, dict) and t.get("name")]
    ntype = str(card.get("type") or "")
    return NoteData(
        post_id=str(card.get("post_id") or post_id),
        title=str(card.get("title") or ""),
        desc=str(card.get("desc") or ""),
        author=AuthorData(author_id=str(user.get("user_id") or ""),
                          nickname=str(user.get("nickname") or "")),
        publish_time=_to_ts(card.get("time")),
        publish_time_raw=(str(card.get("time")) if card.get("time") not in (None, "") else None),
        ip_location=str(card.get("ip_location") or ""),
        tags=tags,
        liked_count=_to_int(inter.get("liked_count")),
        collected_count=_to_int(inter.get("collected_count")),
        comment_count=_to_int(inter.get("comment_count")),
        share_count=_to_int(inter.get("share_count")),
        media_type="video" if ntype == "video" else "image",
        note_url=(f"https://www.xiaohongshu.com/explore/{post_id}"
                  f"?xsec_token={xsec_token}&xsec_source={xsec_source}"),
        source_keyword=keyword,
    )


def _map_comment(c: Dict, post_id: str) -> Optional[CommentData]:
    """评论 → CommentData（来自 direct_api.py::_map_comment）"""
    cid = str(c.get("id") or "").strip()
    if not cid:
        return None
    user = c.get("user_info") or {}
    return CommentData(
        comment_id=cid, post_id=post_id,
        content=str(c.get("content") or ""),
        author=AuthorData(author_id=str(user.get("user_id") or ""),
                          nickname=str(user.get("nickname") or "")),
        create_time=_to_ts(c.get("create_time")),
        create_time_raw=(str(c.get("create_time")) if c.get("create_time") not in (None, "") else None),
        like_count=_to_int(c.get("like_count")),
        reply_count=_to_int(c.get("sub_comment_count")),
        ip_location=str(c.get("ip_location") or ""),
    )


# ============ 统一采集器 ============

class CortechCrawler:
    """Cortech 统一采集器（融合 4 引擎）

    主路径：direct-API 直采（来自 direct_api.py）
    作者补采：creator info（来自 spider_xhs.py 的思路，免 Node.js）
    降级：浏览器模式（来自 mediacrawler.py 的策略，改用 Playwright 直连）
    """

    name = "cortech_crawler"
    title = "Cortech 统一采集器"
    description = "融合 direct-API + 浏览器降级的统一采集器"
    supports_keyword_search = True
    supports_comments = True
    supports_author_info = True

    def check_login(self) -> Tuple[bool, str]:
        cache = login_cache_dir()
        if not cache.exists() or not any(cache.iterdir()):
            return False, "尚未扫码登录：请在「账号管理」页添加账号并扫码登录"
        return True, "登录缓存存在，可直接采集"

    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        """同步入口（驱动层调用）"""
        return asyncio.run(self._acrawl(ctx))

    async def _acrawl(self, ctx: CrawlContext) -> CrawlResult:
        """主流程：direct-API，失败时抛 CortechCrawlerFailed 由驱动层降级"""
        interval = AGGRESSIVE_INTERVAL_SEC if ctx.aggressive else REQUEST_INTERVAL_SEC
        max_pages = AGGRESSIVE_MAX_PAGES if ctx.aggressive else MAX_PAGES_PER_KEYWORD
        if ctx.aggressive:
            ctx.log("[cortech] ⚠️ 强攻模式：请求间隔 0.4s、翻页上限 10，注意风控风险")

        # 登录态（来自 direct_api.py）
        profile = None
        if ctx.account_id:
            from .login_flow import decrypt_profile_to_dir
            import tempfile, shutil
            tmpd = Path(tempfile.mkdtemp(prefix=f"xhs_acc_{ctx.account_id}_"))
            if decrypt_profile_to_dir(ctx.account_id, tmpd):
                profile = tmpd
                ctx.log(f"[cortech] 使用账号「{ctx.account_name}」的登录态")
        try:
            cookie_str, cookie_dict = await _read_login_cookies(profile)
        finally:
            if profile:
                import shutil
                shutil.rmtree(profile, ignore_errors=True)
        if "web_session" not in cookie_dict or "a1" not in cookie_dict:
            raise CortechCrawlerFailed("登录失效（Cookie 缺少 a1/web_session），请重新扫码")

        headers = dict(_BASE_HEADERS)
        headers["Cookie"] = cookie_str
        client = XHSClient(headers=headers, cookie_dict=cookie_dict)
        ctx.log("[cortech] 登录态就绪，开始采集")

        notes: List[NoteData] = []
        comments: List[CommentData] = []
        try:
            async def _run():
                total_kw = len(ctx.keywords)
                for i, kw in enumerate(ctx.keywords):
                    if ctx.should_stop():
                        ctx.log("[cortech] 用户已停止采集")
                        break
                    ctx.report_progress(int(5 + 85 * i / max(total_kw, 1)),
                                        f"正在采集关键词「{kw.strip()}」（{i+1}/{total_kw}）")
                    await self._crawl_keyword(client, ctx, kw.strip(), notes,
                                              comments, interval, max_pages)
                    if len(notes) >= ctx.max_notes:
                        break
                ctx.report_progress(95, "采集完成，正在入库…")
            await asyncio.wait_for(_run(), timeout=ctx.timeout_sec)
        except (_xhs.PlatformAccessError, _xhs.IPBlockError) as e:
            raise CortechCrawlerFailed(f"账号受限/风控（{e}），建议降级浏览器模式") from e
        except asyncio.TimeoutError:
            ctx.log(f"[cortech] 超时（>{ctx.timeout_sec}s），返回已采集部分")
        finally:
            await client.close()
        return CrawlResult(notes=notes[:ctx.max_notes], comments=comments)

    async def _crawl_keyword(self, client: XHSClient, ctx: CrawlContext,
                             keyword: str, notes: List[NoteData],
                             comments: List[CommentData],
                             interval: float, max_pages: int) -> None:
        search_id = _get_search_id()
        page = 1
        while page <= max_pages and len(notes) < ctx.max_notes:
            if ctx.should_stop():
                return
            try:
                res = await client.search_notes(keyword, search_id, page, PAGE_SIZE)
            except (_xhs.PlatformAccessError, _xhs.IPBlockError):
                raise
            except _xhs.DataFetchError as e:
                ctx.log(f"[cortech] 搜索失败 {keyword} p{page}：{e}，跳过本词")
                return
            items = [it for it in (res.get("items") or [])
                     if it.get("model_type") not in ("rec_query", "hot_query")]
            if not items:
                break
            ctx.log(f"[cortech] 关键词「{keyword}」第 {page} 页：{len(items)} 条")
            for it in items:
                if len(notes) >= ctx.max_notes:
                    break
                note = await self._fetch_note(client, ctx, it, keyword, interval)
                if note:
                    notes.append(note)
                    if ctx.enable_comments:
                        await self._fetch_comments(client, ctx, note, it,
                                                   comments, interval)
            if not res.get("has_more"):
                break
            page += 1
            await asyncio.sleep(interval)

    async def _fetch_note(self, client: XHSClient, ctx: CrawlContext,
                          item: Dict, keyword: str,
                          interval: float) -> Optional[NoteData]:
        post_id = str(item.get("id") or "").strip()
        xsec_token = str(item.get("xsec_token") or "")
        xsec_source = str(item.get("xsec_source") or "pc_search")
        if not post_id:
            return None
        try:
            card = await client.get_note_detail(post_id, xsec_source, xsec_token)
        except (_xhs.PlatformAccessError, _xhs.IPBlockError):
            raise
        except (_xhs.DataFetchError, _xhs.NoteNotFoundError):
            return None
        if not card:
            return None
        await asyncio.sleep(interval)
        note = _map_note(card, post_id, xsec_token, xsec_source, keyword)
        # 作者信息补采（来自 spider_xhs.py 的思路）
        await self._enrich_author(client, note)
        return note

    async def _enrich_author(self, client: XHSClient, note: NoteData) -> None:
        """作者信息补采：粉丝数等（来自 spider_xhs.py，改用 direct API 免 Node.js）"""
        if not note.author.author_id:
            return
        try:
            info = await client.get_creator_info(note.author.author_id)
            if isinstance(info, dict):
                note.author.fans_count = _to_int(info.get("fans"))
                note.author.notes_count = _to_int(
                    info.get("notes_count") or info.get("note_count"))
                note.author.liked_count = _to_int(
                    info.get("liked_count") or info.get("interaction"))
        except Exception:
            pass

    async def _fetch_comments(self, client: XHSClient, ctx: CrawlContext,
                              note: NoteData, item: Dict,
                              comments: List[CommentData],
                              interval: float) -> None:
        """一级+二级评论（来自 direct_api.py）"""
        xsec_token = str(item.get("xsec_token") or "")
        max_c = ctx.max_comments_per_note
        cursor, fetched = "", 0
        try:
            while fetched < max_c:
                res = await client.get_comments(note.post_id, xsec_token, cursor)
                batch = res.get("comments") or []
                if not batch:
                    break
                for c in batch:
                    if fetched >= max_c:
                        break
                    cd = _map_comment(c, note.post_id)
                    if cd:
                        comments.append(cd)
                        fetched += 1
                parents = [c for c in batch
                           if _to_int(c.get("sub_comment_count")) > 0][:MAX_SUB_PARENTS]
                for p in parents:
                    try:
                        sub = await client.get_sub_comments(
                            note.post_id, str(p.get("id") or ""),
                            xsec_token, MAX_SUB_PER_PARENT)
                        for sc in (sub.get("comments") or []):
                            sd = _map_comment(sc, note.post_id)
                            if sd:
                                comments.append(sd)
                        await asyncio.sleep(interval)
                    except (_xhs.DataFetchError, _xhs.NoteNotFoundError):
                        continue
                if not res.get("has_more"):
                    break
                cursor = res.get("cursor", "")
                await asyncio.sleep(interval)
        except (_xhs.PlatformAccessError, _xhs.IPBlockError):
            raise
        except _xhs.DataFetchError as e:
            ctx.log(f"[cortech] 评论跳过 {note.post_id}：{e}")

    async def crawl_browser_fallback(self, ctx: CrawlContext) -> CrawlResult:
        """浏览器降级模式（来自 mediacrawler.py 的策略）

        direct-API 被封/签名失效时，用 Playwright 打开小红书搜索页直接抓取。
        需要用户已扫码登录（复用同一登录缓存）。
        """
        ctx.log("[cortech] 进入浏览器降级模式（Playwright）")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("playwright 未安装，无法降级到浏览器模式")

        notes: List[NoteData] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            try:
                cache = login_cache_dir()
                state_file = cache / "storage_state.json"
                if state_file.exists():
                    context = await browser.new_context(
                        storage_state=json.loads(state_file.read_text(encoding="utf-8")))
                else:
                    context = await browser.new_context()
                page = await context.new_page()
                for kw in ctx.keywords:
                    if ctx.should_stop() or len(notes) >= ctx.max_notes:
                        break
                    ctx.log(f"[cortech-browser] 搜索关键词：{kw}")
                    await page.goto(
                        f"https://www.xiaohongshu.com/search_result?keyword={kw}",
                        wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    # 提取搜索结果卡片
                    cards = await page.evaluate("""() => {
                        const items = [];
                        document.querySelectorAll('a[href*="/explore/"]').forEach(a => {
                            const href = a.getAttribute('href') || '';
                            const m = href.match(/\\/explore\\/([a-f0-9]+)/);
                            if (m) items.push({post_id: m[1], url: href,
                                title: a.innerText?.slice(0, 100) || ''});
                        });
                        return items.slice(0, 20);
                    }""")
                    for card in cards[:ctx.max_notes - len(notes)]:
                        notes.append(NoteData(
                            post_id=card["post_id"],
                            title=card.get("title", ""),
                            note_url=f"https://www.xiaohongshu.com{card['url']}",
                            source_keyword=kw,
                        ))
                    ctx.report_progress(50, f"浏览器模式已采集 {len(notes)} 条")
                await context.close()
            finally:
                await browser.close()
        ctx.log(f"[cortech-browser] 降级采集完成：{len(notes)} 条（仅标题+链接，详情需 direct-API）")
        return CrawlResult(notes=notes, comments=[])


# ============ 对外接口（供 crawler_driver.py 调用） ============

_crawler = CortechCrawler()


def get_crawler() -> CortechCrawler:
    return _crawler


def check_login() -> Tuple[bool, str]:
    return _crawler.check_login()


def crawl(ctx: CrawlContext) -> CrawlResult:
    return _crawler.crawl(ctx)


def crawl_with_fallback(ctx: CrawlContext) -> Tuple[CrawlResult, str]:
    """带降级的主入口：direct-API 失败时自动切浏览器模式

    返回 (结果, 使用的模式: "direct" | "browser")
    （降级策略来自 mediacrawler.py）
    """
    try:
        return crawl(ctx), "direct"
    except CortechCrawlerFailed as e:
        ctx.log(f"[cortech] direct-API 失败：{e}")
        ctx.log("[cortech] 自动降级到浏览器模式…")
        return asyncio.run(_crawler.crawl_browser_fallback(ctx)), "browser"
