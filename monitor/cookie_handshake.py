# -*- coding: utf-8 -*-
"""用户本人主动提交的小红书登录态握手。

只接收调用者已经拥有、并明确粘贴的 Cookie（a1 与 web_session）。
不读取他人浏览器，不代为登录，不绕过平台校验。
默认关闭，需 CORTECH_ENABLE_COOKIE_HANDSHAKE=1。

CORTECH_ENABLE_BROWSER_USE=1 时，只报告操作者自己指定的本地 profile 目录是否存在，
不启动自动化去登录小红书。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from .account_crypto import encrypt_login_data
from .config import browser_use_enabled, cookie_handshake_enabled
from . import db


class HandshakeDisabled(Exception):
    """功能未打开。"""


class HandshakeRejected(Exception):
    """提交内容不能入库（面向用户的说明，不含 Cookie 值）。"""


def status() -> Dict[str, Any]:
    profile = (os.environ.get("CORTECH_BROWSER_USE_PROFILE") or "").strip()
    profile_ok = False
    if browser_use_enabled() and profile:
        p = Path(profile).expanduser()
        profile_ok = (p / "storage_state.json").is_file() or p.is_dir()
    return {
        "cookie_handshake": cookie_handshake_enabled(),
        "browser_use": browser_use_enabled(),
        "browser_use_profile_ready": profile_ok,
        "consent": "仅接受你本人已经登录并主动提交的 Cookie，或你自己指定的本地 profile。不会代登录，也不会读取别人的会话。",
    }


def _require_enabled() -> None:
    if not cookie_handshake_enabled():
        raise HandshakeDisabled(
            "Cookie 握手未开启。在服务器设置 CORTECH_ENABLE_COOKIE_HANDSHAKE=1 后，"
            "只能提交你本人小红书网页里已经存在的 a1 与 web_session。"
        )


def accept_user_cookies(a1: str, web_session: str, nickname: str = "") -> Dict[str, Any]:
    """把用户粘贴的本人 Cookie 加密成与扫码登录相同的 storage_state。"""
    _require_enabled()
    a1 = (a1 or "").strip()
    web_session = (web_session or "").strip()
    if len(a1) < 8 or len(web_session) < 8:
        raise HandshakeRejected("需要你本人的 a1 与 web_session，且都不能为空。")
    if any(tok in a1 or tok in web_session for tok in ("\n", "\r", " ")) and "=" in a1:
        raise HandshakeRejected("请分别填写 a1 与 web_session 两个值，不要粘贴整段未拆分的请求头。")
    state = {
        "cookies": [
            {"name": "a1", "value": a1, "domain": ".xiaohongshu.com", "path": "/"},
            {"name": "web_session", "value": web_session,
             "domain": ".xiaohongshu.com", "path": "/"},
        ],
        "origins": [],
    }
    account_id = db.create_account(nickname=(nickname or "").strip()[:40])
    db.update_account(account_id, {
        "nickname": (nickname or "").strip() or f"小红书账号{account_id}",
        "status": "valid",
        "login_data_enc": encrypt_login_data(json.dumps(state, ensure_ascii=False)),
        "last_check_at": int(time.time()),
        "check_note": "由你本人提交的 Cookie 握手写入。服务端未代为登录。",
    })
    return {
        "ok": True,
        "account_id": account_id,
        "status": "valid",
        "message": "已加密保存你提交的登录态。请再用「检查登录」确认；失效后请重新扫码或重新提交。",
    }
