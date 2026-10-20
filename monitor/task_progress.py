# -*- coding: utf-8 -*-
"""采集任务进度：给界面稳定的字段，长任务不回退、说明不会被节流吃掉。"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import db

STATUS_LABELS = {
    "queued": "排队中",
    "running": "采集中",
    "done": "采集完成",
    "failed": "采集失败",
    "cancelled": "已停止",
}

# 进度数字没变时，仍定期刷新 updated_at，避免长任务看起来像停住。
_HEARTBEAT_SEC = 15


def present_task(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """补齐界面轮询要用的字段。不改库。"""
    if not task:
        return task
    out = dict(task)
    status = out.get("status") or "queued"
    try:
        progress = int(out.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0
    if status == "done":
        progress = 100
    progress = max(0, min(100, progress))
    detail = (out.get("progress_detail") or out.get("log_tail") or "")
    try:
        notes_count = int(out.get("notes_count") or 0)
    except (TypeError, ValueError):
        notes_count = 0
    try:
        deleted_count = int(out.get("deleted_count") or 0)
    except (TypeError, ValueError):
        deleted_count = 0
    try:
        updated_at = int(out.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    if not updated_at:
        updated_at = int(out.get("finished_at") or out.get("started_at") or 0)
    out["status"] = status
    out["status_label"] = STATUS_LABELS.get(status, status)
    out["progress"] = progress
    out["progress_detail"] = detail
    out["notes_count"] = notes_count
    out["deleted_count"] = deleted_count
    out["error"] = out.get("error") or ""
    out["updated_at"] = updated_at
    return out


def record_task_progress(task_id: int, pct: Optional[int], detail: str = "",
                         *, deleted_count: Optional[int] = None) -> None:
    """写入进度。百分比只升不降；说明变化即使百分比不变也会落库。

    pct < 0 只更新说明，不清掉已有百分比（全量采集里的日志行用这个）。
    """
    task = db.get_task(task_id)
    if not task:
        return
    try:
        current = int(task.get("progress") or 0)
    except (TypeError, ValueError):
        current = 0
    current = max(0, min(100, current))
    if pct is None or int(pct) < 0:
        new_pct = current
    else:
        new_pct = max(current, min(100, int(pct)))
    text = (detail or "").strip()[:200]
    old_detail = task.get("progress_detail") or ""
    now = int(time.time())
    try:
        last_write = int(task.get("updated_at") or 0)
    except (TypeError, ValueError):
        last_write = 0
    fields: Dict[str, Any] = {}
    if new_pct != current or (text and text != old_detail):
        fields["progress"] = new_pct
        fields["progress_detail"] = text or old_detail
        fields["updated_at"] = now
    elif now - last_write >= _HEARTBEAT_SEC:
        fields["updated_at"] = now
    if deleted_count is not None:
        fields["deleted_count"] = int(deleted_count)
        fields["updated_at"] = now
    if fields:
        db.update_task(task_id, **fields)
