# -*- coding: utf-8 -*-
"""多账号轮换 v2：健康检查跳过、每日配额、失败冷却。

只在当前用户已经保存的小红书账号之间轮换。不登录、不读取别人的会话。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import account_daily_quota, account_skip_sec
from . import db

_BJ = timezone(timedelta(hours=8))


def beijing_day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), _BJ).strftime("%Y-%m-%d")


def _public(acc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in acc.items() if k != "login_data_enc"}


def quota_limit(acc: Dict[str, Any]) -> int:
    custom = int(acc.get("daily_quota") or 0)
    if custom > 0:
        return custom
    return account_daily_quota()


def quota_used_today(acc: Dict[str, Any]) -> int:
    if (acc.get("quota_day") or "") != beijing_day():
        return 0
    return int(acc.get("quota_used") or 0)


def block_reason(acc: Optional[Dict[str, Any]]) -> str:
    """不能参与本轮采集时返回中文原因。可以参与则返回空字符串。不写库。

    只看已启用的 xhs_accounts。检测候选（浏览器扫描、CDP、插件暂存）不在此列。
    """
    if not acc:
        return "账号不存在"
    from .account_guard import enabled_block_reason, pace_block_reason
    enabled_msg = enabled_block_reason(acc)
    if enabled_msg:
        return enabled_msg
    if acc.get("status") != "valid":
        return "登录态不是有效，请在「账号管理」重新扫码或从插件重新确认发送"
    if "login_data_enc" in acc:
        from .login_flow import account_health
        health = account_health(acc)
        if not health.get("ok"):
            return health.get("message") or "健康检查未通过"
    pace = pace_block_reason(acc)
    if pace:
        return pace
    skip_until = int(acc.get("skip_until") or 0)
    if skip_until > int(time.time()):
        return "这个账号刚刚采集失败，先跳过，稍后再轮到它"
    if quota_used_today(acc) >= quota_limit(acc):
        return "这个账号今天的采集配额已用完，先跳过"
    return ""


def eligible_accounts() -> List[Dict[str, Any]]:
    """健康且未超配额的账号，最久未使用的在前。返回值不含登录密文。"""
    out: List[Dict[str, Any]] = []
    for acc in db.list_accounts_full():
        if block_reason(acc):
            continue
        out.append(_public(acc))
    return out


def first_eligible() -> Optional[Dict[str, Any]]:
    rows = eligible_accounts()
    return rows[0] if rows else None


def eligible_ids(preferred: Optional[int] = None, limit: int = 8) -> List[int]:
    ids = [a["id"] for a in eligible_accounts()]
    if preferred in ids:
        ids.remove(preferred)
        ids.insert(0, preferred)
    elif preferred:
        ids.insert(0, preferred)
    return ids[:limit]


def mark_login_failure(account_id: int, note: str) -> None:
    """登录失效：标过期，并在冷却时间内不再参与轮换。"""
    acc = db.get_account(account_id) or {}
    streak = int(acc.get("fail_streak") or 0) + 1
    db.update_account(account_id, {
        "status": "expired",
        "last_check_at": int(time.time()),
        "check_note": (note or "登录失效")[:180],
        "fail_streak": streak,
        "skip_until": int(time.time()) + account_skip_sec(),
    })


def mark_success(account_id: int, note_count: int) -> None:
    """采集成功：清掉失败冷却，并把本次条数计入当天配额。"""
    acc = db.get_account(account_id) or {}
    used = quota_used_today(acc) + max(0, int(note_count or 0))
    db.update_account(account_id, {
        "quota_used": used,
        "quota_day": beijing_day(),
        "fail_streak": 0,
        "skip_until": 0,
        "last_used_at": int(time.time()),
        "status": "valid",
    })
