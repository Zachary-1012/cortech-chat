# -*- coding: utf-8 -*-
"""小红书接口失败语义。

把业务 code（尤其是 -3 / 404 一类）翻译成可展示的中文，并区分：
- deleted：笔记已删除或不可浏览，采集应记一笔，不算任务失败
- login：登录态失效，需要本人重新扫码
- risk：风控或访问受限
- fetch：其余采集失败
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 网页端常见：笔记不存在、已删除、不可浏览
DELETED_CODES = {-3, -404, 404, -10404, 300013}
LOGIN_CODES = {-100, -101}
RISK_CODES = {300012, 300015, 300017}

_DELETED_HINTS = ("不存在", "已删除", "无法浏览", "not found", "note not found")


def _as_int(code: Any) -> Optional[int]:
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def classify_biz_code(code: Any, msg: str = "") -> Tuple[str, str]:
    """返回 (kind, human_message)。kind: deleted | login | risk | fetch。"""
    code_i = _as_int(code)
    text = str(msg or "").strip()
    low = text.lower()
    shown = text or "无补充说明"
    if code_i in DELETED_CODES or any(h in low or h in text for h in _DELETED_HINTS):
        return (
            "deleted",
            f"笔记不存在或已删除（接口 code={code}）。已标记为删除，不计入采集失败。",
        )
    if code_i in LOGIN_CODES or "登录已过期" in text or "登录失效" in text:
        return (
            "login",
            f"小红书登录失效（code={code}）。请在「账号管理」用本人账号重新扫码后再采集。",
        )
    if code_i in RISK_CODES:
        return (
            "risk",
            f"小红书风控或访问受限（code={code}）。请降低频率，或改用另一个已登录的采集账号。",
        )
    return ("fetch", f"采集接口失败（code={code}）：{shown}")


def classify_http_status(status: int, on_detail: bool) -> Optional[Tuple[str, str]]:
    """HTTP 状态。详情接口的 404 视为删除；其它 None 表示继续看业务 code。"""
    if status == 404 and on_detail:
        return (
            "deleted",
            "笔记不存在或已删除（HTTP 404）。已标记为删除，不计入采集失败。",
        )
    if status in (401, 403):
        return (
            "login",
            f"小红书登录失效（HTTP {status}）。请在「账号管理」用本人账号重新扫码后再采集。",
        )
    return None


def share_count_meta(interact: Dict[str, Any] | None) -> Tuple[int, bool, Dict[str, Any]]:
    """网页端通常没有分享数。缺字段时记 metadata，不用 0 假装采到了分享数。"""
    inter = interact or {}
    if "share_count" in inter and inter.get("share_count") not in (None, ""):
        try:
            count = int(float(str(inter.get("share_count")).strip()))
        except (TypeError, ValueError):
            count = 0
        return count, True, {
            "share_count": {"available": True, "source": "api"},
        }
    return 0, False, {
        "share_count": {
            "available": False,
            "source": "not_on_web",
            "reason": "小红书网页端不返回分享数，0 不代表真实分享为 0",
        },
    }


def engagement_of(note: Dict[str, Any]) -> Tuple[int, str]:
    """互动量。网页端明确没有分享数时，不把 0 算进互动，文案标明未返回。"""
    liked = int(note.get("liked_count") or 0)
    collected = int(note.get("collected_count") or 0)
    comments = int(note.get("comment_count") or 0)
    share = int(note.get("share_count") or 0)
    flag = note.get("share_count_available")
    if flag in (0, False, "0"):
        total = liked + collected + comments
        share_text = "分享（网页端未返回）"
    else:
        total = liked + collected + comments + share
        share_text = f"分享{share}"
    return total, f"点赞{liked}/收藏{collected}/评论{comments}/{share_text}"


def failure_kind_from_message(message: str) -> str:
    text = message or ""
    if any(s in text for s in ("登录失效", "重新扫码", "Cookie 缺少", "尚未扫码", "无登录数据")):
        return "login"
    if "风控" in text or "访问受限" in text or "账号受限" in text:
        return "risk"
    if "已删除" in text or "不存在" in text:
        return "deleted"
    return "fetch"
