# -*- coding: utf-8 -*-
"""SaaS 首次启动自检：库、数据目录、登录组件、小红书采集账号。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import (DATA_DIR, bind_host, bind_port, browser_use_enabled,
                     collection_fixture_enabled, cookie_handshake_enabled,
                     ensure_dirs, saas_mode)
from . import db


def _check_db() -> Dict[str, Any]:
    try:
        db.init_db()
        with db._connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"id": "db", "ok": True, "label": "数据库",
                "detail": "SQLite 可读写"}
    except Exception as e:  # noqa: BLE001
        return {"id": "db", "ok": False, "label": "数据库",
                "detail": f"数据库不可用：{type(e).__name__}"}


def _check_data_dir() -> Dict[str, Any]:
    try:
        ensure_dirs()
        probe = DATA_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"id": "data_dir", "ok": True, "label": "数据目录",
                "detail": str(DATA_DIR)}
    except Exception as e:  # noqa: BLE001
        return {"id": "data_dir", "ok": False, "label": "数据目录",
                "detail": f"数据目录不可写：{type(e).__name__}"}


def _check_auth() -> Dict[str, Any]:
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return {"id": "auth", "ok": False, "label": "登录组件",
                "detail": "bcrypt 未安装，无法注册或登录。请在运行环境安装 monitor/requirements.txt。"}
    return {"id": "auth", "ok": True, "label": "登录组件",
            "detail": "bcrypt 可用，邮箱注册与 session 登录已启用"}


def _tenant_snapshot(user_id: Optional[int]) -> Dict[str, Any]:
    """当前租户的账号、话题、最近一次采集。未登录时不跨租户汇总。"""
    empty = {
        "accounts": [],
        "xhs_accounts_healthy": 0,
        "has_topic": False,
        "topic_count": 0,
        "last_crawl": None,
    }
    if user_id is None:
        return empty
    db.set_current_user(user_id)
    try:
        accounts = db.list_accounts()
        topics = db.list_topics()
        tasks = db.list_tasks(limit=1)
    finally:
        db.set_current_user(None)
    valid = [a for a in accounts if a.get("status") == "valid"]
    last = None
    if tasks:
        t = tasks[0]
        last = {
            "task_id": t.get("id"),
            "status": t.get("status") or "",
            "progress": int(t.get("progress") or 0),
            "progress_detail": t.get("progress_detail") or "",
            "notes_count": int(t.get("notes_count") or 0),
            "topic_id": t.get("topic_id"),
            "started_at": t.get("started_at"),
            "finished_at": t.get("finished_at"),
        }
    return {
        "accounts": accounts,
        "xhs_accounts_healthy": len(valid),
        "has_topic": bool(topics),
        "topic_count": len(topics),
        "last_crawl": last,
    }


def _check_xhs(user_id: Optional[int], accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    if user_id is None:
        return {
            "id": "xhs_account",
            "ok": False,
            "label": "小红书采集账号",
            "detail": "尚未登录 Cortech Chat。注册后请在「账号管理」用本人小红书账号扫码。",
            "code": "need_login",
            "valid_count": 0,
        }
    valid = [a for a in accounts if a.get("status") == "valid"]
    if valid:
        names = "、".join((a.get("nickname") or f"账号{a['id']}") for a in valid[:3])
        return {
            "id": "xhs_account",
            "ok": True,
            "label": "小红书采集账号",
            "detail": f"已有 {len(valid)} 个有效登录态（{names}）",
            "code": "xhs_ready",
            "valid_count": len(valid),
        }
    if accounts:
        return {
            "id": "xhs_account",
            "ok": False,
            "label": "小红书采集账号",
            "detail": "已有账号但登录态无效或未完成扫码。请在「账号管理」重新扫码。",
            "code": "need_xhs_rescan",
            "account_count": len(accounts),
        }
    return {
        "id": "xhs_account",
        "ok": False,
        "label": "小红书采集账号",
        "detail": "还没有小红书采集账号。请用本人账号扫码登录后再采集。真实会话无法由服务端代替。",
        "code": "need_xhs_session",
    }


def _check_topic(snap: Dict[str, Any], user_id: Optional[int]) -> Dict[str, Any]:
    if user_id is None:
        return {"id": "topic", "ok": False, "label": "至少一个话题",
                "detail": "登录后可查看本账号是否已创建话题。"}
    if snap["has_topic"]:
        return {"id": "topic", "ok": True, "label": "至少一个话题",
                "detail": f"已有 {snap['topic_count']} 个话题"}
    return {"id": "topic", "ok": False, "label": "至少一个话题",
            "detail": "还没有话题。请先在「话题管理」创建一个再采集。"}


def _check_last_crawl(snap: Dict[str, Any], user_id: Optional[int]) -> Dict[str, Any]:
    if user_id is None:
        return {"id": "last_crawl", "ok": False, "label": "最近一次采集",
                "detail": "登录后可查看本账号最近一次采集状态。"}
    last = snap.get("last_crawl")
    if not last:
        return {"id": "last_crawl", "ok": False, "label": "最近一次采集",
                "detail": "还没有采集任务。真实采集需要本人小红书登录态；"
                          "本地闭环可设 CORTECH_COLLECTION_FIXTURE=1。"}
    ok = last.get("status") == "done"
    detail = (f"任务 {last.get('task_id')} 状态 {last.get('status')}，"
              f"进度 {last.get('progress')}%，入库 {last.get('notes_count')} 条")
    if last.get("progress_detail"):
        detail += f"（{last['progress_detail']}）"
    return {"id": "last_crawl", "ok": ok, "label": "最近一次采集", "detail": detail}


def build_setup_status(user_id: Optional[int] = None) -> Dict[str, Any]:
    snap = _tenant_snapshot(user_id)
    auth = _check_auth()
    checks: List[Dict[str, Any]] = [
        _check_db(),
        _check_data_dir(),
        auth,
        _check_xhs(user_id, snap["accounts"]),
        _check_topic(snap, user_id),
        _check_last_crawl(snap, user_id),
    ]
    by_id = {c["id"]: c for c in checks}
    service_ready = all(by_id[k]["ok"] for k in ("db", "data_dir", "auth"))
    collection_ready = bool(by_id["xhs_account"]["ok"]) or collection_fixture_enabled()
    xhs = by_id["xhs_account"]
    if collection_fixture_enabled() and not xhs["ok"]:
        collection_message = (
            "当前开启了本地样例采集（CORTECH_COLLECTION_FIXTURE=1），"
            "不会访问小红书。关闭该开关后，仍需要主人扫码登录才能采到真实笔记。"
        )
    elif xhs["ok"]:
        collection_message = xhs["detail"]
    else:
        collection_message = xhs["detail"]
    return {
        "product": "Cortech Chat",
        "mode": "saas",
        "ready": service_ready and bool(by_id["xhs_account"]["ok"]),
        "service_ready": service_ready,
        "auth_ready": bool(auth["ok"]),
        "xhs_accounts_healthy": int(snap["xhs_accounts_healthy"]),
        "has_topic": bool(snap["has_topic"]),
        "topic_count": int(snap["topic_count"]),
        "last_crawl": snap["last_crawl"],
        "collection_ready": collection_ready,
        "data_dir": str(DATA_DIR),
        "host": bind_host(),
        "port": bind_port(),
        "checks": checks,
        "collection": {
            "needs_real_xhs_session": not bool(by_id["xhs_account"]["ok"]),
            "fixture": collection_fixture_enabled(),
            "message": collection_message,
            "code": xhs.get("code") or ("xhs_ready" if xhs["ok"] else "need_xhs_session"),
        },
        "optional": {
            "saas": saas_mode(),
            "cookie_handshake": cookie_handshake_enabled(),
            "browser_use": browser_use_enabled(),
        },
    }
