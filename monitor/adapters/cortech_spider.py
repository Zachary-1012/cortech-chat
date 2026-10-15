# -*- coding: utf-8 -*-
"""备用引擎：cv-cat/Spider_XHS（MIT，vendor，不修改其代码）

纯 HTTP API（curl_cffi Chrome 指纹 + Node.js 本地签名），无浏览器开销，
适合定时任务。需要：
  1. Node.js（签名用，本机已有则直接可用）
  2. pip install -r vendor/Spider_XHS/requirements.txt
  3. Cookie：用其 XHSPcAuth.from_qrcode_login() 或 from_phone_login()
     登录一次，把返回的 cookie 字符串保存到 vendor/Spider_XHS/.cookies
    （或环境变量 SPIDER_XHS_COOKIES）

映射字段来源（源码实证）：
  apis/xhs_pc_apis.py::search_some_note -> data.items[]，每项含
    id / note_card{type,title,desc,user{user_id,nickname},interact_info{
    liked_count,collected_count,comment_count,share_count},image_list,
    tag_list,time,ip_location} / xsec_token
  apis/xhs_pc_apis.py::get_note_all_out_comment(post_id, xsec_token)
  apis/xhs_pc_apis.py::get_user_info(user_id) -> 粉丝数等作者信息
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import BASE_DIR
from . import register
from .base import (AdapterNotSupported, AuthorData, CommentData, CrawlContext,
                   CrawlResult, CrawlerAdapter, NoteData)

VENDOR = BASE_DIR / "vendor" / "Spider_XHS"
COOKIE_FILE = VENDOR / ".cookies"


def _load_cookies() -> str:
    env = os.environ.get("SPIDER_XHS_COOKIES", "").strip()
    if env:
        return env
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    return ""


def _import_vendor():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    try:
        from apis.xhs_pc_apis import XHS_Apis
        from xhs_utils.xhs_pc.auth import XHSPcAuth
    except ImportError as e:
        raise RuntimeError(
            "Spider_XHS 依赖未安装：请先 pip install -r vendor/Spider_XHS/requirements.txt"
            f"（{e}）")
    return XHS_Apis, XHSPcAuth


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


def _map_note(item: Dict[str, Any], keyword: str) -> Optional[NoteData]:
    card = item.get("note_card") or {}
    post_id = str(item.get("id") or item.get("post_id") or "").strip()
    if not post_id:
        return None
    user = card.get("user") or {}
    inter = card.get("interact_info") or {}
    tags = [t.get("name", "") for t in (card.get("tag_list") or []) if isinstance(t, dict) and t.get("name")]
    ntype = str(card.get("type") or "")
    return NoteData(
        post_id=post_id,
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
        note_url=f"https://www.xiaohongshu.com/explore/{post_id}"
                 f"?xsec_token={item.get('xsec_token') or ''}",
        source_keyword=keyword,
    )


def _map_comment(item: Dict[str, Any], post_id: str) -> Optional[CommentData]:
    cid = str(item.get("id") or "").strip()
    if not cid:
        return None
    user = item.get("user_info") or {}
    return CommentData(
        comment_id=cid,
        post_id=post_id,
        content=str(item.get("content") or ""),
        author=AuthorData(author_id=str(user.get("user_id") or ""),
                          nickname=str(user.get("nickname") or "")),
        create_time=_to_ts(item.get("create_time")),
        create_time_raw=(str(item.get("create_time")) if item.get("create_time") not in (None, "") else None),
        like_count=_to_int(item.get("like_count")),
        reply_count=_to_int(item.get("sub_comment_count")),
        ip_location=str(item.get("ip_location") or ""),
    )


@register
class SpiderXHSAdapter(CrawlerAdapter):
    name = "cortech_spider"
    title = "Cortech备用引擎"
    description = ("cv-cat/Spider_XHS（MIT）：纯 HTTP API 采集，无浏览器开销；"
                   "支持关键词搜索 + 一级评论 + 作者信息；需要 Node.js 签名与 Cookie。")
    supports_keyword_search = True
    supports_comments = True
    supports_author_info = True
    needs_browser = False
    needs_node = True
    login_method = ("Cookie 串：用其 XHSPcAuth.from_qrcode_login() 登录一次，"
                    "把返回的 cookie 字符串写入 vendor/Spider_XHS/.cookies "
                    "（或环境变量 SPIDER_XHS_COOKIES）")

    def check_login(self) -> Tuple[bool, str]:
        if not shutil.which("node"):
            return False, "未检测到 Node.js（签名必需），请先安装 Node.js 20+"
        if not _load_cookies():
            return False, ("未配置 Cookie：请按 README 用 Spider_XHS 登录一次，"
                            "把 cookie 串写入 vendor/Spider_XHS/.cookies")
        return True, "Node.js 与 Cookie 就绪，可直接采集"

    def _client(self):
        XHS_Apis, XHSPcAuth = _import_vendor()
        cookies = _load_cookies()
        if not cookies:
            raise RuntimeError("Spider_XHS 未配置 Cookie，见 check_login 说明")
        auth = XHSPcAuth.from_cookie(cookies)
        return XHS_Apis(auth)

    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        api = self._client()
        notes: List[NoteData] = []
        comments: List[CommentData] = []
        per_kw = max(1, ctx.max_notes // max(1, len(ctx.keywords)))
        for kw in ctx.keywords:
            ctx.log(f"[spider_xhs] 搜索关键词：{kw}（目标 {per_kw} 条）")
            ok, msg, items = api.search_some_note(kw, per_kw)
            if not ok:
                ctx.log(f"[spider_xhs] 搜索失败 {kw}：{msg}")
                continue
            for item in items or []:
                n = _map_note(item, kw)
                if not n:
                    continue
                notes.append(n)
                if ctx.enable_comments:
                    try:
                        ok2, msg2, clist = api.get_note_all_out_comment(
                            n.post_id, (item.get("xsec_token") or ""))
                        if ok2:
                            for c in (clist or [])[:ctx.max_comments_per_note]:
                                cc = _map_comment(c, n.post_id)
                                if cc:
                                    comments.append(cc)
                        else:
                            ctx.log(f"[spider_xhs] 评论获取失败 {n.post_id}：{msg2}")
                    except Exception as e:  # noqa: BLE001
                        ctx.log(f"[spider_xhs] 评论异常 {n.post_id}：{e}")
            if len(notes) >= ctx.max_notes:
                break
        # 作者信息补采（粉丝数等）
        if self.supports_author_info:
            seen = {}
            for n in notes:
                aid = n.author.author_id
                if aid and aid not in seen:
                    seen[aid] = n.author
            for aid, author in list(seen.items())[:50]:
                try:
                    ok, msg, info = api.get_user_info(aid)
                    if ok and isinstance(info, dict):
                        author.fans_count = _to_int(info.get("fans"))
                        author.notes_count = _to_int(info.get("notes_count") or info.get("note_count"))
                        author.liked_count = _to_int(info.get("liked_count") or info.get("interaction"))
                        author.ip_location = str(info.get("ip_location") or "")
                except Exception:  # noqa: BLE001
                    pass
        return CrawlResult(notes=notes[:ctx.max_notes], comments=comments)
