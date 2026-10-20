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

# 会话状态：idle/starting/waiting/success/expired/failed/cancelled
_manager = None
_manager_lock = threading.Lock()


class LoginSession:
    def __init__(self, account_id: int):
        self.account_id = account_id
        self.status = "starting"
        self.message = "正在启动浏览器…"
        self.qrcode_b64: str = ""  # 纯 base64（无 data: 前缀），前端拼 data:image/png;base64,
        self.started_at = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---------- 对外查询 ----------

    def snapshot(self) -> Dict:
        return {
            "account_id": self.account_id,
            "status": self.status,
            "message": self.message,
            "has_qrcode": bool(self.qrcode_b64),
            "elapsed": int(time.time() - self.started_at),
        }

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
        try:
            cookies = await ctx.cookies("https://www.xiaohongshu.com")
            for c in cookies:
                if c["name"] == "web_session":
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
        state_str = json.dumps(state_json, ensure_ascii=False)
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
        db.update_account(self.account_id, {
            "nickname": nickname or f"小红书账号{self.account_id}",
            "xhs_user_id": user_id,
            "avatar_url": avatar,
            "status": "valid",
            "login_data_enc": encrypt_login_data(state_str),
            "last_check_at": int(time.time()),
            "check_note": "扫码登录成功",
        })
        # 同时写入登录缓存目录，保证统一采集器复用
        try:
            cache = login_cache_dir()
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "storage_state.json").write_text(state_str, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self.status = "success"
        self.message = f"登录成功：{nickname or '小红书账号'}"


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

    def status(self, account_id: int) -> Dict:
        with self._lock:
            sess = self._sessions.get(account_id)
        if not sess:
            acc = db.get_account(account_id)
            return {"account_id": account_id,
                    "status": acc["status"] if acc else "idle",
                    "message": "", "has_qrcode": False, "elapsed": 0}
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
        return {"account_id": account_id, "status": "idle"}


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
        return base
    try:
        state = json.loads(decrypt_login_data(acc["login_data_enc"]))
        cookies = {c.get("name"): c.get("value")
                   for c in state.get("cookies", []) if isinstance(c, dict)}
    except Exception:  # noqa: BLE001
        base["code"] = "login_unreadable"
        base["message"] = "登录数据无法读取，请重新扫码。"
        return base
    missing = [name for name in ("a1", "web_session") if not cookies.get(name)]
    if missing:
        base["code"] = "cookie_incomplete"
        base["message"] = (
            f"登录失效：Cookie 缺少 {'、'.join(missing)}。请重新扫码。"
        )
        return base
    last = int(acc.get("last_check_at") or 0)
    age = int(time.time()) - last if last else 0
    remind = bool(last and age > 7 * 86400)
    message = "登录态有效（已检测到 a1 与 web_session）。"
    if remind:
        message = "登录态已超过 7 天未复查。建议再点一次检查；采集若失败请重新扫码。"
    return {
        "ok": True,
        "account_id": account_id,
        "status": "valid",
        "code": "login_valid",
        "message": message,
        "remind": remind,
        "rescan_path": f"/api/accounts/{account_id}/rescan",
    }


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
