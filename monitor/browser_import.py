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
import os
import platform
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class BrowserImportError(Exception):
    """面向用户的可展示错误（不含敏感信息）"""


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


def _decrypt_value(blob: bytes, key: bytes) -> str:
    """解密单条 Cookie 值。

    v10/v11：Windows=AES-256-GCM；Linux/macOS=AES-128-CBC（IV 为 16 个空格）。
    无前缀：老版本明文存储，直接返回。
    """
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


def _read_profile_cookie_rows(user_data_dir: Path,
                             profile: str) -> List[Dict[str, Any]]:
    """读取某 profile 的小红书 Cookie 全字段（内存中解密，不落盘）。

    返回 [{host_key, name, value(已解密), path, expires_utc, is_secure,
           is_httponly, samesite}]，value 为空的条目已过滤。
    """
    prof_dir = user_data_dir / profile
    db_path = prof_dir / "Network" / "Cookies"
    if not db_path.exists():
        db_path = prof_dir / "Cookies"
    if not db_path.exists():
        return []
    # Chrome 运行时锁定源文件：复制到临时文件再读
    fd, tmp = tempfile.mkstemp(prefix="cortech_ck_")
    os.close(fd)
    try:
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(tmp)
        try:
            rows = conn.execute(
                "SELECT host_key, name, encrypted_value, value, path, expires_utc,"
                " is_secure, is_httponly, samesite FROM cookies"
                " WHERE host_key LIKE '%xiaohongshu.com'"
                " OR host_key LIKE '%rednote.com'"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        key = _get_aes_key(user_data_dir)
        out = []
        for host_key, name, enc, val, path, exp, sec, httponly, samesite in rows:
            try:
                v = _decrypt_value(bytes(enc), key) if enc else (val or "")
            except Exception:  # noqa: BLE001
                continue  # 单条解密失败不影响其他
            if not v:
                continue
            out.append({
                "host_key": host_key, "name": name, "value": v,
                "path": path or "/", "expires_utc": exp or 0,
                "is_secure": bool(sec), "is_httponly": bool(httponly),
                "samesite": samesite if samesite is not None else -1,
            })
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
    """Cookie 行 → name/value 字典；同名时优先 xiaohongshu.com（与采集器口径一致）"""
    ordered = sorted(rows, key=lambda r: "xiaohongshu.com" in r["host_key"])
    return {r["name"]: r["value"] for r in ordered}

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

def scan_browser_logins() -> List[Dict[str, str]]:
    """用户主动触发：扫描本地浏览器的小红书登录态。

    只返回账号基本信息（昵称/头像/uid），绝不返回 Cookie 值。
    每个 profile 只调一次只读 selfinfo；Cookie 全程只存内存。
    """
    found: List[Dict[str, str]] = []
    for browser, udd in _user_data_dirs():
        for profile in _iter_profiles(udd):
            try:
                rows = _read_profile_cookie_rows(udd, profile)
            except BrowserImportError:
                raise
            except Exception:  # noqa: BLE001
                continue
            cookie_dict = _cookie_dict(rows)
            if not _has_login(cookie_dict):
                continue
            try:
                info = asyncio.run(_fetch_self_info(cookie_dict))
            except Exception:  # noqa: BLE001
                continue  # 登录失效或网络问题：跳过，不展示
            found.append({
                "key": _profile_key(browser, profile),
                "browser": browser,
                "profile": profile,
                "nickname": info["nickname"] or f"小红书账号（{browser}·{profile}）",
                "avatar_url": info["avatar_url"],
                "uid": info["uid"],
            })
    return found


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


def import_login(key: str) -> Dict[str, Any]:
    """用户二次确认后：重新读取所选浏览器登录态 → 转 storage_state → 加密入库。

    返回新建账号摘要（不含任何 Cookie）。
    """
    from . import db
    from .account_crypto import encrypt_login_data

    browser, udd, profile = _locate_profile(key)
    rows = _read_profile_cookie_rows(udd, profile)
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
        "login_data_enc": encrypt_login_data(state_str),
        "last_check_at": int(time.time()),
        "check_note": f"从本地浏览器导入（{browser}·{profile}）",
    })
    return {"id": account_id, "nickname": nickname, "status": "valid"}
