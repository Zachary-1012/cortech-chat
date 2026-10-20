# -*- coding: utf-8 -*-
"""站内扫码登录：二维码在网站弹窗内展示，后端驱动 headless 浏览器，前端轮询状态。

流程：POST /api/login/start → 后台线程启动 headless Chromium →
打开小红书登录页 → 提取二维码 base64 → 前端弹窗展示 →
用户用小红书 App 扫码 → 后端检测到登录成功 → 加密保存登录态到账号。

全程不跳外部浏览器、不开新窗口。采集时浏览器引擎同样强制 headless。
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from . import db
from .account_crypto import decrypt_login_data, encrypt_login_data
from .config import login_cache_dir

# 会话内部状态：idle/starting/waiting/success/expired/failed/cancelled
# 接口对外只暴露 waiting / success / expired / need_rescan，并带中文说明。
PUBLIC_LOGIN_STATES = ("waiting", "success", "expired", "need_rescan")
STATE_LABELS = {
    "waiting": "等待扫码",
    "success": "登录成功",
    "expired": "二维码已过期",
    "need_rescan": "需要重新扫码",
}
_SESSION_STATE = {
    "idle": "need_rescan",
    "starting": "waiting",
    "waiting": "waiting",
    "success": "success",
    "expired": "expired",
    "failed": "need_rescan",
    "cancelled": "need_rescan",
}
_ACCOUNT_STATE = {
    "valid": "success",
    "pending": "waiting",
    "expired": "need_rescan",
}

_manager = None
_manager_lock = threading.Lock()
# 测试可关掉，避免扫码流程拉起浏览器。
_launch_browser = True


def set_launch_browser(enabled: bool) -> None:
    """单测关闭后，登录会话停在等待扫码，不启动 Playwright。"""
    global _launch_browser
    _launch_browser = bool(enabled)


def launch_browser_enabled() -> bool:
    return _launch_browser


def login_state_payload(account_id, state: str, message: str, *,
                        status: str = "", ok: Optional[bool] = None,
                        has_qrcode: bool = False, elapsed: int = 0) -> Dict:
    """登录 / 二维码 / 重扫接口共用的状态。"""
    if state not in STATE_LABELS:
        state = "need_rescan"
    if ok is None:
        ok = state == "success"
    return {
        "account_id": account_id,
        "status": status or state,
        "state": state,
        "state_label": STATE_LABELS[state],
        "message": message or STATE_LABELS[state],
        "ok": bool(ok),
        "has_qrcode": bool(has_qrcode),
        "elapsed": int(elapsed),
    }


def account_state_payload(acc: Optional[dict]) -> Dict:
    """没有进行中的扫码会话时，用账号表上的状态。"""
    if not acc:
        return login_state_payload(
            None, "need_rescan", "账号不存在", status="idle", ok=False)
    status = acc.get("status") or "pending"
    state = _ACCOUNT_STATE.get(status, "need_rescan")
    note = (acc.get("check_note") or "").strip()
    if note:
        message = note
    elif state == "waiting":
        message = "请用小红书 App 扫码登录（120 秒内有效）"
    elif state == "success":
        message = "登录成功"
    else:
        message = "需要重新扫码。请在「账号管理」用本人小红书账号再次登录。"
    return login_state_payload(
        acc.get("id"), state, message, status=status, ok=state == "success")


class LoginSession:
    def __init__(self, account_id: int):
        self.account_id = account_id
        self.status = "starting"
        self.message = "正在启动浏览器…"
        self.qrcode_b64: str = ""  # 纯 base64（无 data: 前缀），前端拼 data:image/png;base64,
        self.started_at = time.time()
        self._stop = threading.Event()
        self._thread = None
        if _launch_browser:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            self.status = "waiting"
            self.message = "请用小红书 App 扫码登录（120 秒内有效）"

    # ---------- 对外查询 ----------

    def snapshot(self) -> Dict:
        state = _SESSION_STATE.get(self.status, "need_rescan")
        return login_state_payload(
            self.account_id, state, self.message, status=self.status,
            has_qrcode=bool(self.qrcode_b64),
            elapsed=int(time.time() - self.started_at),
        )

    def mark(self, status: str, message: Optional[str] = None,
             qrcode_b64: Optional[str] = None) -> Dict:
        """测试或外部驱动推进状态机，不碰浏览器。"""
        self.status = status
        if message is not None:
            self.message = message
        if qrcode_b64 is not None:
            self.qrcode_b64 = qrcode_b64
        return self.snapshot()

    def cancel(self):
        self._stop.set()
        if self.status in ("starting", "waiting"):
            self.status = "cancelled"
            self.message = "已取消登录"

    # ---------- 后台流程 ----------

    def _run(self):
        try:
            asyncio.run(self._async_run())
        except Exception as e:  # noqa: BLE001
            self.status = "failed"
            self.message = f"登录流程异常：{type(e).__name__}"

    async def _async_run(self):
        from playwright.async_api import async_playwright

        tmp_profile = Path(tempfile.mkdtemp(prefix="xhs_login_"))
        try:
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(tmp_profile),
                    headless=True,  # 铁律：不许弹出浏览器窗口
                    executable_path="/opt/meta-chromium/chrome",  # 临时：Brio VM 用 meta-chromium
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 800},
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto("https://www.xiaohongshu.com/explore",
                                    wait_until="domcontentloaded", timeout=30000)
                    # 点登录按钮（如未自动弹出登录框）
                    try:
                        btn = page.locator(
                            "xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button")
                        if await btn.count() and await btn.first.is_visible(timeout=3000):
                            await btn.first.click()
                    except Exception:  # noqa: BLE001
                        pass
                    # 提取二维码
                    qr = await self._extract_qrcode(page)
                    if not qr:
                        self.status = "failed"
                        self.message = "未找到登录二维码，小红书页面可能改版，请稍后重试"
                        return
                    self.qrcode_b64 = qr
                    self.status = "waiting"
                    self.message = "请用小红书 App 扫码登录（120 秒内有效）"
                    # 记录登录前的 web_session，用于检测变化
                    before = await self._web_session(ctx)
                    # 轮询登录状态
                    ok = await self._wait_login(ctx, page, before)
                    if self._stop.is_set():
                        return
                    if not ok:
                        self.status = "expired"
                        self.message = "二维码已过期，请重新获取"
                        return
                    # 成功：保存登录态
                    await self._save_login_state(ctx, page)
                finally:
                    await ctx.close()
        finally:
            import shutil
            shutil.rmtree(tmp_profile, ignore_errors=True)

    async def _extract_qrcode(self, page) -> str:
        """提取 img.qrcode-img 的 base64（兼容 data:URL 与 http URL）"""
        for _ in range(20):
            if self._stop.is_set():
                return ""
            try:
                el = await page.wait_for_selector(
                    "xpath=//img[@class='qrcode-img']", timeout=3000)
                src = await el.get_attribute("src") or ""
                if src.startswith("data:image"):
                    return src.split(",", 1)[1] if "," in src else src
                if src.startswith("http"):
                    import httpx
                    async with httpx.AsyncClient(timeout=15) as cli:
                        r = await cli.get(src)
                        if r.status_code == 200:
                            return base64.b64encode(r.content).decode("ascii")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)
        return ""

    async def _web_session(self, ctx) -> str:
        """登录成功看 web_session。www 之外，.cn / .hk / edith 上的同名 Cookie 也算。"""
        try:
            from .xhs_hosts import collect_context_cookies
            cookies = await collect_context_cookies(ctx)
            for c in cookies:
                if c.get("name") == "web_session" and c.get("value"):
                    return c["value"]
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _wait_login(self, ctx, page, before: str) -> bool:
        """120 秒轮询：web_session 变化或出现'我'的个人主页链接即成功"""
        for _ in range(60):
            if self._stop.is_set():
                return False
            try:
                sel = "xpath=//a[contains(@href, '/user/profile/')]"
                if await page.is_visible(sel, timeout=500):
                    return True
            except Exception:  # noqa: BLE001
                pass
            cur = await self._web_session(ctx)
            if cur and cur != before:
                return True
            await asyncio.sleep(2)
        return False

    async def _save_login_state(self, ctx, page):
        state_json = await ctx.storage_state()
        # 提取昵称/用户ID
        nickname, user_id, avatar = "", "", ""
        try:
            sel = "xpath=//a[contains(@href, '/user/profile/')]"
            el = page.locator(sel).first
            href = await el.get_attribute("href") or ""
            if "/user/profile/" in href:
                user_id = href.split("/user/profile/")[-1].split("?")[0].strip()
            nickname = (await el.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        apply_successful_login(
            self.account_id, state_json,
            nickname=nickname, xhs_user_id=user_id, avatar_url=avatar,
        )


def get_manager() -> "_LoginManager":
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = _LoginManager()
        return _manager


class _LoginManager:
    def __init__(self):
        self._sessions: Dict[int, LoginSession] = {}
        self._lock = threading.Lock()

    def start(self, account_id: int) -> Dict:
        with self._lock:
            old = self._sessions.get(account_id)
            if old and old.status in ("starting", "waiting"):
                return old.snapshot()
            sess = LoginSession(account_id)
            self._sessions[account_id] = sess
            return sess.snapshot()

    def get_session(self, account_id: int) -> Optional[LoginSession]:
        with self._lock:
            return self._sessions.get(account_id)

    def status(self, account_id: int) -> Dict:
        with self._lock:
            sess = self._sessions.get(account_id)
        if not sess:
            return account_state_payload(db.get_account(account_id))
        return sess.snapshot()

    def qrcode(self, account_id: int) -> Optional[str]:
        with self._lock:
            sess = self._sessions.get(account_id)
        return sess.qrcode_b64 if sess else None

    def cancel(self, account_id: int) -> Dict:
        with self._lock:
            sess = self._sessions.get(account_id)
        if sess:
            sess.cancel()
            return sess.snapshot()
        return login_state_payload(account_id, "need_rescan", "已取消，需要重新扫码",
                                   status="idle", ok=False)

    def clear(self, account_id: int) -> None:
        with self._lock:
            self._sessions.pop(account_id, None)

    def clear_all(self) -> None:
        with self._lock:
            for sess in self._sessions.values():
                sess.cancel()
            self._sessions.clear()


def _finish_health(payload: dict) -> dict:
    state = "success" if payload.get("ok") else "need_rescan"
    payload["state"] = state
    payload["state_label"] = STATE_LABELS[state]
    return payload


def apply_successful_login(account_id: int, storage_state: dict, *,
                           nickname: str = "", xhs_user_id: str = "",
                           avatar_url: str = "") -> Dict:
    """扫码成功后的落库。账号变为 valid，进行中的会话变为 success。"""
    nickname = (nickname or "").strip()
    display = nickname or f"小红书账号{account_id}"
    state_str = json.dumps(storage_state, ensure_ascii=False)
    db.update_account(account_id, {
        "nickname": display,
        "xhs_user_id": (xhs_user_id or "").strip(),
        "avatar_url": avatar_url or "",
        "status": "valid",
        "login_data_enc": encrypt_login_data(state_str),
        "last_check_at": int(time.time()),
        "check_note": "扫码登录成功",
    })
    try:
        cache = login_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "storage_state.json").write_text(state_str, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sess = get_manager().get_session(account_id)
    if sess:
        sess.status = "success"
        sess.message = f"登录成功：{display}"
    return get_manager().status(account_id)


def account_health(acc: dict) -> dict:
    """登录态健康检查。只看已保存的 Cookie 是否齐全，不向小红书发请求。"""
    account_id = acc.get("id")
    base = {
        "ok": False,
        "account_id": account_id,
        "status": "expired",
        "code": "no_login_data",
        "message": "无登录数据，请在「账号管理」重新扫码。",
        "remind": True,
        "rescan_path": f"/api/accounts/{account_id}/rescan",
    }
    if not acc.get("login_data_enc"):
        return _finish_health(base)
    try:
        state = json.loads(decrypt_login_data(acc["login_data_enc"]))
        cookies = {c.get("name"): c.get("value")
                   for c in state.get("cookies", []) if isinstance(c, dict)}
    except Exception:  # noqa: BLE001
        base["code"] = "login_unreadable"
        base["message"] = "登录数据无法读取，请重新扫码。"
        return _finish_health(base)
    missing = [name for name in ("a1", "web_session") if not cookies.get(name)]
    if missing:
        base["code"] = "cookie_incomplete"
        base["message"] = (
            f"登录失效：Cookie 缺少 {'、'.join(missing)}。请重新扫码。"
        )
        return _finish_health(base)
    last = int(acc.get("last_check_at") or 0)
    age = int(time.time()) - last if last else 0
    remind = bool(last and age > 7 * 86400)
    message = "登录态有效（已检测到 a1 与 web_session）。"
    if remind:
        message = "登录态已超过 7 天未复查。建议再点一次检查；采集若失败请重新扫码。"
    return _finish_health({
        "ok": True,
        "account_id": account_id,
        "status": "valid",
        "code": "login_valid",
        "message": message,
        "remind": remind,
        "rescan_path": f"/api/accounts/{account_id}/rescan",
    })


def inspect_account(account_id: int) -> dict:
    """检查账号。扫码还在等待时不把账号写成失效。"""
    acc = db.get_account(account_id)
    if not acc:
        raise KeyError(account_id)
    sess = get_manager().get_session(account_id)
    now = int(time.time())
    if sess and sess.status in ("starting", "waiting", "success"):
        return sess.snapshot()
    if sess and sess.status in ("expired", "failed", "cancelled"):
        if acc.get("status") == "pending":
            db.update_account(account_id, {
                "last_check_at": now,
                "check_note": (sess.message or "")[:180],
            })
        return sess.snapshot()
    health = account_health(acc)
    db.update_account(account_id, {
        "status": health["status"],
        "last_check_at": now,
        "check_note": health["message"][:180],
    })
    return health


def decorate_account(acc: dict) -> dict:
    """账号列表附加对外状态，不改库存的 status（界面仍用 valid/pending/expired）。"""
    view = dict(acc)
    sess = get_manager().get_session(acc.get("id"))
    snap = sess.snapshot() if sess else account_state_payload(acc)
    view["state"] = snap["state"]
    view["state_label"] = snap["state_label"]
    view["login_message"] = snap["message"]
    return view


def decrypt_profile_to_dir(account_id: int, dest: Path) -> bool:
    """把账号登录态解密写入临时 profile 目录，供采集引擎使用"""
    acc = db.get_account(account_id)
    if not acc or not acc.get("login_data_enc"):
        return False
    try:
        state_str = decrypt_login_data(acc["login_data_enc"])
    except Exception:  # noqa: BLE001
        return False
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "storage_state.json").write_text(state_str, encoding="utf-8")
    return True
