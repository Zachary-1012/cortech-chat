# -*- coding: utf-8 -*-
"""签名漂移检测（REA v2 D6，四层）。

L1 被动检测：每次采集请求，零额外成本
  - HTTP 401/403/429 → PlatformAccessError（已有语义）
  - HTTP 471/461 → 滑块验证
  - 业务码 300011（账号安全限制）、300012（IP 封禁）
  - 签名失效特征：同一登录态下所有端点同时由成功转为 4xx/业务码异常

L2 主动探针：每 30 分钟 1 次，仅 2 个请求
  1. GET /api/sns/web/v1/user/selfinfo（测登录+签名）
  2. POST /api/sns/web/v1/search/notes（固定测试词，page=1, page_size=1）
  连续 3 次失败 → 红色告警"签名可能失效"，自动降级浏览器引擎

L3 Bundle 漂移监控：每天 1 次，纯静态
  - 拉取 search_result 页 HTML → 提取 xhs-pc-web index.*.js hash
  - 主 bundle hash 变 → 黄色告警"官方发版，请回归验证签名"

L4 xhshow 版本跟进：每周 1 次
  - 检查 PyPI xhshow 新版本；有新版先在 staging 跑 L2 探针回归

告警接入分级预警模块（alerts.py）。
数据表：sign_health(ts, probe_type, success, latency_ms, code)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import DATA_DIR

logger = logging.getLogger("xhs-monitor.sign_drift")

# L1：签名失效特征码
SIGN_SUSPECT_CODES = {"300011", "300012"}  # 业务码
SIGN_SUSPECT_HTTP = {401, 403}  # HTTP 状态码（429 可能是限流，单独判断）

# L2：连续失败阈值
L2_FAIL_THRESHOLD = 3
L2_PROBE_INTERVAL_SEC = 1800  # 30 分钟


def _db_path() -> Path:
    return DATA_DIR / "monitor.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """创建漂移检测表（幂等）。"""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sign_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                probe_type TEXT NOT NULL,  -- 'l1_passive' / 'l2_probe' / 'l3_bundle'
                success INTEGER NOT NULL,  -- 1/0
                latency_ms INTEGER DEFAULT 0,
                code TEXT DEFAULT '',
                detail TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sign_health_ts
            ON sign_health(ts)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bundle_track (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                bundle_hash TEXT DEFAULT '',
                sdt_hash TEXT DEFAULT '',
                changed INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def record_probe(probe_type: str, success: bool,
                 latency_ms: int = 0, code: str = "",
                 detail: str = "") -> None:
    """记录一次探针结果。"""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sign_health (ts, probe_type, success, latency_ms, code, detail)"
            " VALUES (?,?,?,?,?,?)",
            (int(time.time()), probe_type, 1 if success else 0,
             latency_ms, code, detail))
        conn.commit()


def l1_check_response(http_status: int, biz_code: str = "") -> Optional[str]:
    """L1 被动检测：分析单次响应，返回可疑原因或 None。

    签名失效特征：同一登录态下所有端点同时由成功转为 4xx/业务码异常。
    """
    if str(biz_code) in SIGN_SUSPECT_CODES:
        return f"业务码 {biz_code}（账号安全限制/IP封禁）"
    if http_status in SIGN_SUSPECT_HTTP:
        return f"HTTP {http_status}（签名可能失效）"
    if http_status in (471, 461):
        return f"HTTP {http_status}（滑块验证，需人工处理）"
    return None


def l2_recent_failures(window_sec: int = 3600) -> int:
    """L2：统计最近 window_sec 内探针连续失败次数。"""
    init_db()
    since = int(time.time()) - window_sec
    with _connect() as conn:
        rows = conn.execute(
            "SELECT success FROM sign_health"
            " WHERE probe_type='l2_probe' AND ts>=?"
            " ORDER BY ts DESC LIMIT ?",
            (since, L2_FAIL_THRESHOLD)).fetchall()
    fails = 0
    for r in rows:
        if r["success"]:
            break
        fails += 1
    return fails


def l2_should_alert() -> bool:
    """L2：连续 3 次探针失败 → 红色告警。"""
    return l2_recent_failures() >= L2_FAIL_THRESHOLD


async def l2_probe(account_id: Optional[int] = None) -> Dict:
    """L2 主动探针：2 个请求，测登录+签名+搜索链路。

    返回 {success, latency_ms, detail}。失败时记库，连续3次失败触发告警。
    """
    import asyncio
    from .adapters.cortech_direct import _import_vendor, _read_login_cookies, _BASE_HEADERS

    t0 = time.time()
    try:
        XiaoHongShuClient, _, excs = _import_vendor()
        cookie_str, cookie_dict = await _read_login_cookies(
            None if not account_id else {"account_id": account_id})
        if "a1" not in cookie_dict:
            # v2 铁律：a1 是签名锚点，缺了直接抛错
            raise RuntimeError("缺少 a1（签名锚点），登录态无效")
        headers = dict(_BASE_HEADERS)
        headers["Cookie"] = cookie_str
        client = XiaoHongShuClient(headers=headers, playwright_page=None,
                                   cookie_dict=cookie_dict)
        # 探针1：pong（最便宜）
        pong_ok = await client.pong()
        if not pong_ok:
            raise RuntimeError("pong 失败（登录态失效）")
        # 探针2：搜索（固定测试词，page_size=1）
        # v2 参数：sort=time_descending, note_type=0
        from media_platform.xhs.field import SearchNoteType, SearchSortType
        res = await client.get_note_by_keyword(
            keyword="测试", page=1, page_size=1,
            sort=SearchSortType.LATEST, note_type=SearchNoteType.ALL)
        latency = int((time.time() - t0) * 1000)
        record_probe("l2_probe", True, latency, "", "pong+search ok")
        return {"success": True, "latency_ms": latency, "detail": "pong+search 正常"}
    except Exception as e:  # noqa: BLE001
        latency = int((time.time() - t0) * 1000)
        detail = f"{type(e).__name__}: {e}"
        record_probe("l2_probe", False, latency, "", detail[:200])
        logger.warning("[sign_drift] L2 探针失败：%s", detail[:100])
        # 连续3次失败 → 红色告警 + 自动降级标记
        if l2_should_alert():
            _fire_sign_alert(detail)
        return {"success": False, "latency_ms": latency, "detail": detail[:200]}


def _fire_sign_alert(detail: str) -> None:
    """L2 红色告警：签名可能失效，接入分级预警模块。"""
    try:
        from . import alerts
        alerts.create_alert(
            level="critical",
            title="签名可能失效（direct-api）",
            message=f"连续 {L2_FAIL_THRESHOLD} 次探针失败，direct-api 签名可能已漂移。"
                    f"已自动降级为浏览器引擎。详情：{detail[:100]}",
            topic_id=None,
        )
        logger.error("[sign_drift] 已触发红色告警：签名可能失效")
    except Exception as e:  # noqa: BLE001
        logger.error("[sign_drift] 告警写入失败：%s", e)


def get_sign_status() -> Dict:
    """签名健康状态（供前端/任务详情展示）。"""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sign_health ORDER BY ts DESC LIMIT 1").fetchone()
        recent = conn.execute(
            "SELECT success FROM sign_health WHERE probe_type='l2_probe'"
            " ORDER BY ts DESC LIMIT 10").fetchall()
    if not row:
        return {"status": "unknown", "message": "暂无探针数据"}
    fails = l2_recent_failures()
    if fails >= L2_FAIL_THRESHOLD:
        return {"status": "red", "message": "签名可能失效，已降级浏览器引擎",
                "consecutive_failures": fails}
    if fails > 0:
        return {"status": "yellow", "message": f"探针连续失败 {fails} 次，关注中",
                "consecutive_failures": fails}
    return {"status": "green", "message": "签名正常",
            "last_check": row["ts"], "latency_ms": row["latency_ms"]}
