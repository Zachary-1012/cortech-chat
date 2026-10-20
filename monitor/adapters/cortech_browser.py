# -*- coding: utf-8 -*-
"""浏览器采集引擎：Playwright 打开已登录的小红书页面。

调用方式：子进程 `python main.py --platform xhs --lt qrcode --type search
--keywords "a,b" --crawler_max_notes_count N --get_comment yes/no
--save_data_option jsonl --save_data_path <run_dir>`
（参数定义见 vendor/cmd_arg/arg.py）

产物：<run_dir>/xhs/jsonl/search_contents_<日期>.jsonl /
      search_comments_<日期>.jsonl
字段定义见 vendor/store/xhs/__init__.py::update_xhs_note /
update_xhs_note_comment（源码实证）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import (CORTECH_VENDOR_DIR as VENDOR_DIR, active_login_cache_dir,
                     login_cache_dir)
from . import register
from .base import (AdapterNotSupported, AuthorData, CommentData, CrawlContext,
                   CrawlResult, CrawlerAdapter)


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
        elif ts > 1e10:
            ts /= 1e6
        return int(ts)
    except (ValueError, TypeError):
        return None


def note_from_jsonl(item: Dict[str, Any]) -> "NoteData":
    from .base import NoteData
    tags = [t.strip() for t in str(item.get("tag_list") or "").split(",") if t.strip()]
    return NoteData(
        post_id=str(item.get("post_id") or "").strip(),
        title=str(item.get("title") or ""),
        desc=str(item.get("desc") or ""),
        author=AuthorData(nickname=str(item.get("nickname") or "")),
        publish_time=_to_ts(item.get("time")),
        publish_time_raw=(str(item.get("time")) if item.get("time") not in (None, "") else None),
        ip_location="",
        tags=tags,
        liked_count=_to_int(item.get("liked_count")),
        collected_count=_to_int(item.get("collected_count")),
        comment_count=_to_int(item.get("comment_count")),
        share_count=_to_int(item.get("share_count")),
        media_type="video" if str(item.get("type") or "") == "video" else "image",
        note_url=str(item.get("note_url") or ""),
        source_keyword=str(item.get("source_keyword") or ""),
    )


def comment_from_jsonl(item: Dict[str, Any]) -> "CommentData":
    return CommentData(
        comment_id=str(item.get("comment_id") or "").strip(),
        post_id=str(item.get("post_id") or "").strip(),
        content=str(item.get("content") or ""),
        author=AuthorData(nickname=str(item.get("nickname") or "")),
        create_time=_to_ts(item.get("create_time")),
        create_time_raw=(str(item.get("create_time")) if item.get("create_time") not in (None, "") else None),
        like_count=_to_int(item.get("like_count")),
        reply_count=_to_int(item.get("sub_comment_count")),
        ip_location="",
    )


def parse_jsonl_dir(jsonl_dir: Path) -> Tuple[List["NoteData"], List["CommentData"]]:
    from .base import NoteData, CommentData
    notes: List[NoteData] = []
    comments: List[CommentData] = []
    if not jsonl_dir.is_dir():
        return notes, comments
    for path in sorted(jsonl_dir.glob("search_contents_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                n = note_from_jsonl(json.loads(line))
            except (json.JSONDecodeError, AttributeError):
                continue
            if n.post_id:
                notes.append(n)
    for path in sorted(jsonl_dir.glob("search_comments_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = comment_from_jsonl(json.loads(line))
            except (json.JSONDecodeError, AttributeError):
                continue
            if c.comment_id and c.post_id:
                comments.append(c)
    return notes, comments


@register
class CortechBrowserAdapter(CrawlerAdapter):
    name = "cortech_browser"
    title = "Cortech浏览器引擎"
    description = ("Playwright 浏览器采集：关键词搜索 + 一级评论，"
                   "使用本人扫码登录态，结果写入 JSONL。")
    supports_keyword_search = True
    supports_comments = True
    supports_author_info = False
    needs_browser = True
    needs_node = False
    login_method = "小红书 App 扫码。登录态写在数据目录的 xhs_login_cache，不要求放在程序目录。"

    def check_login(self) -> Tuple[bool, str]:
        cache = active_login_cache_dir()
        ok = cache.exists() and any(cache.iterdir())
        return (ok, "检测到浏览器登录缓存，可直接采集"
                if ok else "尚未扫码登录：请在「账号管理」页添加账号并扫码登录")

    def crawl(self, ctx: CrawlContext) -> CrawlResult:
        from .base import CrawlResult
        if not self.supports_keyword_search:
            raise AdapterNotSupported("当前浏览器引擎不支持关键词搜索")
        # 多账号：把指定账号的登录态同步到 vendor 缓存目录（单任务锁保证安全）
        if ctx.account_id:
            from ..login_flow import decrypt_profile_to_dir
            import tempfile, shutil
            tmpd = Path(tempfile.mkdtemp(prefix=f"xhs_acc_{ctx.account_id}_"))
            if decrypt_profile_to_dir(ctx.account_id, tmpd):
                cache = login_cache_dir()
                state = tmpd / "storage_state.json"
                if state.exists():
                    cache.mkdir(parents=True, exist_ok=True)
                    shutil.copy(state, cache / "storage_state.json")
                    ctx.log(f"[cortech-browser] 已切换到账号「{ctx.account_name}」的登录态")
            shutil.rmtree(tmpd, ignore_errors=True)
        cmd = [
            sys.executable, "main.py",
            "--platform", "xhs",
            "--lt", "qrcode",
            "--type", "search",
            "--keywords", ",".join(ctx.keywords),
            "--crawler_max_notes_count", str(ctx.max_notes),
            "--get_comment", "yes" if ctx.enable_comments else "no",
            "--max_comments_count_singlenotes", str(ctx.max_comments_per_note),
            "--save_data_option", "jsonl",
            "--save_data_path", str(ctx.run_dir),
            "--headless", "yes",  # UX 铁律：全程后台，绝不弹出浏览器窗口
            "--get_media", "no",
        ]
        if ctx.aggressive:
            ctx.log("[mediacrawler] ⚠️ 强攻模式：浏览器引擎提速，注意风控风险")
        ctx.log(f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            cwd=str(VENDOR_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        ctx.report_progress(10, "浏览器引擎启动中（headless 后台运行）…")
        try:
            assert proc.stdout is not None
            import time as _time
            start = _time.time()
            # 逐行读日志，同时检查停止请求与超时
            while True:
                if ctx.should_stop():
                    proc.kill()
                    ctx.log("[mediacrawler] 用户已停止采集")
                    break
                if _time.time() - start > ctx.timeout_sec:
                    proc.kill()
                    raise TimeoutError(f"采集超时（>{ctx.timeout_sec} 秒），已终止")
                line = proc.stdout.readline()
                if line:
                    ctx.log(line.rstrip("\n"))
                elif proc.poll() is not None:
                    break
        except TimeoutError:
            raise
        except Exception:
            proc.kill()
            raise
        if proc.returncode not in (0, None) and not ctx.should_stop():
            raise RuntimeError(f"采集进程异常退出，exit_code={proc.returncode}")
        notes, comments = parse_jsonl_dir(ctx.run_dir / "xhs" / "jsonl")
        return CrawlResult(notes=notes, comments=comments)
