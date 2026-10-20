# -*- coding: utf-8 -*-
"""账号保护：刚登录不采集、限速、固定指纹、风控暂停、只读。

采集只用已经启用、且过了保护期的账号。检测出来的登录态不在这里。
不记录 Cookie。不提权解密浏览器。
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import DATA_DIR, login_ramp_sec, login_warmup_sec, quiet_hours_spec
from . import db

logger = logging.getLogger("xhs-monitor.account_guard")

# 与登录浏览器一致的默认指纹。同一账号保存后不再改，避免每次像新设备。
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# playwright-stealth / MediaCrawler 同类初始化脚本：去掉 webdriver 标记。
STEALTH_INIT_SCRIPT = """
(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  if (!window.chrome) { window.chrome = { runtime: {} }; }
  Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US'] });
  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
  }
})();
"""

# 只读公开读取。搜索和详情在网页端是 POST，但不写数据。
READ_ENDPOINTS = {
    ("POST", "/api/sns/web/v1/search/notes"),
    ("POST", "/api/sns/web/v1/feed"),
    ("GET", "/api/sns/web/v2/comment/page"),
    ("GET", "/api/sns/web/v2/comment/sub/page"),
    ("GET", "/api/sns/web/v1/user/selfinfo"),
    ("GET", "/api/sns/web/v1/user/otherinfo"),
}


class ReadOnlyViolation(Exception):
    """采集试图访问写接口。"""


def default_fingerprint() -> Dict[str, Any]:
    return {
        "user_agent": _DEFAULT_UA,
        "viewport": {"width": 1280, "height": 800},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }


def fingerprint_for_browser(browser: str) -> Dict[str, Any]:
    fp = default_fingerprint()
    if (browser or "").lower() == "edge":
        fp["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
        )
    return fp


def _clean_fingerprint(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = default_fingerprint()
    if not isinstance(raw, dict):
        return base
    ua = raw.get("user_agent")
    if isinstance(ua, str) and ua.strip() and "cookie" not in ua.lower():
        base["user_agent"] = ua.strip()[:400]
    locale = raw.get("locale")
    if isinstance(locale, str) and locale.strip():
        base["locale"] = locale.strip()[:32]
    tz = raw.get("timezone_id")
    if isinstance(tz, str) and tz.strip():
        base["timezone_id"] = tz.strip()[:64]
    vp = raw.get("viewport") if isinstance(raw.get("viewport"), dict) else {}
    try:
        width = int(vp.get("width") or base["viewport"]["width"])
        height = int(vp.get("height") or base["viewport"]["height"])
    except (TypeError, ValueError):
        width, height = 1280, 800
    base["viewport"] = {"width": max(800, min(width, 1920)), "height": max(600, min(height, 1400))}
    return base


def fingerprint_for(account_id: Optional[int]) -> Dict[str, Any]:
    if not account_id:
        return default_fingerprint()
    acc = db.get_account(int(account_id)) or {}
    raw = acc.get("fingerprint_json") or ""
    if not raw:
        return default_fingerprint()
    try:
        saved = json.loads(raw)
    except (TypeError, ValueError):
        return default_fingerprint()
    return _clean_fingerprint(saved if isinstance(saved, dict) else None)


def remember_fingerprint(account_id: int, fingerprint: Optional[Dict[str, Any]]) -> None:
    clean = _clean_fingerprint(fingerprint)
    db.update_account(account_id, {
        "fingerprint_json": json.dumps(clean, ensure_ascii=False),
    })


def context_kwargs_from_fingerprint(fingerprint: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    fp = _clean_fingerprint(fingerprint)
    return {
        "viewport": dict(fp["viewport"]),
        "locale": fp["locale"],
        "timezone_id": fp["timezone_id"],
        "user_agent": fp["user_agent"],
    }


def apply_fingerprint_headers(headers: Dict[str, str], account_id: Optional[int]) -> Dict[str, str]:
    fp = fingerprint_for(account_id)
    out = dict(headers)
    out["user-agent"] = fp["user_agent"]
    out["accept-language"] = "zh-CN,zh;q=0.9"
    return out


def persistent_profile_dir(account_id: int) -> Path:
    path = DATA_DIR / "xhs_profiles" / str(int(account_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir_has_session(path: Path) -> bool:
    return (path / "Default").is_dir() or (path / "Local State").is_file()


def chromium_launch_attempts() -> list:
    """CORTECH_CHROMIUM_PATH 优先，否则自带 Chromium，再 Edge，再 Chrome。"""
    import os
    env = (os.environ.get("CORTECH_CHROMIUM_PATH") or "").strip()
    if env:
        return [{"executable_path": env, "label": "CORTECH_CHROMIUM_PATH"}]
    return [
        {"label": "bundled"},
        {"channel": "msedge", "label": "msedge"},
        {"channel": "chrome", "label": "chrome"},
    ]


async def launch_persistent_context(pw, user_data_dir, *, fingerprint=None,
                                    storage_state=None, headless: bool = True):
    """按固定指纹启动持久化上下文，并注入 stealth 脚本。失败会逐个换浏览器。"""
    fp = _clean_fingerprint(fingerprint)
    base = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    base.update(context_kwargs_from_fingerprint(fp))
    if storage_state and not profile_dir_has_session(Path(user_data_dir)):
        base["storage_state"] = storage_state
    last = None
    attempts = chromium_launch_attempts()
    for attempt in attempts:
        kwargs = dict(base)
        if attempt.get("executable_path"):
            kwargs["executable_path"] = attempt["executable_path"]
        if attempt.get("channel"):
            kwargs["channel"] = attempt["channel"]
        try:
            ctx = await pw.chromium.launch_persistent_context(**kwargs)
            await ctx.add_init_script(STEALTH_INIT_SCRIPT)
            return ctx
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.exception("Chromium 启动失败 label=%s", attempt.get("label"))
    if last:
        raise last
    raise RuntimeError("没有可用的浏览器")


def is_enabled(acc: Optional[Dict[str, Any]]) -> bool:
    if not acc:
        return False
    val = acc.get("enabled")
    if val is None:
        return True
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def enabled_block_reason(acc: Optional[Dict[str, Any]]) -> str:
    if acc and not is_enabled(acc):
        return "这个账号已停用，不会参与采集。可在「账号管理」重新启用或删除。"
    return ""


def in_quiet_hours(now: Optional[float] = None, spec: Optional[str] = None) -> bool:
    """北京时间安静时段。off / none 表示不启用。0-7 表示 [0, 7)。22-7 跨午夜。"""
    from datetime import datetime, timedelta, timezone
    text = (spec if spec is not None else quiet_hours_spec()).strip().lower()
    if text in ("", "off", "none", "disabled", "-", "no"):
        return False
    if "-" not in text:
        return False
    left, right = text.split("-", 1)
    try:
        start, end = int(left), int(right)
    except ValueError:
        return False
    if start == end:
        return False
    bj = timezone(timedelta(hours=8))
    hour = datetime.fromtimestamp(now if now is not None else time.time(), bj).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def pace_block_reason(acc: Optional[Dict[str, Any]], now: Optional[float] = None) -> str:
    if not acc:
        return ""
    ts = int(now if now is not None else time.time())
    warm = int(acc.get("warmup_until") or 0)
    if warm > ts:
        mins = max(1, (warm - ts + 59) // 60)
        return f"刚登录，处于保护期（约 {mins} 分钟内不采集），避免账号刚登录就被风控。"
    if in_quiet_hours(ts):
        return "当前是安静时段，这个账号先休息，不会采集。"
    risk_until = int(acc.get("risk_pause_until") or 0)
    if risk_until > ts:
        return "这个账号触发了风控信号，已自动暂停，不会立刻重试。请稍后再用，或改用其他已启用的账号。"
    return ""


def phase_of(account_id: Optional[int], now: Optional[float] = None) -> str:
    if not account_id:
        return "normal"
    acc = db.get_account(int(account_id)) or {}
    ts = int(now if now is not None else time.time())
    warm = int(acc.get("warmup_until") or 0)
    if warm > ts:
        return "warmup"
    ramp = login_ramp_sec()
    logged = int(acc.get("logged_in_at") or 0)
    ramp_end = (warm or logged) + ramp if ramp and (warm or logged) else 0
    if ramp_end and ts < ramp_end:
        return "ramp"
    return "normal"


def arm_fresh_login(account_id: int, fingerprint: Optional[Dict[str, Any]] = None) -> None:
    """用户明确登录或确认导入后调用。开始保护期，并清掉旧的风控暂停。"""
    now = int(time.time())
    fields: Dict[str, Any] = {
        "enabled": 1,
        "logged_in_at": now,
        "warmup_until": now + login_warmup_sec(),
        "risk_pause_until": 0,
        "risk_code": "",
        "fail_streak": 0,
        "skip_until": 0,
    }
    if fingerprint is not None:
        fields["fingerprint_json"] = json.dumps(_clean_fingerprint(fingerprint), ensure_ascii=False)
    db.update_account(account_id, fields)


def next_delay(account_id: Optional[int] = None, *, aggressive: bool = False,
               fallback: Optional[float] = None, rng: Optional[random.Random] = None) -> float:
    """人类间隔。慢速恢复更长。强攻也有下限，避免刚登录就被判爬虫。"""
    chooser = rng or random
    phase = phase_of(account_id)
    if phase in ("warmup", "ramp"):
        return chooser.uniform(8.0, 16.0)
    if aggressive:
        return chooser.uniform(2.0, 4.5)
    if fallback and fallback > 7.5:
        return chooser.uniform(fallback, fallback + 2.0)
    return chooser.uniform(3.0, 7.5)


def mark_risk(account_id: int, note: str, code: str = "") -> None:
    """风控信号：拉长暂停，写一条预警。重复调用不会把暂停改短，也不会立刻重试。"""
    acc = db.get_account(account_id) or {}
    streak = int(acc.get("fail_streak") or 0) + 1
    from .config import account_skip_sec
    base = account_skip_sec()
    delay = min(base * (2 ** min(streak - 1, 4)), 24 * 3600)
    until = int(time.time()) + delay
    prev = int(acc.get("risk_pause_until") or 0)
    if prev > until:
        until = prev
    text = (note or "小红书风控或访问受限，已暂停该账号")[:180]
    db.update_account(account_id, {
        "fail_streak": streak,
        "risk_pause_until": until,
        "risk_code": (code or "")[:40],
        "skip_until": until,
        "last_check_at": int(time.time()),
        "check_note": text,
    })
    try:
        from . import alerts
        alerts.init_db()
        alerts.create_alert(
            "orange",
            "采集账号已自动暂停",
            text,
            link="/settings/account",
        )
    except Exception:  # noqa: BLE001
        logger.exception("风控预警写入失败 account=%s", account_id)


def assert_readonly_endpoint(method: str, uri: str) -> None:
    path = (uri or "").split("?", 1)[0].rstrip("/") or "/"
    if (method.upper(), path) not in READ_ENDPOINTS:
        raise ReadOnlyViolation("采集只访问只读公开接口，不会点赞、关注、评论或发布。")


def public_status(acc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给账号列表的保护状态。不含 Cookie。"""
    from .account_rotation import block_reason, quota_limit, quota_used_today
    reason = block_reason(acc) if acc else "账号不存在"
    blocking = bool(reason)
    phase = "blocked"
    if not acc:
        message = reason
    elif not blocking:
        phase = phase_of(acc.get("id"))
        if phase == "ramp":
            message = "登录保护：慢速恢复中，采集间隔会更长。只读公开内容。"
        else:
            phase = "ready"
            message = "登录保护：可以采集。按账号限速，只读公开内容，不点赞、不评论、不关注。"
    else:
        message = reason
        if "保护期" in reason or "刚登录" in reason:
            phase = "warmup"
        elif "安静" in reason:
            phase = "quiet"
        elif "风控" in reason or "暂停" in reason:
            phase = "risk"
        elif "停用" in reason:
            phase = "disabled"
        elif "配额" in reason:
            phase = "quota"
    fp = _clean_fingerprint(None)
    raw = (acc or {}).get("fingerprint_json") or ""
    if raw:
        try:
            fp = _clean_fingerprint(json.loads(raw))
        except (TypeError, ValueError):
            pass
    return {
        "enabled": 1 if is_enabled(acc) else 0,
        "phase": phase,
        "blocking": blocking,
        "message": message,
        "warmup_until": int((acc or {}).get("warmup_until") or 0),
        "risk_pause_until": int((acc or {}).get("risk_pause_until") or 0),
        "quota_used": quota_used_today(acc or {}),
        "quota_limit": quota_limit(acc or {}),
        "readonly_collection": True,
        "fingerprint": {
            "locale": fp["locale"],
            "timezone_id": fp["timezone_id"],
            "viewport": fp["viewport"],
            "user_agent": fp["user_agent"],
        },
    }
