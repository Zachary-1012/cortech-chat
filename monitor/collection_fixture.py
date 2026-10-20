# -*- coding: utf-8 -*-
"""本地样例采集：不访问小红书。

发布时间与采集时间故意不同，并包含一条「已删除」笔记和「网页端无分享数」字段，
用来验证入库与列表，而不是冒充真实会话。
"""
from __future__ import annotations

import time
from typing import Any, Dict

from .adapters.base import AuthorData, CrawlResult, NoteData
from .ingester import store_unified


def build_notes(keyword: str = "样例话题") -> list:
    now = int(time.time())
    published = now - 3 * 86400
    live = NoteData(
        post_id=f"fixture_live_{published}",
        title="样例笔记：发布时间早于采集时间",
        desc="这是本地样例，不是从小红书拉回来的内容。",
        author=AuthorData(author_id="fixture_author", nickname="样例作者"),
        publish_time=published,
        publish_time_raw=str(published * 1000),
        liked_count=12,
        collected_count=3,
        comment_count=1,
        share_count=0,
        share_count_available=False,
        field_meta={
            "share_count": {
                "available": False,
                "source": "not_on_web",
                "reason": "小红书网页端不返回分享数，0 不代表真实分享为 0",
            }
        },
        post_status="active",
        media_type="image",
        note_url="https://www.xiaohongshu.com/explore/fixture_live",
        source_keyword=keyword,
    )
    deleted = NoteData(
        post_id=f"fixture_deleted_{published}",
        title="（已删除或不可浏览）",
        desc="样例：详情接口返回已删除，不应算作采集失败。",
        author=AuthorData(nickname=""),
        publish_time=published - 86400,
        publish_time_raw=str((published - 86400) * 1000),
        share_count=0,
        share_count_available=False,
        field_meta={"availability": {"status": "deleted", "http": 404}},
        post_status="deleted",
        note_url="https://www.xiaohongshu.com/explore/fixture_deleted",
        source_keyword=keyword,
    )
    return [live, deleted]


def build_result(keyword: str = "样例话题") -> CrawlResult:
    notes = build_notes(keyword)
    deleted = sum(1 for n in notes if n.post_status == "deleted")
    return CrawlResult(notes=notes, comments=[], deleted_count=deleted)


def ingest_fixture(topic_id: int, keyword: str = "样例话题") -> Dict[str, Any]:
    notes = build_notes(keyword)
    n_notes, n_comments = store_unified(topic_id, notes, [])
    return {
        "mode": "fixture",
        "needs_real_xhs_session": True,
        "message": "已写入本地样例笔记。这不是真实小红书会话，真实采集仍需主人扫码登录。",
        "notes": n_notes,
        "comments": n_comments,
        "post_ids": [n.post_id for n in notes],
    }
