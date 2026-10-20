# -*- coding: utf-8 -*-
"""从本地浏览器导入小红书登录态（需用户明确授权）。

安全铁律（用户亲定）：
1. 绝不自动扫描/读取：scan_browser_logins() 只在用户点击"从浏览器导入"后调用，
   后端不做任何定时任务、后台扫描或开机自检。
2. Cookie 只存内存：检测接口只返回昵称/头像/uid，不返回、不记录任何 Cookie 值；
   解密后的 Cookie 不写日志、不落盘。
3. 二次确认：import_login() 必须由用户在检测列表中选择具体账号并确认后才调用。
4. 导入后走 account_crypto Fernet 加密入库，与扫码登录同一存储。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("xhs-monitor.browser_import")


class BrowserImportError(Exception):
    """面向用户的可展示错误（不含敏感信息）"""


class CookieProtectError(BrowserImportError):
    """v20 或数据库被占用。code 给接口，message 给人看。不含 Cookie。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


V20_MESSAGE = (
    "Chrome 或 Edge 启用了 App-Bound Encryption（Cookie 前缀 v20）。"
    "这里不会把密文当成明文，也不会用提权去冒充浏览器解密。"
    "请安装「Cortech Chat 小红书助手」，在「账号管理」勾选并确认后再导入；"
    "或完全退出浏览器后重试；也可以改用扫码登录。"
)
LOCK_MESSAGE = (
    "浏览器正在运行，Cookies 数据库被独占锁定，无法复制。"
    "请安装「Cortech Chat 小红书助手」，在「账号管理」勾选并确认后再导入；"
    "或完全退出浏览器后再检测。也可以改用扫码登录。"
)
RECOMMENDED_ACTION = {
    "action": "extension",
    "fallback": "qr",
    "message": (
        "推荐安装「Cortech Chat 小红书助手」一键同步。"
        "检测只列出候选（浏览器、配置、昵称、头像、站点），不含 Cookie，也不会自动导入。"
        "请在「账号管理」勾选要导入的登录态并确认。未勾选的不会参与采集。"
        "若暂不安装扩展，请改用扫码登录。"
    ),
}


# ---------------- 浏览器 profile 发现 ----------------

def _user_data_dirs() -> List[Tuple[str, Path]]:
    """各 OS 的 Chromium 系浏览器用户数据目录（仅返回存在的）"""
    home = Path.home()
    system = platform.system()
    cands: List[Tuple[str, Path]] = []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        cands = [
            ("Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ]
    elif system == "Darwin":
        cands = [
            ("Chrome", home / "Library" / "Application Support" / "Google" / "Chrome"),
            ("Edge", home / "Library" / "Application Support" / "Microsoft Edge"),
        ]
    else:  # Linux
        cands = [
            ("Chrome", home / ".config" / "google-chrome"),
            ("Chromium", home / ".config" / "chromium"),
            ("Edge", home / ".config" / "microsoft-edge"),
        ]
    return [(name, p) for name, p in cands if p.is_dir()]


def _iter_profiles(user_data_dir: Path) -> List[str]:
    """用户数据目录下的 profile 子目录（Default / Profile N），按 Cookies 文件存在过滤"""
    out = []
    try:
        entries = sorted(p for p in user_data_dir.iterdir() if p.is_dir())
    except OSError:
        return out
    for p in entries:
        if (p / "Network" / "Cookies").exists() or (p / "Cookies").exists():
            out.append(p.name)
    return out


def _profile_key(browser: str, profile: str) -> str:
    return f"{browser}||{profile}"


# ---------------- Cookie 解密 ----------------

def _dpapi_unprotect(enc: bytes) -> bytes:
    """Windows DPAPI 解密：优先 pywin32，无则用 ctypes 直调 crypt32.dll（零依赖）"""
    try:
        import win32crypt  # type: ignore
        return win32crypt.CryptUnprotectData(enc, None, None, None, 0)[1]
    except ImportError:
        pass
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.WinDLL("crypt32.dll")
    kernel32 = ctypes.WinDLL("kernel32.dll")
    buf = ctypes.create_string_buffer(enc)
    blob_in = DATA_BLOB(len(enc), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise BrowserImportError("浏览器密钥解密失败（DPAPI）")
    try:
        return bytes(bytearray(blob_out.pbData[:blob_out.cbData]))
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _get_aes_key(user_data_dir: Path) -> bytes:
    """从 Local State 取 os_crypt 密钥。

    Windows：密钥本身被 DPAPI 保护，需 pywin32 解开；
    Linux：base64 解码去 DPAPI 前缀后直接可用；
    macOS：钥匙串读取暂不支持。
    """
    ls_path = user_data_dir / "Local State"
    try:
        data = json.loads(ls_path.read_text(encoding="utf-8"))
        enc_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
    except Exception as e:  # noqa: BLE001
        raise BrowserImportError("读取浏览器密钥失败，浏览器可能未安装或数据损坏") from e
    if enc_key.startswith(b"DPAPI"):
        enc_key = enc_key[5:]
    system = platform.system()
    if system == "Windows":
        try:
            return _dpapi_unprotect(enc_key)
        except BrowserImportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BrowserImportError("浏览器密钥解密失败（DPAPI）") from e
    if system == "Darwin":
        raise BrowserImportError("macOS 钥匙串读取暂不支持，请用扫码登录")
    return enc_key


def _is_lock_error(exc: BaseException) -> bool:
    win = getattr(exc, "winerror", None)
    if win in (32, 33):
        return True
    if isinstance(exc, PermissionError):
        return True
    text = str(exc).lower()
    return (
        "being used by another process" in text
        or "resource busy" in text
        or "text file busy" in text
    )


def _cdp_allowed() -> bool:
    flag = (os.environ.get("CORTECH_BROWSER_CDP") or "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _decrypt_value(blob: bytes, key: bytes) -> str:
    """解密单条 Cookie 值。

    v10/v11：Windows=AES-256-GCM；Linux/macOS=AES-128-CBC（IV 为 16 个空格）。
    v20：App-Bound Encryption。不把密文当明文，也不做提权解密。
    无前缀：老版本明文存储，直接返回。
    """
    if blob.startswith(b"v20"):
        raise CookieProtectError("v20", V20_MESSAGE)
    if blob.startswith(b"v10") or blob.startswith(b"v11"):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        if platform.system() == "Windows":
            nonce, ct_tag = blob[3:15], blob[15:]
            ct, tag = ct_tag[:-16], ct_tag[-16:]
            d = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            return (d.update(ct) + d.finalize()).decode("utf-8", "ignore")
        ct = blob[3:]
        d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        pt = d.update(ct) + d.finalize()
        pad = pt[-1]
        if 1 <= pad <= 16:
            pt = pt[:-pad]
        return pt.decode("utf-8", "ignore")
    return blob.decode("utf-8", "ignore")


def _copy_sqlite(db_path: Path) -> str:
    fd, tmp = tempfile.mkstemp(prefix="cortech_ck_")
    os.close(fd)
    try:
        shutil.copy2(db_path, tmp)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if _is_lock_error(exc):
            raise CookieProtectError("locked", LOCK_MESSAGE) from exc
        raise
    return tmp


def _cookie_db_path(user_data_dir: Path, profile: str) -> Optional[Path]:
    prof_dir = user_data_dir / profile
    db_path = prof_dir / "Network" / "Cookies"
    if not db_path.exists():
        db_path = prof_dir / "Cookies"
    if not db_path.exists():
        return None
    return db_path


def _query_cookie_rows(db_path: Path) -> List[tuple]:
    tmp = _copy_sqlite(db_path)
    try:
        conn = sqlite3.connect(tmp)
        try:
            return conn.execute(
                "SELECT host_key, name, encrypted_value, value, path, expires_utc,"
                " is_secure, is_httponly, samesite FROM cookies"
                " WHERE host_key LIKE '%xiaohongshu.com'"
                " OR host_key LIKE '%xiaohongshu.cn'"
                " OR host_key LIKE '%xiaohongshu.hk'"
                " OR host_key LIKE '%rednote.com'"
            ).fetchall()
        finally:
            conn.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _decrypt_rows(raw_rows: List[tuple], key: bytes) -> Tuple[List[Dict[str, Any]], bool]:
    out: List[Dict[str, Any]] = []
    saw_v20 = False
    for host_key, name, enc, val, path, exp, sec, httponly, samesite in raw_rows:
        blob = bytes(enc) if enc else b""
        if blob.startswith(b"v20"):
            saw_v20 = True
            continue
        try:
            value = _decrypt_value(blob, key) if blob else (val or "")
        except CookieProtectError:
            raise
        except Exception:  # noqa: BLE001
            continue
        if not value:
            continue
        out.append({
            "host_key": host_key, "name": name, "value": value,
            "path": path or "/", "expires_utc": exp or 0,
            "is_secure": bool(sec), "is_httponly": bool(httponly),
            "samesite": samesite if samesite is not None else -1,
        })
    return out, saw_v20


def _xhs_domain(domain: str) -> bool:
    text = (domain or "").lower().lstrip(".")
    return any(
        text == marker or text.endswith("." + marker)
        for marker in ("xiaohongshu.com", "xiaohongshu.cn", "xiaohongshu.hk")
    )


def filter_cdp_cookies(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只留小红书 .com / .cn / .hk。调用方不得把 value 写入日志。"""
    kept = []
    for row in cookies or []:
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain") or "")
        if not _xhs_domain(domain):
            continue
        if not row.get("name") or not row.get("value"):
            continue
        kept.append(row)
    return kept


def _rows_from_cdp(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    same = {"Strict": 2, "Lax": 1, "None": 0}
    out = []
    for row in filter_cdp_cookies(cookies):
        expires = row.get("expires") or 0
        try:
            expires_f = float(expires)
        except (TypeError, ValueError):
            expires_f = 0
        expires_utc = int((expires_f + _CHROME_EPOCH_DIFF) * 1_000_000) if expires_f > 0 else 0
        out.append({
            "host_key": str(row.get("domain") or ""),
            "name": row.get("name"),
            "value": row.get("value"),
            "path": row.get("path") or "/",
            "expires_utc": expires_utc,
            "is_secure": bool(row.get("secure")),
            "is_httponly": bool(row.get("httpOnly")),
            "samesite": same.get(str(row.get("sameSite") or ""), -1),
        })
    return out


def _browser_executable(browser: str) -> str:
    env = (os.environ.get("CORTECH_CHROMIUM_PATH") or "").strip()
    if env and Path(env).is_file():
        return env
    name = (browser or "").lower()
    system = platform.system()
    candidates: List[Path] = []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        pf = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        pf86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        if "edge" in name:
            candidates = [
                pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        else:
            candidates = [
                pf / "Google" / "Chrome" / "Application" / "chrome.exe",
                pf86 / "Google" / "Chrome" / "Application" / "chrome.exe",
                local / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
    else:
        import shutil as _sh
        for bin_name in (("microsoft-edge", "msedge") if "edge" in name else ("google-chrome", "chromium", "chrome")):
            found = _sh.which(bin_name)
            if found:
                return found
    for path in candidates:
        if path.is_file():
            return str(path)
    return ""


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_profile_for_cdp(user_data_dir: Path, profile: str) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="cortech_cdp_"))
    local_state = user_data_dir / "Local State"
    if local_state.is_file():
        shutil.copy2(local_state, dest / "Local State")
    db_path = _cookie_db_path(user_data_dir, profile)
    if db_path is None:
        return dest
    rel = db_path.relative_to(user_data_dir)
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, target)
    return dest


async def _cdp_get_all_cookies(port: int) -> List[Dict[str, Any]]:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            session = await context.new_cdp_session(page)
            data = await session.send("Network.getAllCookies")
            cookies = data.get("cookies") if isinstance(data, dict) else None
            return list(cookies or [])
        finally:
            await browser.close()


def read_cookies_via_cdp(browser: str, user_data_dir: Path, profile: str) -> List[Dict[str, Any]]:
    """用浏览器自己的调试端口读取 Cookie。只在复制出来的 profile 上启动。

    不记录 Cookie 值。浏览器打不开时抛 BrowserImportError。
    """
    if not _cdp_allowed():
        raise BrowserImportError("已关闭调试端口读取。请改用扩展确认导入，或扫码登录。")
    exe = _browser_executable(browser)
    if not exe:
        raise BrowserImportError("没有找到浏览器程序，无法用调试端口读取。请改用扩展或扫码登录。")
    dest = _copy_profile_for_cdp(user_data_dir, profile)
    port = _free_port()
    import subprocess
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={dest}",
         f"--profile-directory={profile}", "--headless=new", "--no-first-run",
         "--no-default-browser-check", "--disable-gpu", "--no-sandbox"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        ready = False
        import urllib.request
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        if not ready:
            raise BrowserImportError("调试端口没有起来。请改用扩展确认导入，或扫码登录。")
        cookies = asyncio.run(_cdp_get_all_cookies(port))
        rows = _rows_from_cdp(cookies)
        logger.info("CDP 读取完成 browser=%s profile=%s count=%s", browser, profile, len(rows))
        return rows
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(dest, ignore_errors=True)


def _read_profile_cookie_rows(user_data_dir: Path, profile: str, *,
                             browser: str = "", allow_cdp: bool = True) -> List[Dict[str, Any]]:
    """读取某 profile 的小红书 Cookie（内存中解密，不落盘、不写日志）。

    v20 或文件锁会抛 CookieProtectError。v20 且允许时，用复制的 profile
    启动浏览器调试端口，再取 Network.getAllCookies。
    """
    db_path = _cookie_db_path(user_data_dir, profile)
    if db_path is None:
        return []
    raw = _query_cookie_rows(db_path)
    if not raw:
        return []
    key = _get_aes_key(user_data_dir)
    rows, saw_v20 = _decrypt_rows(raw, key)
    if saw_v20 and not _has_login(_cookie_dict(rows)):
        if allow_cdp and _cdp_allowed():
            try:
                cdp_rows = read_cookies_via_cdp(browser or "Chrome", user_data_dir, profile)
            except CookieProtectError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("CDP 读取失败 browser=%s profile=%s（不含 Cookie）", browser, profile)
                raise CookieProtectError("v20", V20_MESSAGE) from exc
            if _has_login(_cookie_dict(cdp_rows)):
                return cdp_rows
        raise CookieProtectError("v20", V20_MESSAGE)
    return rows


# ---------------- storage_state 构建（playwright 兼容） ----------------

_SAMESITE_MAP = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}
_CHROME_EPOCH_DIFF = 11644473600  # 1601-01-01 到 1970-01-01 的秒数


def _to_storage_state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Chrome Cookie 行 → playwright storage_state JSON（与扫码登录同一格式）"""
    cookies = []
    for r in rows:
        exp = -1.0
        if r["expires_utc"]:
            exp = r["expires_utc"] / 1_000_000 - _CHROME_EPOCH_DIFF
        cookies.append({
            "name": r["name"],
            "value": r["value"],
            "domain": r["host_key"],
            "path": r["path"],
            "expires": exp,
            "httpOnly": r["is_httponly"],
            "secure": r["is_secure"],
            "sameSite": _SAMESITE_MAP.get(r["samesite"], "Lax"),
        })
    return {"cookies": cookies, "origins": []}


# ---------------- 账号信息识别（只读 selfinfo） ----------------

def _cookie_dict(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Cookie 行 → name/value。同名时 .com 优先于 .cn / .hk / rednote。"""
    from .xhs_hosts import merge_cookie_rows
    merged = merge_cookie_rows(rows)
    return {r["name"]: r["value"] for r in merged}

async def _fetch_self_info(cookie_dict: Dict[str, str]) -> Dict[str, str]:
    """用 Cookie 调一次 selfinfo 拿昵称/头像/uid（只读，不做任何写操作）"""
    from .cortech_crawler import XHSClient, _BASE_HEADERS, convert_cookies
    cookie_str, _ = convert_cookies(
        [{"name": k, "value": v} for k, v in cookie_dict.items()])
    headers = dict(_BASE_HEADERS)
    headers["Cookie"] = cookie_str
    client = XHSClient(headers=headers, cookie_dict=cookie_dict)
    try:
        data = await client._request("GET", "/api/sns/web/v1/user/selfinfo", params={})
    finally:
        await client.close()
    return {
        "nickname": str(data.get("nickname") or ""),
        "avatar_url": str(data.get("images") or data.get("imageb") or ""),
        "uid": str(data.get("red_id") or data.get("userid") or ""),
    }


def _has_login(cookie_dict: Dict[str, str]) -> bool:
    """登录态核心：a1 + web_session（与采集器校验口径一致）"""
    return bool(cookie_dict.get("web_session") and cookie_dict.get("a1"))


# ---------------- 对外接口 ----------------

def _region_of(rows: List[Dict[str, Any]]) -> str:
    domains = " ".join(str(r.get("host_key") or "") for r in rows).lower()
    if "xiaohongshu.com" in domains:
        return "www.xiaohongshu.com"
    if "xiaohongshu.cn" in domains:
        return "www.xiaohongshu.cn"
    if "xiaohongshu.hk" in domains:
        return "www.xiaohongshu.hk"
    return ""


def _public_candidate(browser: str, profile: str, info: Dict[str, str],
                      region: str) -> Dict[str, str]:
    """检测结果只含这些字段。没有 Cookie，也不会写成账号。"""
    return {
        "key": _profile_key(browser, profile),
        "source": "browser",
        "browser": browser,
        "profile": profile,
        "nickname": info.get("nickname") or f"小红书账号（{browser}·{profile}）",
        "avatar_url": info.get("avatar_url") or "",
        "region": region,
        "uid": info.get("uid") or "",
    }


def scan_browser_logins(roots: Optional[List[Tuple[str, Path]]] = None,
                        fetch_info=None) -> Dict[str, Any]:
    """用户主动触发：只列出候选，不导入、不参与采集。

    返回 items / reasons / recommended。items 不含 Cookie 值。
    """
    found: List[Dict[str, str]] = []
    reasons: List[Dict[str, str]] = []
    if roots is None and (os.environ.get("CORTECH_SKIP_BROWSER_SCAN") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        roots = []
    pairs = roots if roots is not None else _user_data_dirs()
    for browser, udd in pairs:
        for profile in _iter_profiles(udd):
            try:
                rows = _read_profile_cookie_rows(udd, profile, browser=browser)
            except CookieProtectError as exc:
                reasons.append({
                    "code": exc.code,
                    "browser": browser,
                    "profile": profile,
                    "message": str(exc),
                })
                continue
            except BrowserImportError as exc:
                reasons.append({
                    "code": "unreadable",
                    "browser": browser,
                    "profile": profile,
                    "message": str(exc),
                })
                continue
            except Exception:  # noqa: BLE001
                logger.exception("读取浏览器登录态失败 browser=%s profile=%s", browser, profile)
                continue
            cookie_dict = _cookie_dict(rows)
            region = _region_of(rows)
            if not _has_login(cookie_dict):
                cookie_dict.clear()
                continue
            try:
                if fetch_info is not None:
                    info = fetch_info(cookie_dict)
                else:
                    info = asyncio.run(_fetch_self_info(cookie_dict))
            except Exception:  # noqa: BLE001
                info = {}
            finally:
                cookie_dict.clear()
            if not info:
                continue
            found.append(_public_candidate(browser, profile, info, region))
    return {
        "items": found,
        "reasons": reasons,
        "recommended": dict(RECOMMENDED_ACTION),
        "auto_imported": False,
    }


def _locate_profile(key: str) -> Tuple[str, Path, str]:
    """按 key 重新定位浏览器 profile（导入时重新读取，不缓存 Cookie）"""
    try:
        browser, profile = key.split("||", 1)
    except ValueError as e:
        raise BrowserImportError("账号标识无效，请重新检测") from e
    for b, udd in _user_data_dirs():
        if b == browser and profile in _iter_profiles(udd):
            return browser, udd, profile
    raise BrowserImportError("未找到该浏览器登录态，可能已被清除，请重新检测")


def _state_has_login(state: Dict[str, Any]) -> bool:
    names = set()
    for c in state.get("cookies") or []:
        if not isinstance(c, dict) or not c.get("value"):
            continue
        domain = str(c.get("domain") or "")
        if domain and "xiaohongshu" not in domain and "rednote" not in domain:
            continue
        names.add(c.get("name"))
    return "a1" in names and "web_session" in names


def _find_storage_state(path: Path) -> Optional[Path]:
    if path.is_file() and path.name == "storage_state.json":
        return path
    for cand in (path / "storage_state.json", path / "Default" / "storage_state.json"):
        if cand.is_file():
            return cand
    return None


def _rows_from_configured_dir(path: Path) -> List[Dict[str, Any]]:
    """只读调用者指定的那一个目录，不扫描其他浏览器。"""
    if (path / "Local State").is_file():
        profiles = _iter_profiles(path)
        ordered = (["Default"] if "Default" in profiles else []) + [
            p for p in profiles if p != "Default"]
        for profile in ordered:
            rows = _read_profile_cookie_rows(path, profile)
            if _has_login(_cookie_dict(rows)):
                return rows
        return []
    if (path / "Cookies").exists() or (path / "Network" / "Cookies").exists():
        return _read_profile_cookie_rows(path.parent, path.name)
    return []


def load_profile_storage_state(path: Path) -> Dict[str, Any]:
    """读取用户指定目录里已经存在的小红书登录态。

    没有 a1 / web_session 就拒绝。不打开登录页，不代为登录。
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise BrowserImportError("指定的 profile 目录不存在。请先在自己的浏览器登录小红书。")
    state_file = _find_storage_state(path)
    if state_file is not None:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise BrowserImportError(
                "storage_state.json 无法读取。请重新导出你已经登录的会话。"
            ) from e
        if not isinstance(state, dict) or not _state_has_login(state):
            raise BrowserImportError(
                "该会话里没有完整的本人登录态（缺少 a1 或 web_session）。"
                "请重新登录后再指定目录。服务端不会代为登录。"
            )
        return state
    try:
        rows = _rows_from_configured_dir(path)
    except BrowserImportError:
        raise
    except Exception as e:  # noqa: BLE001
        raise BrowserImportError(
            "无法读取指定目录里的登录态。请改用插件确认发送，或放入 storage_state.json。"
        ) from e
    if not _has_login(_cookie_dict(rows)):
        raise BrowserImportError(
            "指定目录里没有你已经登录的小红书会话。"
            "请先在自己的浏览器登录，再把目录配到 CORTECH_BROWSER_USE_PROFILE。"
            "服务端不会代为登录。"
        )
    return _to_storage_state(rows)


def import_login(key: str, *, confirmed: bool = False) -> Dict[str, Any]:
    """用户在账号管理勾选并确认后才导入。未确认不会建账号，也不会参与采集。

    返回新建账号摘要（不含任何 Cookie）。
    """
    if not confirmed:
        raise BrowserImportError(
            "请在「账号管理」勾选这个登录态并确认后再导入。"
            "检测结果不会自动导入，也不会参与采集。"
        )
    if (key or "").startswith("detected:"):
        try:
            detected_id = int(key.split(":", 1)[1])
        except ValueError as exc:
            raise BrowserImportError("候选标识无效，请重新检测") from exc
        return confirm_detected(detected_id, confirmed=True)

    from . import db
    from .account_crypto import encrypt_login_data

    browser, udd, profile = _locate_profile(key)
    rows = _read_profile_cookie_rows(udd, profile, browser=browser)
    cookie_dict = _cookie_dict(rows)
    if not _has_login(cookie_dict):
        raise BrowserImportError("该浏览器登录态已失效，请在浏览器中重新登录小红书后重试")

    state_str = json.dumps(_to_storage_state(rows), ensure_ascii=False)

    # 昵称 best-effort（失败则用兜底名，不阻塞导入）
    nickname, avatar_url, uid = "", "", ""
    try:
        info = asyncio.run(_fetch_self_info(cookie_dict))
        nickname, avatar_url, uid = info["nickname"], info["avatar_url"], info["uid"]
    except Exception:  # noqa: BLE001
        pass
    if not nickname:
        nickname = f"浏览器导入（{browser}·{profile}）"

    account_id = db.create_account()
    db.update_account(account_id, {
        "nickname": nickname,
        "xhs_user_id": uid,
        "avatar_url": avatar_url,
        "status": "valid",
        "enabled": 1,
        "login_data_enc": encrypt_login_data(state_str),
        "last_check_at": int(time.time()),
        "check_note": f"从本地浏览器导入（{browser}·{profile}）",
    })
    from .account_guard import arm_fresh_login, fingerprint_for_browser
    arm_fresh_login(account_id, fingerprint_for_browser(browser))
    return {"id": account_id, "nickname": nickname, "status": "valid", "enabled": 1}


def _state_from_cookie_dict(cookies: Dict[str, str]) -> Dict[str, Any]:
    return {
        "cookies": [
            {
                "name": name,
                "value": value,
                "domain": ".xiaohongshu.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
            for name, value in cookies.items()
            if value
        ],
        "origins": [],
    }


def stage_detected_login(*, source: str, browser: str, profile: str,
                         nickname: str = "", avatar_url: str = "",
                         region: str = "", cookies: Optional[Dict[str, str]] = None,
                         storage_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把检测结果放进候选。不创建账号，轮换也看不到。"""
    from . import db
    from .account_crypto import encrypt_login_data

    payload = ""
    if cookies:
        payload = encrypt_login_data(json.dumps(
            _state_from_cookie_dict(cookies), ensure_ascii=False))
        cookies.clear()
    elif storage_state:
        payload = encrypt_login_data(json.dumps(storage_state, ensure_ascii=False))
    detected_id = db.add_detected_login(
        source, browser, profile, nickname, avatar_url, region, payload)
    return {
        "ok": True,
        "staged": True,
        "auto_imported": False,
        "detected_id": detected_id,
        "key": f"detected:{detected_id}",
        "nickname": nickname,
        "browser": browser,
        "profile": profile,
        "region": region,
        "message": (
            "已列入候选。请在「账号管理」勾选并确认后才会导入。"
            "未选择的登录态不会参与采集。"
        ),
    }


def public_staged() -> List[Dict[str, Any]]:
    from . import db
    out = []
    for row in db.list_detected_logins():
        out.append({
            "key": row.get("candidate_key") or f"detected:{row['id']}",
            "detected_id": row["id"],
            "source": row.get("source") or "detected",
            "browser": row.get("browser") or "",
            "profile": row.get("profile") or "",
            "nickname": row.get("nickname") or "小红书账号",
            "avatar_url": row.get("avatar_url") or "",
            "region": row.get("region") or "",
        })
    return out


def confirm_detected(detected_id: int, *, confirmed: bool = False) -> Dict[str, Any]:
    """账号管理里的第二次确认。未确认不会进入账号表。"""
    if not confirmed:
        raise BrowserImportError(
            "请在「账号管理」勾选这个登录态并确认后再导入。"
            "未选择的检测结果不会导入，也不会参与采集。"
        )
    from . import db
    from .account_crypto import decrypt_login_data
    from .account_guard import arm_fresh_login, default_fingerprint

    row = db.get_detected_login(detected_id)
    if not row or not row.get("payload_enc"):
        raise BrowserImportError("没有可导入的登录态。请在插件里确认发送，或改用扫码登录。")
    try:
        state = json.loads(decrypt_login_data(row["payload_enc"]))
    except Exception as exc:  # noqa: BLE001
        raise BrowserImportError("候选登录态无法读取。请重新检测后再确认。") from exc
    if not isinstance(state, dict) or not _state_has_login(state):
        raise BrowserImportError("该候选缺少完整登录态。请重新检测后再确认。")
    nickname = (row.get("nickname") or "").strip() or f"浏览器导入（{row.get('browser') or '候选'}）"
    account_id = db.create_account()
    db.update_account(account_id, {
        "nickname": nickname,
        "avatar_url": row.get("avatar_url") or "",
        "status": "valid",
        "enabled": 1,
        "login_data_enc": row["payload_enc"],
        "last_check_at": int(time.time()),
        "check_note": "已在账号管理确认导入",
    })
    arm_fresh_login(account_id, default_fingerprint())
    db.delete_detected_login(detected_id)
    return {
        "id": account_id,
        "nickname": nickname,
        "status": "valid",
        "enabled": 1,
        "auto_imported": False,
    }


def discard_detected(detected_id: int) -> bool:
    from . import db
    return db.delete_detected_login(detected_id)
