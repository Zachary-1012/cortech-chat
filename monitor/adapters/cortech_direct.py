# -*- coding: utf-8 -*-
"""主引擎（默认）：小红书网页版 direct-API 直采

可行性依据：docs/REA-小红书机制解析-v2.md（v2 修正了 v1 的两处参数错误）。

v2 关键纠正（已落实）：
- search/notes 参数：sort=general/popularity_descending/time_descending（v1 的
  newest/most_popular 有误）；note_type=0/1/2 整数（v1 的 all/video/image 有误）。
  本适配器经 vendor 的 SearchSortType/SearchNoteType 枚举调用，值为 v2 正确值。
- 签名输入铁律：POST 用紧凑 JSON（separators=(",",":")，ensure_ascii=False，
  中文不转义）；GET 参数逗号不编码。vendor 的 client.py 已按此实现。
- 登录态最小集：持久化 a1+web_session 即可（a1 是签名锚点，缺了直接抛错）；
  健康检查调 pong()（GET /api/sns/web/v1/user/selfinfo）。
- 漂移检测：monitor/sign_drift.py（L1 被动/L2 探针/L3 bundle/L4 xhshow），
  告警接入分级预警，签名红灯自动降级浏览器引擎。

实现策略：签名使用 xhshow（MIT）；登录态读取数据目录里本人扫码保存的
a1 + web_session；流程为关键词搜索、笔记详情、评论。签名失效或账号风控时抛
DirectAPIFailed，由驱动层降级到浏览器引擎。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import BASE_DIR, active_login_cache_dir
from ..xhs_errors import share_count_meta
from . import register
from .base import (AuthorData, CommentData, CrawlContext, CrawlResult,
                   CrawlerAdapter, NoteData)

VENDOR = BASE_DIR / "vendor" / "MediaCrawler"

# 风控参数
REQUEST_INTERVAL_SEC = 1.5   # 每次 API 请求间隔（正常模式）
AGGRESSIVE_INTERVAL_SEC = 0.4  # 强攻模式间隔（⚠️ 风控风险高）
MAX_PAGES_PER_KEYWORD = 5    # 每关键词最大翻页数（page_size=20 → 上限约100条/词）
AGGRESSIVE_MAX_PAGES = 10    # 强攻模式翻页上限
PAGE_SIZE = 20
MAX_SUB_PARENTS = 10         # 每帖最多取二级评论的父评论数
MAX_SUB_PER_PARENT = 10      # 每父评论最多二级评论数


class DirectAPIFailed(Exception):
    """direct-api 不可用（签名失效/登录过期/风控），驱动层据此降级到浏览器引擎"""


def _ensure_vendor() -> None:
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


def _import_vendor():
    _ensure_vendor()
    try:
        from media_platform.xhs.client import XiaoHongShuClient
        from media_platform.xhs.exception import (DataFetchError,
                                                 IPBlockError,
                                                 NoteNotFoundError,
                                                 PlatformAccessError)
        from tools.crawler_util import convert_cookies
    except ImportError as e:
        raise RuntimeError(
            "direct-api 依赖未就绪：请先安装运行环境依赖（monitor/requirements.txt）"
            f"（缺 {e}）。注意 xhshow/playwright 来自该 requirements。")
    return XiaoHongShuClient, convert_cookies, (DataFetchError, IPBlockError,
                                                NoteNotFoundError, PlatformAccessError)


async def _read_login_cookies(profile_dir=None) -> Tuple[str, Dict[str, str]]:
    """读取本人扫码保存的 Cookie（不重新登录）

    profile_dir：指定账号的解密 profile 目录（多账号）；None 则用默认登录缓存。
    """
    _ensure_vendor()
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright 未安装，无法从浏览器 profile 读取登录态")
    from pathlib import Path

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
            if isinstance(profile_dir, dict):
                # 多账号：调用方已解密出 storage_state dict
                cookies = await _cookies_from_storage(browser, profile_dir)
            else:
                cache = Path(profile_dir) if profile_dir else active_login_cache_dir()
                state_file = cache / "storage_state.json"
                if state_file.exists():
                    cookies = await _cookies_from_storage(
                        browser, json.loads(state_file.read_text(encoding="utf-8")))
                elif cache.exists() and any(cache.iterdir()):
                    # 兼容旧版：vendor 完整 profile 目录（扫码直写）
                    await browser.close()
                    ctx2 = await pw.chromium.launch_persistent_context(
                        user_data_dir=str(cache), headless=True,
                        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                    try:
                        cookies = await ctx2.cookies("https://www.xiaohongshu.com")
                    finally:
                        await ctx2.close()
                    from tools.crawler_util import convert_cookies
                    return convert_cookies(cookies)
                else:
                    raise RuntimeError("未检测到扫码登录缓存，请先在「账号管理」完成扫码登录")
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
    from tools.crawler_util import convert_cookies
    return convert_cookies(cookies)


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
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
}


@register
class DirectAPIAdapter(CrawlerAdapter):
    name = "cortech_direct"
    title = "Cortech直连引擎（默认）"
    description = ("小红书网页版 direct-API 直采（edith.xiaohongshu.com）：轻量快速，"
                   "无浏览器开销；登录失效时降级到浏览器引擎。")
    supports_keyword_search = True
    supports_comments = True
    supports_author_info = True
    needs_browser = False   # 仅 headless 读一次登录 Cookie，不跑可见浏览器
    needs_node = False
    login_method = ("在「账号管理」用本人小红书账号扫码一次，"
                    "本引擎读取 Cookie（a1 + web_session）后直调 API")

    def check_login(self) -> Tuple[bool, str]:
        try:
            _import_vendor()
        except RuntimeError as e:
            return False, str(e)
        cache = active_login_cache_dir()
        if not cache.exists() or not any(cache.iterdir()):
            return False, "尚未扫码登录：请在「账号管理」页添加账号并扫码登录"
        return True, "登录缓存存在，Cookie 可在采集时 headless 读取"

    # ---- 同步接口（驱动层调用） ----
    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        return asyncio.run(self._acrawl(ctx))

    # ---- 异步采集主流程 ----
    async def _acrawl(self, ctx: CrawlContext) -> CrawlResult:
        XiaoHongShuClient, _, excs = _import_vendor()
        DataFetchError, IPBlockError, NoteNotFoundError, PlatformAccessError = excs
        self._excs = excs

        # 多账号：用指定账号的登录态；否则用默认缓存
        profile = None
        if ctx.account_id:
            from ..login_flow import decrypt_profile_to_dir  # 延迟导入防循环
            import tempfile
            tmpd = Path(tempfile.mkdtemp(prefix=f"xhs_acc_{ctx.account_id}_"))
            if decrypt_profile_to_dir(ctx.account_id, tmpd):
                profile = tmpd
                ctx.log(f"[direct-api] 使用账号「{ctx.account_name}」的登录态")
            else:
                ctx.log(f"[direct-api] 账号「{ctx.account_name}」登录态解密失败，回退默认缓存")
        self._interval = AGGRESSIVE_INTERVAL_SEC if ctx.aggressive else REQUEST_INTERVAL_SEC
        self._max_pages = AGGRESSIVE_MAX_PAGES if ctx.aggressive else MAX_PAGES_PER_KEYWORD
        if ctx.aggressive:
            ctx.log("[direct-api] ⚠️ 强攻模式：请求间隔 0.4s、翻页上限 10，注意风控风险")

        try:
            cookie_str, cookie_dict = await _read_login_cookies(profile)
        finally:
            if profile:
                import shutil
                shutil.rmtree(profile, ignore_errors=True)
        if "web_session" not in cookie_dict or "a1" not in cookie_dict:
            raise DirectAPIFailed(
                f"账号「{ctx.account_name or '默认'}」登录失效（Cookie 缺少 a1/web_session），"
                "请在「账号管理」重新扫码")
        headers = dict(_BASE_HEADERS)
        headers["Cookie"] = cookie_str
        client = XiaoHongShuClient(headers=headers, playwright_page=None,
                                   cookie_dict=cookie_dict)
        ctx.log("[direct-api] 登录态就绪（headless 读取缓存），开始直采")

        notes: List[NoteData] = []
        comments: List[CommentData] = []
        try:
            async def _run():
                total_kw = len(ctx.keywords)
                for i, kw in enumerate(ctx.keywords):
                    if ctx.should_stop():
                        ctx.log("[direct-api] 用户已停止采集")
                        break
                    ctx.report_progress(int(5 + 85 * i / max(total_kw, 1)),
                                        f"正在采集关键词「{kw.strip()}」（{i+1}/{total_kw}）")
                    await self._crawl_keyword(client, ctx, kw.strip(), notes, comments)
                    if len(notes) >= ctx.max_notes:
                        break
                ctx.report_progress(95, "采集完成，正在入库…")
            await asyncio.wait_for(_run(), timeout=ctx.timeout_sec)
        except (PlatformAccessError, IPBlockError) as e:
            raise DirectAPIFailed(f"账号受限/风控（{e}），建议降级浏览器引擎") from e
        except asyncio.TimeoutError:
            ctx.log(f"[direct-api] 超时（>{ctx.timeout_sec}s），返回已采集部分")
        return CrawlResult(notes=notes[:ctx.max_notes], comments=comments)

    async def _crawl_keyword(self, client, ctx: CrawlContext, keyword: str,
                             notes: List[NoteData], comments: List[CommentData]) -> None:
        DataFetchError, IPBlockError, NoteNotFoundError, PlatformAccessError = self._excs
        from media_platform.xhs.client import get_search_id
        search_id = get_search_id()
        page = 1
        interval = getattr(self, "_interval", REQUEST_INTERVAL_SEC)
        max_pages = getattr(self, "_max_pages", MAX_PAGES_PER_KEYWORD)
        while page <= max_pages and len(notes) < ctx.max_notes:
            if ctx.should_stop():
                return
            try:
                res = await client.get_note_by_keyword(
                    keyword=keyword, search_id=search_id, page=page, page_size=PAGE_SIZE)
            except (PlatformAccessError, IPBlockError) as e:
                # L1 被动检测：记录可疑特征（签名失效/风控）
                try:
                    from ..sign_drift import l1_check_response, record_probe
                    reason = l1_check_response(0, getattr(e, "code", ""))
                    if reason:
                        record_probe("l1_passive", False, 0,
                                     getattr(e, "code", ""), f"{keyword}: {reason}"[:150])
                        ctx.log(f"[sign_drift] L1 告警：{reason}")
                except Exception:  # noqa: BLE001
                    pass
                raise
            except DataFetchError as e:
                ctx.log(f"[direct-api] 搜索失败 {keyword} p{page}：{e}，跳过本词")
                return
            items = [it for it in (res.get("items") or [])
                     if it.get("model_type") not in ("rec_query", "hot_query")]
            if not items:
                break
            ctx.log(f"[direct-api] 关键词「{keyword}」第 {page} 页：{len(items)} 条")
            for it in items:
                if len(notes) >= ctx.max_notes:
                    break
                note = await self._fetch_note(client, ctx, it, keyword)
                if note:
                    notes.append(note)
                    if ctx.enable_comments:
                        await self._fetch_comments(client, ctx, note, it, comments)
            if not res.get("has_more"):
                break
            page += 1
            await asyncio.sleep(getattr(self, '_interval', REQUEST_INTERVAL_SEC))

    async def _fetch_note(self, client, ctx: CrawlContext, item: Dict,
                          keyword: str) -> Optional[NoteData]:
        DataFetchError, IPBlockError, NoteNotFoundError, PlatformAccessError = self._excs
        post_id = str(item.get("id") or "").strip()
        xsec_token = str(item.get("xsec_token") or "")
        xsec_source = str(item.get("xsec_source") or "pc_search")
        if not post_id:
            return None
        try:
            card = await client.get_note_by_id(post_id, xsec_source, xsec_token)
        except (PlatformAccessError, IPBlockError):
            raise
        except NoteNotFoundError as e:
            ctx.log(f"[direct-api] 已删除或不存在 {post_id}：{e}")
            return NoteData(
                post_id=post_id,
                title="（已删除或不可浏览）",
                desc=str(e),
                post_status="deleted",
                share_count_available=False,
                field_meta={"availability": {"status": "deleted", "reason": str(e)}},
                note_url=f"https://www.xiaohongshu.com/explore/{post_id}",
                source_keyword=keyword,
            )
        except DataFetchError as e:
            ctx.log(f"[direct-api] 详情采集失败（不是删除） {post_id}：{e}")
            return None
        if not card:
            ctx.log(f"[direct-api] 详情为空，按已删除处理 {post_id}")
            return NoteData(
                post_id=post_id,
                title="（已删除或不可浏览）",
                desc="详情为空，按已删除或不可浏览处理",
                post_status="deleted",
                share_count_available=False,
                field_meta={"availability": {"status": "deleted"}},
                note_url=f"https://www.xiaohongshu.com/explore/{post_id}",
                source_keyword=keyword,
            )
        await asyncio.sleep(getattr(self, '_interval', REQUEST_INTERVAL_SEC))
        return self._map_note(card, post_id, xsec_token, xsec_source, keyword)

    @staticmethod
    def _map_note(card: Dict, post_id: str, xsec_token: str,
                  xsec_source: str, keyword: str) -> NoteData:
        """详情卡片 → 统一 NoteData（synthetic 可单测）"""
        user = card.get("user") or {}
        inter = card.get("interact_info") or {}
        tags = [t.get("name", "") for t in (card.get("tag_list") or [])
                if isinstance(t, dict) and t.get("name")]
        ntype = str(card.get("type") or "")
        _share = share_count_meta(inter)
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
            share_count=_share[0],
            share_count_available=_share[1],
            field_meta=_share[2],
            media_type="video" if ntype == "video" else "image",
            note_url=(f"https://www.xiaohongshu.com/explore/{post_id}"
                      f"?xsec_token={xsec_token}&xsec_source={xsec_source}"),
            source_keyword=keyword,
        )

    async def _fetch_comments(self, client, ctx: CrawlContext, note: NoteData,
                              item: Dict, comments: List[CommentData]) -> None:
        DataFetchError, IPBlockError, NoteNotFoundError, PlatformAccessError = self._excs
        xsec_token = str(item.get("xsec_token") or "")
        max_c = ctx.max_comments_per_note
        cursor, fetched = "", 0
        try:
            while fetched < max_c:
                res = await client.get_note_comments(
                    post_id=note.post_id, xsec_token=xsec_token, cursor=cursor)
                batch = res.get("comments") or []
                if not batch:
                    break
                for c in batch:
                    if fetched >= max_c:
                        break
                    cd = self._map_comment(c, note.post_id)
                    if cd:
                        comments.append(cd)
                        fetched += 1
                # 二级评论：只取有回复的父评论，限量
                parents = [c for c in batch if _to_int(c.get("sub_comment_count")) > 0][:MAX_SUB_PARENTS]
                for p in parents:
                    try:
                        sub = await client.get_note_sub_comments(
                            post_id=note.post_id,
                            root_comment_id=str(p.get("id") or ""),
                            xsec_token=xsec_token, num=MAX_SUB_PER_PARENT)
                        for sc in (sub.get("comments") or []):
                            sd = self._map_comment(sc, note.post_id)
                            if sd:
                                comments.append(sd)
                        await asyncio.sleep(getattr(self, '_interval', REQUEST_INTERVAL_SEC))
                    except (DataFetchError, NoteNotFoundError):
                        continue
                if not res.get("has_more"):
                    break
                cursor = res.get("cursor", "")
                await asyncio.sleep(getattr(self, '_interval', REQUEST_INTERVAL_SEC))
        except (PlatformAccessError, IPBlockError):
            raise
        except DataFetchError as e:
            ctx.log(f"[direct-api] 评论跳过 {note.post_id}：{e}")

    @staticmethod
    def _map_comment(c: Dict, post_id: str) -> Optional[CommentData]:
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
