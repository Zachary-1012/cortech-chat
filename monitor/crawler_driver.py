# -*- coding: utf-8 -*-
"""采集驱动：通过适配器注册表调度引擎（主/备可切换）

流程：trigger_crawl -> 建任务行 -> 后台线程 -> 适配器.crawl(ctx)
     -> ingester.store_unified 入库 -> 任务状态更新。

同一时间只跑一个采集任务（进程级锁 + DB 状态双保险）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from . import db
from . import cortech_crawler
from .cortech_crawler import CrawlContext, CortechCrawlerFailed
from .config import (DEFAULT_MAX_COMMENTS_PER_NOTE, DEFAULT_TIMEOUT_SEC,
                     RUNS_DIR)


class _Cancelled(Exception):
    """用户主动停止任务"""


_run_lock = threading.Lock()
_LOG_TAIL_MAX = 60


def trigger_crawl(topic_id: int, engine: Optional[str] = None,
                  max_notes: Optional[int] = None,
                  enable_comments: Optional[bool] = None,
                  timeout_sec: int = DEFAULT_TIMEOUT_SEC,
                  account_id: Optional[int] = None,
                  aggressive: bool = False) -> int:
    """创建任务并在后台线程执行，返回 task_id。engine 为空时用话题配置的引擎。

    account_id 为空时用默认账号（上次使用的有效账号）。
    aggressive=True 为强攻模式（高频采集，风控风险高，调用方必须已提示用户）。
    """
    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")
    eng = "cortech_crawler"  # 统一采集器（融合4引擎）
    crawler = cortech_crawler.get_crawler()
    if account_id is None:
        default_acc = db.get_default_account()
        account_id = default_acc["id"] if default_acc else None
    if account_id:
        acc = db.get_account(account_id)
        if not acc or acc["status"] != "valid":
            raise ValueError(
                f"账号「{acc['nickname'] if acc else account_id}」登录失效，"
                "请在「账号管理」重新扫码后再采集")
        db.update_account(account_id, {"last_used_at": int(time.time())})
    task_id = db.create_task(topic_id, eng, account_id=account_id,
                             aggressive=1 if aggressive else 0)
    uid = db.current_user_id()
    if uid is None:
        uid = topic.get("user_id")
    t = threading.Thread(
        target=_run_task,
        args=(task_id, topic, eng, max_notes, enable_comments, timeout_sec,
              account_id, aggressive, uid),
        daemon=True,
    )
    t.start()
    return task_id


def request_stop(task_id: int) -> bool:
    """请求停止任务（采集循环内检查，尽快中断）"""
    task = db.get_task(task_id)
    if not task or task["status"] not in ("queued", "running"):
        return False
    db.update_task(task_id, cancel_requested=1)
    return True


def _append_log(task_id: int, log_file: Path, line: str) -> None:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        tail = log_file.read_text(encoding="utf-8").splitlines()[-_LOG_TAIL_MAX:]
        db.update_task(task_id, log_tail="\n".join(tail))
    except Exception:
        pass


def _run_task(task_id: int, topic: dict, engine: str, max_notes: Optional[int],
              enable_comments: Optional[bool], timeout_sec: int,
              account_id: Optional[int] = None, aggressive: bool = False,
              uid: Optional[int] = None) -> None:
    # ContextVar 不跨线程：入口处设置本任务的用户上下文
    db.set_current_user(uid)
    try:
        _run_task_inner(task_id, topic, engine, max_notes, enable_comments,
                        timeout_sec, account_id, aggressive)
    finally:
        db.set_current_user(None)


def _run_task_inner(task_id: int, topic: dict, engine: str, max_notes: Optional[int],
                    enable_comments: Optional[bool], timeout_sec: int,
                    account_id: Optional[int] = None, aggressive: bool = False) -> None:
    if not _run_lock.acquire(blocking=False):
        db.update_task(task_id, status="failed", finished_at=int(time.time()),
                        error="已有采集任务在运行，请等待其完成后再试")
        return
    run_dir = RUNS_DIR / f"task_{task_id}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "crawler.log"

    def _log(s: str) -> None:
        _append_log(task_id, log_file, s)

    _last_progress = [0]

    def _report_progress(pct: int, detail: str) -> None:
        pct = max(0, min(100, int(pct)))
        # 节流：进度变化>=2 或 detail 变化才写库
        if pct - _last_progress[0] >= 2 or pct >= 100:
            _last_progress[0] = pct
            db.update_task(task_id, progress=pct, progress_detail=detail)

    def _should_stop() -> bool:
        t = db.get_task(task_id)
        return bool(t and t.get("cancel_requested"))

    try:
        db.update_task(task_id, status="running", started_at=int(time.time()),
                        progress=2, progress_detail="任务启动中…")
        engine_used = "cortech_crawler"
        crawler = cortech_crawler.get_crawler()
        acc_name = ""
        if account_id:
            acc = db.get_account(account_id)
            acc_name = acc["nickname"] if acc else ""
        _log(f"[driver] 引擎=Cortech统一采集器（{crawler.title}）话题={topic['name']}"
             f"{' 账号=' + acc_name if acc_name else ''}"
             f"{' [强攻模式]' if aggressive else ''}")
        ok, hint = crawler.check_login()
        _log(f"[driver] 登录态检查：{'通过' if ok else '未通过'} - {hint}")
        if not ok:
            raise RuntimeError(f"引擎 {engine} 未就绪：{hint}")
        if _should_stop():
            raise _Cancelled()

        keywords = [k.strip() for k in topic["keywords"].split(",") if k.strip()]
        ctx = CrawlContext(
            topic_id=topic["id"], topic_name=topic["name"],
            keywords=keywords,
            max_notes=max_notes or topic["max_notes"] or 50,
            enable_comments=(enable_comments if enable_comments is not None
                             else bool(topic["enable_comments"])),
            max_comments_per_note=DEFAULT_MAX_COMMENTS_PER_NOTE,
            run_dir=run_dir, task_id=task_id,
            timeout_sec=timeout_sec, log=_log,
            aggressive=aggressive, account_id=account_id, account_name=acc_name,
            report_progress=_report_progress, should_stop=_should_stop,
        )
        result = None
        try:
            result = crawler.crawl(ctx)
        except CortechCrawlerFailed as e:
            # 统一采集器内部已处理降级，这里直接抛错
            raise RuntimeError(f"采集失败：{e}") from e
        if _should_stop():
            raise _Cancelled()
        _report_progress(96, "正在入库…")
        _log(f"[driver] 采集返回：帖子 {len(result.notes)} 条，评论 {len(result.comments)} 条"
             f"（实际引擎：{engine_used}），开始入库")

        from .ingester import store_unified
        n_notes, n_comments = store_unified(topic["id"], result.notes, result.comments)
        db.update_task(task_id, status="done", finished_at=int(time.time()),
                        notes_count=n_notes, engine_used=engine_used,
                        progress=100, progress_detail="采集完成")
        _log(f"[ingester] 入库完成：帖子 {n_notes} 条（新增），评论 {n_comments} 条（新增）")
        # 预警联动：任务完成后评估分级预警（失败不影响主流程）
        try:
            from . import alerts
            new_alerts = alerts.evaluate_after_task(task_id)
            if new_alerts:
                _log(f"[alerts] 触发 {len(new_alerts)} 条预警")
        except Exception as e:  # noqa: BLE001
            _log(f"[alerts] 预警评估失败（不影响采集）：{type(e).__name__}")
    except _Cancelled:
        db.update_task(task_id, status="cancelled", finished_at=int(time.time()),
                        progress_detail="用户已停止")
        _log("[driver] 任务已被用户停止")
    except (AdapterNotSupported, NotImplementedError) as e:
        db.update_task(task_id, status="failed", finished_at=int(time.time()),
                        error=str(e), engine_used=engine_used)
        _log(f"[error] {e}")
    except Exception as e:  # noqa: BLE001
        db.update_task(task_id, status="failed", finished_at=int(time.time()),
                        error=str(e), engine_used=engine_used)
        _log(f"[error] {e}")
    finally:
        _run_lock.release()


def is_running() -> bool:
    return _run_lock.locked()
