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
from .adapters.base import AdapterNotSupported
from .config import (DEFAULT_MAX_COMMENTS_PER_NOTE, DEFAULT_TIMEOUT_SEC,
                     RUNS_DIR, collection_fixture_enabled)
from .xhs_errors import failure_kind_from_message


class _Cancelled(Exception):
    """用户主动停止任务"""


_run_lock = threading.Lock()
_LOG_TAIL_MAX = 60


def trigger_crawl(topic_id: int, engine: Optional[str] = None,
                  max_notes: Optional[int] = None,
                  enable_comments: Optional[bool] = None,
                  timeout_sec: int = DEFAULT_TIMEOUT_SEC,
                  account_id: Optional[int] = None,
                  aggressive: bool = False,
                  use_browser_profile: bool = False) -> int:
    """创建任务并在后台线程执行，返回 task_id。engine 为空时用话题配置的引擎。

    account_id 为空时在健康且未超配额的账号里轮换。
    use_browser_profile=True 时只用用户已经登录的 profile，不代登录。
    aggressive=True 为强攻模式（高频采集，风控风险高，调用方必须已提示用户）。
    """
    from .account_rotation import block_reason, first_eligible
    from .browser_use_worker import BrowserUseError, can_use, ensure_ready, has_capacity

    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")
    eng = "cortech_crawler"  # 统一采集器
    cortech_crawler.get_crawler()
    use_profile = False
    auto_rotate = False
    if collection_fixture_enabled():
        account_id = None
    elif use_browser_profile:
        try:
            ensure_ready()
        except BrowserUseError as e:
            raise ValueError(str(e)) from e
        if not has_capacity():
            raise ValueError("浏览器会话队列已满，请稍后再试。")
        account_id = None
        use_profile = True
    elif account_id is None:
        picked = first_eligible()
        if picked:
            account_id = picked["id"]
            auto_rotate = True
        elif can_use() and has_capacity():
            use_profile = True
        else:
            raise ValueError(
                "需要真实小红书登录态：请先在「账号管理」用本人账号扫码，"
                "或用浏览器插件确认发送你自己的登录态后再采集。"
                "服务端不能代替这一步。（need_xhs_session）"
            )
    else:
        acc = db.get_account(account_id)
        reason = block_reason(acc)
        if reason:
            nick = acc["nickname"] if acc else account_id
            raise ValueError(
                f"账号「{nick}」这次不参与采集：{reason}（need_xhs_rescan）"
            )
        db.update_account(account_id, {"last_used_at": int(time.time())})
    task_id = db.create_task(topic_id, eng, account_id=account_id,
                             aggressive=1 if aggressive else 0)
    uid = db.current_user_id()
    if uid is None:
        uid = topic.get("user_id")
    t = threading.Thread(
        target=_run_task,
        args=(task_id, topic, eng, max_notes, enable_comments, timeout_sec,
              account_id, aggressive, uid, auto_rotate, use_profile),
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
              uid: Optional[int] = None, auto_rotate: bool = False,
              use_profile: bool = False) -> None:
    # ContextVar 不跨线程：入口处设置本任务的用户上下文
    db.set_current_user(uid)
    try:
        _run_task_inner(task_id, topic, engine, max_notes, enable_comments,
                        timeout_sec, account_id, aggressive, auto_rotate,
                        use_profile)
    finally:
        db.set_current_user(None)


def _candidate_accounts(account_id: Optional[int], auto_rotate: bool) -> list:
    """自动轮换时跳过不健康或已超配额的账号；指定账号则不擅自换号。"""
    if collection_fixture_enabled():
        return [None]
    if not auto_rotate:
        return [account_id]
    from .account_rotation import eligible_ids
    return eligible_ids(account_id, limit=8) or [account_id]


def _run_task_inner(task_id: int, topic: dict, engine: str, max_notes: Optional[int],
                    enable_comments: Optional[bool], timeout_sec: int,
                    account_id: Optional[int] = None, aggressive: bool = False,
                    auto_rotate: bool = False, use_profile: bool = False) -> None:
    if not _run_lock.acquire(blocking=False):
        db.update_task(task_id, status="failed", finished_at=int(time.time()),
                        error="已有采集任务在运行，请等待其完成后再试")
        return
    run_dir = RUNS_DIR / f"task_{task_id}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "crawler.log"

    def _log(s: str) -> None:
        _append_log(task_id, log_file, s)

    def _report_progress(pct: int, detail: str) -> None:
        from .task_progress import record_task_progress
        record_task_progress(task_id, pct, detail)

    def _should_stop() -> bool:
        t = db.get_task(task_id)
        return bool(t and t.get("cancel_requested"))

    engine_used = "cortech_crawler"
    try:
        db.update_task(task_id, status="running", started_at=int(time.time()),
                        progress=2, progress_detail="任务启动中…")
        crawler = cortech_crawler.get_crawler()
        keywords = [k.strip() for k in topic["keywords"].split(",") if k.strip()]
        candidates = [None] if use_profile else _candidate_accounts(account_id, auto_rotate)
        result = None
        last_error = "采集失败"
        used_account_id = None
        collect_source = "browser_use_profile" if use_profile else "account"
        for idx, acc_id in enumerate(candidates):
            acc_name = ""
            if use_profile:
                acc_name = "本机已登录会话"
            elif acc_id:
                acc = db.get_account(acc_id) or {}
                acc_name = acc.get("nickname") or ""
                from .account_rotation import block_reason
                reason = block_reason(acc)
                if reason and auto_rotate:
                    last_error = f"账号「{acc_name or acc_id}」已跳过：{reason}"
                    _log(f"[driver] {last_error}")
                    continue
                db.update_account(acc_id, {"last_used_at": int(time.time())})
            _log(f"[driver] 引擎=Cortech统一采集器（{crawler.title}）话题={topic['name']}"
                 f"{' 账号=' + acc_name if acc_name else ''}"
                 f"{' [强攻模式]' if aggressive else ''}"
                 f"{' [本地样例]' if collection_fixture_enabled() else ''}"
                 f"{' [已登录浏览器会话]' if use_profile else ''}")
            if collection_fixture_enabled():
                ok, hint = True, "本地样例模式，不访问小红书，也不能代替真实扫码登录"
            elif use_profile:
                ok, hint = True, "将使用你指定的、已经登录的浏览器会话，不会代为登录"
            elif acc_id:
                ok, hint = True, f"使用账号「{acc_name or acc_id}」已保存的登录态"
            else:
                ok, hint = crawler.check_login()
            _log(f"[driver] 登录态检查：{'通过' if ok else '未通过'} - {hint}")
            if not ok:
                raise RuntimeError(
                    "需要真实小红书登录态：请在「账号管理」用本人账号扫码后再采集。"
                    f"（{hint}）"
                )
            if _should_stop():
                raise _Cancelled()

            def _ctx(profile_dir=None):
                return CrawlContext(
                    topic_id=topic["id"], topic_name=topic["name"],
                    keywords=keywords,
                    max_notes=max_notes or topic["max_notes"] or 50,
                    enable_comments=(enable_comments if enable_comments is not None
                                     else bool(topic["enable_comments"])),
                    max_comments_per_note=DEFAULT_MAX_COMMENTS_PER_NOTE,
                    run_dir=run_dir, task_id=task_id,
                    timeout_sec=timeout_sec, log=_log,
                    aggressive=aggressive, account_id=None if use_profile else acc_id,
                    account_name=acc_name, profile_dir=profile_dir,
                    report_progress=_report_progress, should_stop=_should_stop,
                )

            try:
                if collection_fixture_enabled():
                    from .collection_fixture import build_result
                    result = build_result(keywords[0] if keywords else topic["name"])
                    _log("[driver] 已生成本地样例笔记，未请求小红书")
                    break
                if use_profile:
                    from .browser_use_worker import BrowserUseError, session_scope
                    try:
                        with session_scope() as profile_dir:
                            result = crawler.crawl(_ctx(profile_dir))
                    except BrowserUseError as e:
                        raise RuntimeError(str(e)) from e
                    engine_used = "browser_use_profile"
                    break
                result = crawler.crawl(_ctx())
                used_account_id = acc_id
                break
            except CortechCrawlerFailed as e:
                human = str(e)
                last_error = human if human.startswith("采集") else f"采集失败：{human}"
                kind = failure_kind_from_message(human)
                if kind == "login" and acc_id:
                    from .account_rotation import mark_login_failure
                    mark_login_failure(acc_id, human)
                    _log(f"[driver] 账号「{acc_name or acc_id}」登录失效，已标为过期并暂时跳过。{human}")
                    if auto_rotate and idx + 1 < len(candidates):
                        _log("[driver] 轮换到下一个健康账号")
                        continue
                if kind == "risk" and acc_id:
                    from .account_guard import mark_risk
                    mark_risk(acc_id, human)
                    _log(f"[driver] 账号「{acc_name or acc_id}」触发风控，已自动暂停，本任务不立刻重试。")
                    if auto_rotate and idx + 1 < len(candidates):
                        _log("[driver] 轮换到下一个健康账号")
                        continue
                raise RuntimeError(last_error) from e
        if result is None:
            raise RuntimeError(
                last_error if last_error != "采集失败"
                else "没有可轮换的小红书账号：健康检查未通过，或今天的配额已用完。"
            )
        if _should_stop():
            raise _Cancelled()
        _report_progress(96, "正在入库…")
        _log(f"[driver] 采集返回：帖子 {len(result.notes)} 条，评论 {len(result.comments)} 条"
             f"，删除 {getattr(result, 'deleted_count', 0)} 条"
             f"（实际引擎：{engine_used}），开始入库")

        from .ingester import store_unified
        n_notes, n_comments = store_unified(
            topic["id"], result.notes, result.comments,
            account_id=used_account_id, task_id=task_id, source=collect_source)
        if used_account_id and not collection_fixture_enabled():
            from .account_rotation import mark_success
            mark_success(used_account_id, len(result.notes))
        deleted_n = int(getattr(result, "deleted_count", 0) or 0)
        finished = int(time.time())
        from .task_progress import record_task_progress
        record_task_progress(task_id, 100, "采集完成", deleted_count=deleted_n)
        db.update_task(task_id, status="done", finished_at=finished,
                        notes_count=n_notes, engine_used=engine_used,
                        progress=100, progress_detail="采集完成",
                        deleted_count=deleted_n, updated_at=finished)
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
