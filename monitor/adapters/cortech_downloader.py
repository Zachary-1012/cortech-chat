# -*- coding: utf-8 -*-
"""互补引擎：JoeanAmier/XHS-Downloader（vendor，不修改其代码）

定位说明（源码实证）：
- 这是一个「作品下载器」：按作品链接下载图片/视频文件 + 记录作品数据
  （source/CLI/main.py：--url/-u 作品链接，--record_data/-rd 记录作品数据至文件）。
- 「提取搜索结果作品链接」靠浏览器用户脚本实现，主程序不做关键词搜索；
- 不采集评论区。
因此它不能作为舆情采集的主/备引擎，注册为「互补引擎」：
  crawl() 会明确报错指引；fetch_details(urls) 可按链接补采作品详情/媒体文件。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from ..config import BASE_DIR
from . import register
from .base import (AdapterNotSupported, CrawlContext, CrawlResult,
                   CrawlerAdapter)

VENDOR = BASE_DIR / "vendor" / "XHS-Downloader"


@register
class XHSDownloaderAdapter(CrawlerAdapter):
    name = "cortech_downloader"
    title = "Cortech下载引擎"
    description = ("JoeanAmier/XHS-Downloader：作品下载器，按链接下载作品文件/记录作品数据；"
                   "不支持关键词搜索与评论采集，适合详情/媒体补采。")
    supports_keyword_search = False
    supports_comments = False
    supports_author_info = False
    needs_browser = False
    needs_node = False
    login_method = "无需登录（--cookie 可选传入网页版 Cookie 提升成功率）"

    def check_login(self) -> Tuple[bool, str]:
        ok = (VENDOR / "main.py").exists()
        return (ok, "引擎代码就绪（无需登录即可按链接补采）"
                if ok else "vendor/XHS-Downloader 缺失")

    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        raise AdapterNotSupported(
            "XHS-Downloader 是下载器定位，不支持关键词搜索采集；"
            "请用 mediacrawler（主）或 spider_xhs（备）做舆情采集，"
            "用本引擎的 fetch_details() 按链接补采作品详情/媒体文件。")

    def fetch_details(self, urls: List[str], work_path: str,
                      record_data: bool = True,
                      cookie: str = "") -> Tuple[bool, str]:
        """按作品链接补采：下载/记录作品数据到 work_path。返回 (成功, 日志)。"""
        if not urls:
            return False, "urls 为空"
        cmd = [sys.executable, "main.py", "--url", *urls,
               "--work_path", work_path,
               "--record_data", "true" if record_data else "false"]
        if cookie:
            cmd += ["--cookie", cookie]
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(VENDOR), env={**os.environ, "PYTHONUNBUFFERED": "1"},
            timeout=600,
        )
        return proc.returncode == 0, proc.stdout[-4000:]
