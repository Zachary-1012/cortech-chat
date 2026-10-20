# -*- coding: utf-8 -*-
"""SaaS 首次启动自检：库、数据目录、登录组件、小红书采集账号。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import (DATA_DIR, bind_host, bind_port, browser_use_enabled,
                     collection_fixture_enabled, cookie_handshake_enabled,
                     ensure_dirs)
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


def _check_xhs(user_id: Optional[int]) -> Dict[str, Any]:
    if user_id is None:
        return {
            "id": "xhs_account",
            "ok": False,
            "label": "小红书采集账号",
            "detail": "尚未登录 Cortech Chat。注册后请在「账号管理」用本人小红书账号扫码。",
            "code": "need_login",
        }
    db.set_current_user(user_id)
    try:
        accounts = db.list_accounts()
    finally:
        db.set_current_user(None)
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


def build_setup_status(user_id: Optional[int] = None) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = [
        _check_db(),
        _check_data_dir(),
        _check_auth(),
        _check_xhs(user_id),
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
            "cookie_handshake": cookie_handshake_enabled(),
            "browser_use": browser_use_enabled(),
        },
    }
