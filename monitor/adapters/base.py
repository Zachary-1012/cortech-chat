# -*- coding: utf-8 -*-
"""爬虫适配器统一接口。

所有引擎（Cortech自研采集引擎（直连/浏览器/下载器））
都实现 CrawlerAdapter，输出统一的 NoteData / CommentData / AuthorData，
字段缺失时用默认值容错，入库走同一条链路（ingester.store_unified）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple


@dataclass
class AuthorData:
    author_id: str = ""
    nickname: str = ""
    fans_count: int = 0
    liked_count: int = 0      # 获赞数
    notes_count: int = 0      # 笔记数
    ip_location: str = ""


@dataclass
class NoteData:
    post_id: str
    title: str = ""
    desc: str = ""
    author: AuthorData = field(default_factory=AuthorData)
    publish_time: Optional[int] = None   # 秒级时间戳（发布时间）；解析失败=None（未知），绝不用采集时间冒充
    publish_time_raw: Optional[str] = None  # 接口原始值（毫秒时间戳等），备查
    ip_location: str = ""
    tags: List[str] = field(default_factory=list)
    liked_count: int = 0
    collected_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    # False：网页端没有该字段，0 不是真实分享数
    share_count_available: bool = False
    field_meta: dict = field(default_factory=dict)
    # active：正常笔记；deleted：404/已删除，入库展示，不算采集失败
    post_status: str = "active"
    media_type: str = ""      # image / video
    note_url: str = ""
    source_keyword: str = ""


@dataclass
class CommentData:
    comment_id: str
    post_id: str
    content: str = ""
    author: AuthorData = field(default_factory=AuthorData)
    create_time: Optional[int] = None    # 秒级时间戳（发布时间）；解析失败=None（未知）
    create_time_raw: Optional[str] = None  # 接口原始值，备查
    like_count: int = 0
    reply_count: int = 0
    ip_location: str = ""


@dataclass
class CrawlContext:
    """一次采集任务的上下文"""
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
    # 强攻模式：提高并发与频率（UI 必须提示风控风险）
    aggressive: bool = False
    # 指定账号：解密后的 profile 目录（None 则用默认登录缓存）
    profile_dir: Optional[Path] = None
    account_id: Optional[int] = None
    account_name: str = ""
    # 进度/停止回调（驱动层注入）
    report_progress: Callable[[int, str], None] = lambda pct, detail: None
    should_stop: Callable[[], bool] = lambda: False


@dataclass
class CrawlResult:
    notes: List[NoteData] = field(default_factory=list)
    comments: List[CommentData] = field(default_factory=list)
    deleted_count: int = 0
    detail_skipped: int = 0


class AdapterNotSupported(Exception):
    """引擎不支持某类采集能力"""


class CrawlerAdapter(ABC):
    """所有采集引擎必须实现的接口"""

    name: str = ""
    title: str = ""
    description: str = ""
    # 能力声明（前端据此展示/禁用）
    supports_keyword_search: bool = False
    supports_comments: bool = False
    supports_author_info: bool = False
    # 运行依赖
    needs_browser: bool = False
    needs_node: bool = False
    login_method: str = ""

    @abstractmethod
    def check_login(self) -> Tuple[bool, str]:
        """返回 (是否就绪, 说明文字)"""

    @abstractmethod
    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        """执行采集，返回统一数据模型"""
