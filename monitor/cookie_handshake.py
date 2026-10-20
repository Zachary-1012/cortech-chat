# -*- coding: utf-8 -*-
"""用户本人主动提交的小红书登录态握手。

只接收调用者已经拥有、并明确确认发送的 Cookie（至少 a1 与 web_session）。
不读取他人浏览器，不代为登录，不绕过平台校验。
SaaS（CORTECH_SAAS=1）默认开启；本地演示默认关闭。
显式 CORTECH_ENABLE_COOKIE_HANDSHAKE=0 始终关闭。

保存后走与扫码相同的 Fernet 加密，并立刻做登录态健康检查。
响应只有中文说明和账号摘要，不回显任何 Cookie。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .account_crypto import encrypt_login_data
from .config import cookie_handshake_enabled, saas_mode
from .login_flow import account_health
from . import db


class HandshakeDisabled(Exception):
    """功能未打开。"""


class HandshakeRejected(Exception):
    """提交内容不能入库（面向用户的说明，不含 Cookie 值）。"""


# 采集器签名与请求会用到的小红书网页 Cookie。其余名字一律丢弃。
ALLOWED_COOKIE_NAMES = frozenset({
    "a1", "web_session", "webId", "gid", "websectiga", "sec_poison_id",
    "xsecappid", "abRequestId", "webBuild", "acw_tc", "customerClientId",
    "unread",
})

_DISABLED_MSG = (
    "Cookie 握手未开启。云上请设置 CORTECH_SAAS=1（握手默认开启），"
    "或单独设置 CORTECH_ENABLE_COOKIE_HANDSHAKE=1。"
    "只接受你本人在浏览器里已经登录、并在插件里确认发送的 Cookie。"
)


def status() -> Dict[str, Any]:
    from .browser_use_worker import status as browser_status
    browser = browser_status()
    return {
        "product": "Cortech Chat",
        "cookie_handshake": cookie_handshake_enabled(),
        "saas": saas_mode(),
        "browser_use": browser["enabled"],
        "browser_use_profile_ready": browser["profile_ready"],
        "browser_use_queued": browser["queued"],
        "browser_use_inflight": browser["inflight"],
        "consent": (
            "仅接受你本人已经登录并主动确认发送的 Cookie，"
            "或你自己指定的、已经登录过的本地 profile。"
            "不会代登录，也不会读取别人的会话。"
        ),
        "message": (
            "握手已开启。请用 Cortech Chat 小红书助手，在你确认后再发送。"
            if cookie_handshake_enabled() else _DISABLED_MSG
        ),
    }


def _require_enabled() -> None:
    if not cookie_handshake_enabled():
        raise HandshakeDisabled(_DISABLED_MSG)


def _clean_value(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise HandshakeRejected("Cookie 格式不正确。请从插件重新确认发送，不要手写整段请求头。")
    value = value.strip()
    if not value or len(value) > 4096:
        raise HandshakeRejected(f"{name} 为空或过长。请在浏览器里重新登录小红书后再试。")
    if any(tok in value for tok in ("\n", "\r", "\x00")):
        raise HandshakeRejected("请分别提交各项 Cookie，不要粘贴整段未拆分的请求头。")
    return value


def _collect_cookies(a1: str, web_session: str,
                     extra: Optional[Dict[str, Any]]) -> Dict[str, str]:
    raw: Dict[str, Any] = {}
    if isinstance(extra, dict):
        raw.update(extra)
    raw["a1"] = a1
    raw["web_session"] = web_session
    out: Dict[str, str] = {}
    for name, value in raw.items():
        if name not in ALLOWED_COOKIE_NAMES:
            continue
        if value is None or value == "":
            continue
        out[name] = _clean_value(name, value)
    a1_val = out.get("a1") or ""
    web_val = out.get("web_session") or ""
    if len(a1_val) < 8 or len(web_val) < 8:
        raise HandshakeRejected("需要你本人的 a1 与 web_session，两项都不能为空。")
    if "=" in a1_val and ("web_session" in a1_val or " " in a1_val):
        raise HandshakeRejected("请分别填写 a1 与 web_session，不要粘贴整段未拆分的请求头。")
    return out


def _storage_state(cookies: Dict[str, str]) -> str:
    state = {
        "cookies": [
            {"name": name, "value": value, "domain": ".xiaohongshu.com", "path": "/"}
            for name, value in cookies.items()
        ],
        "origins": [],
    }
    return json.dumps(state, ensure_ascii=False)


def _resolve_account(account_id: Optional[int], xhs_user_id: str) -> Optional[Dict[str, Any]]:
    if account_id:
        acc = db.get_account(int(account_id))
        if not acc:
            raise HandshakeRejected("要更新的账号不存在，或不是当前登录用户的账号。")
        return acc
    if xhs_user_id:
        return db.find_account_by_xhs_user_id(xhs_user_id)
    return None


def stage_user_cookies(a1: str = "", web_session: str = "", nickname: str = "",
                       xhs_user_id: str = "",
                       extra_cookies: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """插件确认发送后只进入候选。不创建账号，采集轮换看不到。"""
    _require_enabled()
    cookies = _collect_cookies(a1, web_session, extra_cookies)
    from .browser_import import stage_detected_login
    try:
        staged = stage_detected_login(
            source="extension",
            browser="浏览器插件",
            profile="当前浏览器",
            nickname=(nickname or "").strip()[:40],
            region="www.xiaohongshu.com",
            cookies=cookies,
        )
    finally:
        cookies.clear()
    staged["xhs_user_id"] = (xhs_user_id or "").strip()[:64]
    staged["account_id"] = None
    return staged


def accept_user_cookies(a1: str = "", web_session: str = "", nickname: str = "",
                        account_id: Optional[int] = None, xhs_user_id: str = "",
                        extra_cookies: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """加密保存本人 Cookie，并立刻做健康检查。返回值不含 Cookie。"""
    _require_enabled()
    cookies = _collect_cookies(a1, web_session, extra_cookies)
    nickname = (nickname or "").strip()[:40]
    xhs_user_id = (xhs_user_id or "").strip()[:64]
    existing = _resolve_account(account_id, xhs_user_id)
    created = existing is None
    if existing is None:
        account_id = db.create_account(nickname=nickname, xhs_user_id=xhs_user_id)
    else:
        account_id = int(existing["id"])
    display = nickname or (existing or {}).get("nickname") or f"小红书账号{account_id}"
    db.update_account(account_id, {
        "nickname": display,
        "xhs_user_id": xhs_user_id or (existing or {}).get("xhs_user_id") or "",
        "status": "pending",
        "login_data_enc": encrypt_login_data(_storage_state(cookies)),
        "last_check_at": int(time.time()),
        "check_note": "正在检查你本人提交的登录态",
    })
    # 密文已落库，内存中的明文不再需要
    cookies.clear()
    acc = db.get_account(account_id) or {"id": account_id}
    health = account_health(acc)
    db.update_account(account_id, {
        "status": health["status"],
        "last_check_at": int(time.time()),
        "check_note": health["message"][:180],
    })
    if health.get("ok"):
        from .account_guard import arm_fresh_login, default_fingerprint
        arm_fresh_login(account_id, default_fingerprint())
        if created:
            message = (
                f"已加密保存你本人提交的登录态，并完成健康检查。账号「{display}」可以参加采集。"
                "接口不会回显登录态。"
            )
        else:
            message = (
                f"已更新账号「{display}」的登录态，并完成健康检查。"
                "接口不会回显登录态。"
            )
    else:
        message = (
            f"已加密保存，但健康检查未通过：{health['message']}"
            "请在浏览器里重新登录小红书后，再从插件确认发送。"
        )
    return {
        "ok": bool(health.get("ok")),
        "account_id": account_id,
        "created": created,
        "updated": not created,
        "nickname": display,
        "status": health["status"],
        "health": health["code"],
        "message": message,
    }
