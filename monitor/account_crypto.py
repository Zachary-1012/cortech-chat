# -*- coding: utf-8 -*-
"""小红书账号登录态加密存储。

- 登录态（playwright storage state JSON）用 Fernet 对称加密后存 SQLite。
- 密钥在 data/.fernet_key（600 权限，gitignore），不上传任何外部服务。
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import DATA_DIR

KEY_PATH = DATA_DIR / ".fernet_key"


def _get_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    os.chmod(KEY_PATH, 0o600)
    return key


def encrypt_login_data(plain: str) -> str:
    """加密登录态 JSON → base64 字符串入库"""
    from cryptography.fernet import Fernet
    return Fernet(_get_key()).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_login_data(token: str) -> str:
    """解密入库字符串 → 登录态 JSON"""
    from cryptography.fernet import Fernet
    return Fernet(_get_key()).decrypt(token.encode("ascii")).decode("utf-8")
