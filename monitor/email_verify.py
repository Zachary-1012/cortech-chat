"""邮箱验证码：注册时的人机/邮箱归属校验。

- 6 位数字码，有效期 10 分钟，一次性使用，最多 5 次试错
- 验证码哈希入库（不存明文）
- SMTP 未配置时走开发模式：控制台打印验证码

注：本地单机版默认不强制验证码，本模块保留供云端版使用。
"""
from __future__ import annotations

import hashlib
import logging
import random
import smtplib
import time
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional, Tuple

from . import db

log = logging.getLogger("cortech.email_verify")

CODE_TTL = 600          # 10 分钟
CODE_LEN = 6
MAX_ATTEMPTS = 5
# 发送频率：同一邮箱 60 秒内只能发一次
RESEND_INTERVAL = 60


def _smtp_conf() -> dict:
    import os
    return {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT", "465") or 465),
        "user": os.environ.get("SMTP_USER", ""),
        "pass": os.environ.get("SMTP_PASS", ""),
        "from": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
        "use_ssl": os.environ.get("SMTP_SSL", "1") != "0",
    }


def smtp_configured() -> bool:
    c = _smtp_conf()
    return bool(c["host"] and c["user"] and c["pass"])


def _code_hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.lower()}|{code}".encode()).hexdigest()


def _gen_code() -> str:
    return "".join(random.choice("0123456789") for _ in range(CODE_LEN))


def send_code(email: str) -> Tuple[bool, str]:
    """生成并发送验证码。返回 (ok, message)。开发模式下打印到控制台。"""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "邮箱格式不正确"

    now = int(time.time())
    with db._lock, db._connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM email_codes WHERE email=?", (email,)).fetchone()
        if row and now - row["created_at"] < RESEND_INTERVAL:
            wait = RESEND_INTERVAL - (now - row["created_at"])
            return False, f"发送过于频繁，请 {wait} 秒后再试"
        code = _gen_code()
        conn.execute(
            "REPLACE INTO email_codes (email, code_hash, created_at, attempts)"
            " VALUES (?,?,?,0)",
            (email, _code_hash(email, code), now))

    if smtp_configured():
        ok, msg = _send_smtp(email, code)
        if not ok:
            return False, msg
        return True, "验证码已发送，请查收邮件"
    # 开发模式：控制台打印
    log.warning("[开发模式] %s 的注册验证码：%s（10 分钟内有效）", email, code)
    print(f"[Cortech Chat] {email} 的注册验证码：{code}（10 分钟内有效，SMTP 未配置）",
          flush=True)
    return True, "验证码已生成（开发模式：请查看服务端控制台）"


def _send_smtp(email: str, code: str) -> Tuple[bool, str]:
    c = _smtp_conf()
    subject = "Cortech Chat 注册验证码"
    body = (f"您的注册验证码是：{code}\n\n"
            f"10 分钟内有效，请勿泄露给他人。\n"
            f"—— Cortech Chat")
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(("Cortech Chat", c["from"]))
        msg["To"] = email
        if c["use_ssl"]:
            server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=20)
        else:
            server = smtplib.SMTP(c["host"], c["port"], timeout=20)
            server.starttls()
        try:
            server.login(c["user"], c["pass"])
            server.sendmail(c["from"], [email], msg.as_string())
        finally:
            server.quit()
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        log.error("SMTP 发送失败：%s", e)
        return False, f"邮件发送失败：{e}"


def verify_code(email: str, code: str) -> Tuple[bool, str]:
    """校验验证码。成功返回 (True, 'ok')，失败返回 (False, 原因)。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not code:
        return False, "请输入验证码"
    now = int(time.time())
    with db._lock, db._connect() as conn:
        row = conn.execute(
            "SELECT code_hash, created_at, attempts FROM email_codes WHERE email=?",
            (email,)).fetchone()
        if not row:
            return False, "请先获取验证码"
        if now - row["created_at"] > CODE_TTL:
            conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
            return False, "验证码已过期，请重新获取"
        if row["attempts"] >= MAX_ATTEMPTS:
            conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
            return False, "试错次数过多，请重新获取"
        if _code_hash(email, code) != row["code_hash"]:
            conn.execute("UPDATE email_codes SET attempts=attempts+1 WHERE email=?",
                         (email,))
            return False, "验证码不正确"
        # 成功：一次性使用，删除
        conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
        return True, "ok"


def get_dev_code(email: str) -> Optional[str]:
    """测试辅助：仅开发模式可用，返回当前有效验证码的明文（禁止生产使用）。"""
    return None  # 占位：明文不入库，无法取回
