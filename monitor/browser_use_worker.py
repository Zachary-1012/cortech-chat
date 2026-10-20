# -*- coding: utf-8 -*-
"""可选的浏览器会话采集：只用用户已经登录好的 profile。

CORTECH_ENABLE_BROWSER_USE 默认关闭。
开启后只读取 CORTECH_BROWSER_USE_PROFILE 里已经存在的登录态
（storage_state.json，或该目录里已有的小红书 Cookie）。
不打开登录页，不提交账号密码，不扫码。

同时跑的任务数和排队长度由环境变量限制。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .config import (browser_use_concurrency, browser_use_enabled,
                     browser_use_queue_max)


class BrowserUseError(Exception):
    """面向用户的说明，不含 Cookie。"""


class BrowserUseDisabled(BrowserUseError):
    pass


class BrowserUseRejected(BrowserUseError):
    pass


class _Gate:
    def __init__(self, concurrency: int, queue_max: int):
        self.concurrency = concurrency
        self.queue_max = queue_max
        self.sem = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.queued = 0
        self.inflight = 0

    def acquire(self) -> None:
        with self.lock:
            if self.inflight < self.concurrency and self.sem.acquire(blocking=False):
                self.inflight += 1
                return
            if self.queued >= self.queue_max:
                raise BrowserUseRejected("浏览器会话队列已满，请稍后再试。")
            self.queued += 1
        try:
            self.sem.acquire()
        except BaseException:
            with self.lock:
                self.queued -= 1
            raise
        with self.lock:
            self.queued -= 1
            self.inflight += 1

    def release(self) -> None:
        with self.lock:
            if self.inflight:
                self.inflight -= 1
        self.sem.release()


_gate_lock = threading.Lock()
_gate: Optional[_Gate] = None


def reset_for_tests() -> None:
    global _gate
    with _gate_lock:
        _gate = None


def _gate_get() -> _Gate:
    global _gate
    concurrency = browser_use_concurrency()
    queue_max = browser_use_queue_max()
    with _gate_lock:
        if _gate is None or _gate.concurrency != concurrency or _gate.queue_max != queue_max:
            _gate = _Gate(concurrency, queue_max)
        return _gate


def profile_path() -> Path:
    raw = (os.environ.get("CORTECH_BROWSER_USE_PROFILE") or "").strip()
    if not raw:
        raise BrowserUseRejected(
            "未设置 CORTECH_BROWSER_USE_PROFILE。"
            "请指向你已经登录小红书的 profile 目录。服务端不会代为登录。"
        )
    return Path(raw).expanduser()


def ensure_ready() -> Path:
    """确认开关打开，且指定目录里已经有本人登录态。"""
    if not browser_use_enabled():
        raise BrowserUseDisabled(
            "浏览器会话采集未开启。需要时设置 CORTECH_ENABLE_BROWSER_USE=1，"
            "并把 CORTECH_BROWSER_USE_PROFILE 指到你已经登录的目录。"
            "服务端不会代为登录。"
        )
    path = profile_path()
    if not path.exists():
        raise BrowserUseRejected(
            "指定的 profile 目录不存在。请先在自己的浏览器登录小红书，再填写那个目录。"
        )
    from .browser_import import load_profile_storage_state
    try:
        load_profile_storage_state(path)
    except Exception as e:  # noqa: BLE001
        text = str(e).strip() or "指定目录里没有可用的登录态"
        raise BrowserUseRejected(text) from None
    return path


def can_use() -> bool:
    try:
        ensure_ready()
    except BrowserUseError:
        return False
    return True


def has_capacity() -> bool:
    if not browser_use_enabled():
        return False
    gate = _gate_get()
    with gate.lock:
        if gate.inflight < gate.concurrency:
            return True
        return gate.queued < gate.queue_max


def status() -> Dict[str, Any]:
    enabled = browser_use_enabled()
    ready = False
    if not enabled:
        message = (
            "浏览器会话采集未开启。需要时设置 CORTECH_ENABLE_BROWSER_USE=1，"
            "并指向你已经登录的 profile。不会代为登录。"
        )
    else:
        try:
            ensure_ready()
            ready = True
            message = "已找到你指定目录里的登录会话。采集时只会复用它，不会代为登录。"
        except BrowserUseError as e:
            message = str(e)
    gate = _gate if _gate is not None else None
    return {
        "enabled": enabled,
        "profile_ready": ready,
        "concurrency": browser_use_concurrency(),
        "queue_max": browser_use_queue_max(),
        "queued": gate.queued if gate else 0,
        "inflight": gate.inflight if gate else 0,
        "message": message,
    }


def _write_temp(state: Dict[str, Any]) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="cortech_browser_use_"))
    (dest / "storage_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return dest


@contextmanager
def session_scope() -> Iterator[Path]:
    """占一个并发名额，得到只含已有登录态的目录。退出时释放名额。"""
    ensure_ready()
    if not has_capacity():
        raise BrowserUseRejected("浏览器会话队列已满，请稍后再试。")
    gate = _gate_get()
    gate.acquire()
    temp: Optional[Path] = None
    try:
        from .browser_import import load_profile_storage_state
        state = load_profile_storage_state(profile_path())
        temp = _write_temp(state)
        yield temp
    finally:
        gate.release()
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
