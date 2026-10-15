# -*- coding: utf-8 -*-
"""统一模型入库：所有引擎的 CrawlResult 都走这里（情感分析 + 去重 + 作者表）。

时间铁律：publish_time/create_time 为发布时间（接口毫秒时间戳→北京时间秒）；
collected_at/created_at 为采集入库时间。发布时间解析失败记为 NULL（未知），
绝不拿采集时间冒充；原始值存 *_raw 字段并打日志备查。

另保留 ingest_run()：读取 Cortech浏览器引擎 JSONL 产物 -> 统一模型 -> store_unified，
字段映射见 cortech_crawler.py。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Tuple

from . import db
from .adapters.base import CommentData, NoteData
# parse_jsonl_dir 已移入 cortech_crawler（融合版）
from .sentiment import analyze

logger = logging.getLogger("xhs-monitor.ingester")


def store_unified(topic_id: int, notes: List[NoteData],
                  comments: List[CommentData]) -> Tuple[int, int]:
    """入库统一数据模型。返回 (新增帖子数, 新增评论数)。"""
    now = int(time.time())  # 采集时间：入库瞬间打戳
    n_notes = n_comments = 0
    for n in notes:
        if not n.post_id:
            continue
        if n.publish_time is None:
            # 发布时间解析失败：记为未知，不许用采集时间冒充；日志留原始值备查
            logger.warning("发布时间未知 post_id=%s raw=%r（已记为NULL，未用采集时间冒充）",
                           n.post_id, n.publish_time_raw)
        label, score = analyze(f"{n.title}\n{n.desc}".strip())
        if db.upsert_note({
            "post_id": n.post_id, "topic_id": topic_id,
            "title": n.title, "desc": n.desc,
            "author": n.author.nickname, "author_id": n.author.author_id,
            "fans_count": n.author.fans_count,
            "publish_time": n.publish_time,
            "publish_time_raw": n.publish_time_raw or "",
            "ip_location": n.ip_location or n.author.ip_location,
            "tags": ",".join(n.tags), "media_type": n.media_type,
            "liked_count": n.liked_count, "collected_count": n.collected_count,
            "comment_count": n.comment_count, "share_count": n.share_count,
            "note_url": n.note_url, "source_keyword": n.source_keyword,
            "sentiment": label, "sentiment_score": score,
            "collected_at": now,
        }):
            n_notes += 1
        if n.author.author_id:
            db.upsert_author({
                "author_id": n.author.author_id, "nickname": n.author.nickname,
                "fans_count": n.author.fans_count, "liked_count": n.author.liked_count,
                "notes_count": n.author.notes_count,
                "ip_location": n.author.ip_location,
            })
    for c in comments:
        if not c.comment_id or not c.post_id:
            continue
        if c.create_time is None:
            logger.warning("评论发布时间未知 comment_id=%s raw=%r（已记为NULL）",
                           c.comment_id, c.create_time_raw)
        label, score = analyze(c.content)
        if db.upsert_comment({
            "comment_id": c.comment_id, "post_id": c.post_id,
            "content": c.content, "author": c.author.nickname,
            "create_time": c.create_time,
            "create_time_raw": c.create_time_raw or "",
            "like_count": c.like_count,
            "reply_count": c.reply_count, "ip_location": c.ip_location,
            "sentiment": label, "sentiment_score": score,
        }):
            n_comments += 1
    return n_notes, n_comments


def ingest_run(task_id: int, topic_id: int, run_dir: Path) -> Tuple[int, int]:
    """读取某次 Cortech浏览器引擎 采集运行的 JSONL 产物，去重入库。"""
    notes, comments = parse_jsonl_dir(Path(run_dir) / "xhs" / "jsonl")
    return store_unified(topic_id, notes, comments)
