# -*- coding: utf-8 -*-
"""SQLite 数据层（短连接 + 写锁，线程安全）

多用户隔离：所有业务表带 user_id；当前用户经 ContextVar 传递，
由 app.py 的鉴权中间件设置（请求上下文），后台任务显式设置。
uid=None 表示系统上下文（不做过滤，仅服务端内部使用）。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

from .config import DB_PATH, PRESET_TOPICS, ensure_dirs

_lock = threading.Lock()

try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:  # Windows 包未装 bcrypt 时降级提示
    _bcrypt = None
    _HAS_BCRYPT = False

# ---------------- 当前用户上下文 ----------------
_current_uid: ContextVar[Optional[int]] = ContextVar("cortech_uid", default=None)


def set_current_user(uid: Optional[int]) -> None:
    """设置当前用户（鉴权中间件 / 后台任务调用）"""
    _current_uid.set(uid)


def current_user_id() -> Optional[int]:
    return _current_uid.get()


def _uw(where: List[str], params: List[Any], alias: str = "") -> None:
    """给 WHERE 条件追加 user_id 过滤（uid=None 的系统上下文不过滤）"""
    uid = _current_uid.get()
    if uid is not None:
        where.append(f"{alias}user_id=?")
        params.append(uid)


def apply_uid_filter(where: List[str], params: List[Any], alias: str = "") -> None:
    """公共版 _uw：供 fakecheck/alerts/competitors/events 等模块做数据隔离。"""
    _uw(where, params, alias)


def uid_sql(alias: str = "") -> Tuple[str, tuple]:
    """raw SQL 用的 user_id 过滤片段：返回 (sql, params)，拼在 WHERE 条件后。"""
    uid = _current_uid.get()
    if uid is None:
        return "", ()
    return f" AND {alias}user_id=?", (uid,)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS email_codes (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL,
    max_notes INTEGER NOT NULL DEFAULT 50,
    enable_comments INTEGER NOT NULL DEFAULT 1,
    cron TEXT NOT NULL DEFAULT '0 2 * * *',
    enabled INTEGER NOT NULL DEFAULT 0,
    engine TEXT NOT NULL DEFAULT 'cortech_browser',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xhs_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    nickname TEXT NOT NULL DEFAULT '',
    xhs_user_id TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    login_data_enc TEXT NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL,
    last_used_at INTEGER DEFAULT 0,
    last_check_at INTEGER DEFAULT 0,
    check_note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    topic_id INTEGER,  -- xhs 任务关联话题；web-news 资讯任务为 NULL，用 news_source_id
    task_type TEXT NOT NULL DEFAULT 'xhs',
    news_source_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    engine TEXT NOT NULL DEFAULT 'cortech_browser',
    engine_used TEXT DEFAULT '',
    started_at INTEGER,
    finished_at INTEGER,
    log_tail TEXT DEFAULT '',
    notes_count INTEGER DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS topic_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    topic_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'expanded',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE(topic_id, tag)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    post_id TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    desc TEXT DEFAULT '',
    author TEXT DEFAULT '',
    author_id TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    publish_time INTEGER,
    ip_location TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    liked_count INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    note_url TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    collected_at INTEGER,
    created_at INTEGER NOT NULL,
    post_status TEXT DEFAULT 'active',
    share_count_available INTEGER,
    field_meta TEXT DEFAULT '',
    UNIQUE(user_id, post_id)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    comment_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    content TEXT DEFAULT '',
    author TEXT DEFAULT '',
    create_time INTEGER,
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, comment_id)
);
CREATE TABLE IF NOT EXISTS authors (
    user_id INTEGER,
    author_id TEXT NOT NULL,
    nickname TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    liked_count INTEGER DEFAULT 0,
    notes_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, author_id)
);
CREATE TABLE IF NOT EXISTS news_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'search',
    keywords TEXT DEFAULT '',
    url TEXT DEFAULT '',
    cron TEXT NOT NULL DEFAULT '0 3 * * *',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS creations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    inputs_json TEXT NOT NULL DEFAULT '{}',
    content TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    parent_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'text',
    filename TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    scores_json TEXT NOT NULL DEFAULT '{}',
    weights_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT DEFAULT '',
    publish_time INTEGER,
    publish_time_raw TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    keyword TEXT DEFAULT '',
    collected_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, url)
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(publish_time);
CREATE INDEX IF NOT EXISTS idx_news_sentiment ON news(sentiment);
CREATE INDEX IF NOT EXISTS idx_news_source ON news(source);
CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes(topic_id);
CREATE INDEX IF NOT EXISTS idx_notes_sentiment ON notes(sentiment);
CREATE INDEX IF NOT EXISTS idx_notes_post_id ON notes(post_id);
CREATE INDEX IF NOT EXISTS idx_notes_publish ON notes(publish_time);
CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(post_id);
CREATE TABLE IF NOT EXISTS note_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    post_id TEXT NOT NULL,
    topic_id INTEGER,
    account_id INTEGER,
    task_id INTEGER,
    source TEXT NOT NULL DEFAULT 'account',
    collected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_note_collections_post ON note_collections(user_id, post_id);
"""

# 增量迁移：老库缺列时补列（表已存在时 CREATE TABLE 不生效）
_MIGRATIONS = {
    "topics": {"engine": "TEXT NOT NULL DEFAULT 'cortech_browser'",
               "user_id": "INTEGER"},
    "xhs_accounts": {
        "user_id": "INTEGER",
        "daily_quota": "INTEGER NOT NULL DEFAULT 0",
        "quota_used": "INTEGER NOT NULL DEFAULT 0",
        "quota_day": "TEXT NOT NULL DEFAULT ''",
        "skip_until": "INTEGER NOT NULL DEFAULT 0",
        "fail_streak": "INTEGER NOT NULL DEFAULT 0",
    },
    "auth_sessions": {"csrf_token": "TEXT NOT NULL DEFAULT ''"},
    "tasks": {"engine": "TEXT NOT NULL DEFAULT 'cortech_browser'",
              "engine_used": "TEXT DEFAULT ''",
              "task_type": "TEXT NOT NULL DEFAULT 'xhs'",
              "news_source_id": "INTEGER",
              "account_id": "INTEGER",
              "aggressive": "INTEGER NOT NULL DEFAULT 0",
              "progress": "INTEGER NOT NULL DEFAULT 0",
              "progress_detail": "TEXT DEFAULT ''",
              "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
              "deleted_count": "INTEGER NOT NULL DEFAULT 0",
              "updated_at": "INTEGER NOT NULL DEFAULT 0",
              "user_id": "INTEGER"},
    "topic_tags": {"user_id": "INTEGER"},
    "news_sources": {"user_id": "INTEGER"},
    "creations": {"user_id": "INTEGER"},
    "bids": {"user_id": "INTEGER"},
    "notes": {
        "author_id": "TEXT DEFAULT ''",
        "fans_count": "INTEGER DEFAULT 0",
        "ip_location": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "media_type": "TEXT DEFAULT ''",
        "collected_at": "INTEGER",
        "publish_time_raw": "TEXT DEFAULT ''",
        "post_status": "TEXT DEFAULT 'active'",
        "share_count_available": "INTEGER",
        "field_meta": "TEXT DEFAULT ''",
        "collected_account_id": "INTEGER",
    },
    "comments": {
        "reply_count": "INTEGER DEFAULT 0",
        "ip_location": "TEXT DEFAULT ''",
        "create_time_raw": "TEXT DEFAULT ''",
    },
}

# 需要重建以变更唯一约束的表：新 CREATE TABLE（必须含 user_id）
_REBUILD_TABLES = {
    "notes": """CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    post_id TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    desc TEXT DEFAULT '',
    author TEXT DEFAULT '',
    author_id TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    publish_time INTEGER,
    ip_location TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    media_type TEXT DEFAULT '',
    liked_count INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    note_url TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    collected_at INTEGER,
    created_at INTEGER NOT NULL,
    post_status TEXT DEFAULT 'active',
    share_count_available INTEGER,
    field_meta TEXT DEFAULT '',
    UNIQUE(user_id, post_id)
)""",
    "comments": """CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    comment_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    content TEXT DEFAULT '',
    author TEXT DEFAULT '',
    create_time INTEGER,
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, comment_id)
)""",
    "authors": """CREATE TABLE authors (
    user_id INTEGER,
    author_id TEXT NOT NULL,
    nickname TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    liked_count INTEGER DEFAULT 0,
    notes_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, author_id)
)""",
    "news": """CREATE TABLE news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT DEFAULT '',
    publish_time INTEGER,
    publish_time_raw TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    sentiment TEXT DEFAULT 'neutral',
    sentiment_score REAL DEFAULT 0.5,
    keyword TEXT DEFAULT '',
    collected_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, url)
)""",
}

_REBUILD_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_news_time ON news(publish_time)",
    "CREATE INDEX IF NOT EXISTS idx_news_sentiment ON news(sentiment)",
    "CREATE INDEX IF NOT EXISTS idx_news_source ON news(source)",
    "CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes(topic_id)",
    "CREATE INDEX IF NOT EXISTS idx_notes_sentiment ON notes(sentiment)",
    "CREATE INDEX IF NOT EXISTS idx_notes_post_id ON notes(post_id)",
    "CREATE INDEX IF NOT EXISTS idx_notes_publish ON notes(publish_time)",
    "CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(post_id)",
]


def _rebuild_with_userid(conn: sqlite3.Connection, table: str, create_sql: str) -> bool:
    """老表无 user_id 时重建（改唯一约束为 user_id 维度），保留数据。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "user_id" in cols:
        return False
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
    conn.executescript(create_sql)
    new_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    old_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table}__old)").fetchall()}
    common = [c for c in new_cols if c != "user_id" and c in old_cols]
    if common:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(common)})"
            f" SELECT {', '.join(common)} FROM {table}__old")
    conn.execute(f"DROP TABLE {table}__old")
    for idx_sql in _REBUILD_INDEXES:
        # 仅重建属于该表的索引
        if f" ON {table}(" in idx_sql:
            conn.execute(idx_sql)
    return True


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # 多用户：唯一约束改为 user_id 维度（无 user_id 列的老表才重建）
    for table, create_sql in _REBUILD_TABLES.items():
        try:
            _rebuild_with_userid(conn, table, create_sql)
        except Exception:
            pass
    # 重建会换掉表，列补丁再跑一遍，避免新列在重建后丢失
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # 存量迁移：tasks.topic_id 去掉 NOT NULL（资讯任务无话题，用 NULL）
    cols = {r["name"]: r["notnull"] for r in
            conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if cols and cols.get("topic_id"):
        conn.executescript("""
            CREATE TABLE tasks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER,
                task_type TEXT NOT NULL DEFAULT 'xhs',
                news_source_id INTEGER,
                status TEXT NOT NULL DEFAULT 'queued',
                engine TEXT NOT NULL DEFAULT 'cortech_browser',
                engine_used TEXT DEFAULT '',
                started_at INTEGER,
                finished_at INTEGER,
                log_tail TEXT DEFAULT '',
                notes_count INTEGER DEFAULT 0,
                error TEXT DEFAULT ''
            );
            INSERT INTO tasks_new (id, topic_id, task_type, news_source_id, status,
                engine, engine_used, started_at, finished_at, log_tail, notes_count, error)
            SELECT id, topic_id, task_type, news_source_id, status,
                engine, engine_used, started_at, finished_at, log_tail, notes_count, error
            FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
        """)
    # 产品重命名：存量引擎标识 tkh_* -> cortech_*（topics/tasks 表）
    for _old, _new in (("tkh_direct", "cortech_direct"),
                       ("tkh_browser", "cortech_browser"),
                       ("tkh_crawler", "cortech_crawler"),
                       ("tkh_spider", "cortech_spider"),
                       ("tkh_downloader", "cortech_downloader")):
        for _table in ("topics", "tasks"):
            try:
                conn.execute(f"UPDATE {_table} SET engine=? WHERE engine=?",
                             (_new, _old))
            except Exception:
                pass
        try:
            conn.execute("UPDATE tasks SET engine_used=? WHERE engine_used=?",
                         (_new, _old))
        except Exception:
            pass
    # 旧库若重建过 tasks，进度列可能被丢掉，这里再补一次
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for col, ddl in _MIGRATIONS["tasks"].items():
        if col not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _pre_migrate(conn: sqlite3.Connection) -> None:
    """SCHEMA 执行前的修复：旧列名改名，保证索引能正常创建。"""
    # 命名统一：notes.note_id -> post_id，comments.note_id -> post_id（历史坏迁移修复）
    for table in ("notes", "comments"):
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            cols = set()
        if "note_id" in cols and "post_id" not in cols:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN note_id TO post_id")
    # 索引改名：idx_notes_note_id -> idx_notes_post_id
    try:
        idxs = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        if "idx_notes_note_id" in idxs and "idx_notes_post_id" not in idxs:
            conn.execute("DROP INDEX idx_notes_note_id")
    except Exception:
        pass


def init_db() -> None:
    ensure_dirs()
    with _lock, _connect() as conn:
        _pre_migrate(conn)
        conn.executescript(SCHEMA)
        _migrate(conn)


def seed_topics() -> None:
    """首次启动预置两个话题（按名称去重）"""
    init_db()
    with _lock, _connect() as conn:
        for t in PRESET_TOPICS:
            row = conn.execute("SELECT id FROM topics WHERE name=?", (t["name"],)).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO topics (name, keywords, max_notes, enable_comments, cron, enabled, engine, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (t["name"], t["keywords"], t["max_notes"], t["enable_comments"],
                 t["cron"], 0, t.get("engine", "cortech_direct"), int(time.time())),
            )
    # 纯手动触发为默认：预置话题也不启用定时（用户可在话题页手动开启）


# ---------------- 用户账号体系 ----------------

SESSION_TTL = 30 * 86400  # 会话有效期 30 天


def hash_password(password: str) -> str:
    if not _HAS_BCRYPT:
        raise RuntimeError("bcrypt 未安装，请先安装 bcrypt")
    return _bcrypt.hashpw(password.encode("utf-8"),
                          _bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, pw_hash: str) -> bool:
    if not _HAS_BCRYPT:
        return False
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), pw_hash.encode("utf-8"))
    except Exception:
        return False


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (email or "").strip()))


def _assign_legacy(conn: sqlite3.Connection, uid: int) -> None:
    """历史数据（user_id 为 NULL）归属指定用户。"""
    for t in ("topics", "xhs_accounts", "tasks", "topic_tags", "notes",
              "comments", "authors", "news_sources", "creations", "bids", "news",
              "note_collections"):
        try:
            conn.execute(f"UPDATE {t} SET user_id=? WHERE user_id IS NULL", (uid,))
        except Exception:
            pass


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(email: str, password: str) -> int:
    """注册。首个注册用户自动继承历史数据。"""
    email = (email or "").strip().lower()
    if not _valid_email(email):
        raise ValueError("邮箱格式不正确")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    with _lock, _connect() as conn:
        if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            raise ValueError("该邮箱已注册")
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            (email, hash_password(password), int(time.time())))
        uid = cur.lastrowid
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            _assign_legacy(conn, uid)
        return uid


def verify_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    """登录校验，成功返回用户信息（不含密码哈希），失败返回 None。"""
    email = (email or "").strip().lower()
    with _connect() as conn:
        row = conn.execute("SELECT id, email, password_hash, created_at FROM users"
                           " WHERE email=?", (email,)).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}


def get_user(uid: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT id, email, created_at FROM users WHERE id=?",
                           (uid,)).fetchone()
        return dict(row) if row else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_auth_session(user_id: int) -> str:
    """创建会话，返回明文 token（只此一次）；库中只存哈希。"""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (user_id, token_hash, expires_at, created_at, last_seen)"
            " VALUES (?,?,?,?,?)",
            (user_id, _token_hash(token), now + SESSION_TTL, now, now))
    return token


def get_session_user(token: str) -> Optional[Dict[str, Any]]:
    """token 换用户；有效则刷新 last_seen（半衰期后续期），过期返回 None。"""
    if not token:
        return None
    th = _token_hash(token)
    now = int(time.time())
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, s.last_seen, u.email, u.created_at"
            " FROM auth_sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token_hash=?", (th,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (th,))
            return None
        # 滑动过期：剩余不足一半时续期
        if row["expires_at"] - now < SESSION_TTL // 2:
            conn.execute("UPDATE auth_sessions SET expires_at=?, last_seen=?"
                         " WHERE token_hash=?", (now + SESSION_TTL, now, th))
        else:
            conn.execute("UPDATE auth_sessions SET last_seen=? WHERE token_hash=?",
                         (now, th))
        return {"id": row["user_id"], "email": row["email"],
                "created_at": row["created_at"]}


def issue_csrf(token: str) -> Optional[str]:
    """为当前会话签发 CSRF。明文 token 不入库，只更新哈希对应的 csrf_token。"""
    if get_session_user(token) is None:
        return None
    csrf = secrets.token_urlsafe(32)
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE auth_sessions SET csrf_token=? WHERE token_hash=?",
            (csrf, _token_hash(token)),
        )
    return csrf


def csrf_matches(token: str, provided: str) -> bool:
    """校验调用方在已登录会话上附带的 CSRF。不匹配时不回显任何令牌。"""
    provided = provided or ""
    if not token or not provided:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT csrf_token FROM auth_sessions WHERE token_hash=?",
            (_token_hash(token),),
        ).fetchone()
    if not row:
        return False
    saved = row["csrf_token"] or ""
    if not saved or len(saved) != len(provided):
        return False
    return secrets.compare_digest(saved, provided)


def delete_auth_session(token: str) -> None:
    if not token:
        return
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                     (_token_hash(token),))


def delete_user_sessions(user_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))


def cleanup_expired_sessions() -> int:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM auth_sessions WHERE expires_at<?",
                           (int(time.time()),))
        return cur.rowcount


def _day_start(day: str) -> int:
    """YYYY-MM-DD -> 当天 00:00 时间戳"""
    import datetime as _dt
    return int(_dt.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=_dt.timezone(_dt.timedelta(hours=8))).timestamp())


def _day_end(day: str) -> int:
    return _day_start(day) + 86399


# ---------------- topics ----------------

def list_topics() -> List[Dict[str, Any]]:
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        return [dict(r) for r in
                conn.execute(f"SELECT * FROM topics {clause} ORDER BY id", params).fetchall()]


def get_topic(topic_id: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [topic_id]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM topics WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def create_topic(name: str, keywords: str, max_notes: int = 50,
                 enable_comments: int = 1, cron: str = "0 2 * * *",
                 enabled: int = 0, engine: str = "cortech_direct") -> int:
    """纯手动触发为默认：enabled=0（定时采集为可选项，用户手动开启）"""
    uid = _current_uid.get()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO topics (user_id, name, keywords, max_notes, enable_comments, cron, enabled, engine, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, name, keywords, max_notes, enable_comments, cron, enabled, engine, int(time.time())),
        )
        return cur.lastrowid


def update_topic(topic_id: int, fields: Dict[str, Any]) -> bool:
    allowed = {"name", "keywords", "max_notes", "enable_comments", "cron", "enabled", "engine"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    where, params = ["id=?"], [topic_id]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE topics SET {', '.join(f'{k}=?' for k in sets)} WHERE {' AND '.join(where)}",
            (*sets.values(), *params),
        )
        return cur.rowcount > 0


def save_topic_tags(topic_id: int, tags: List[str], kind: str = "expanded",
                    enabled: bool = True) -> int:
    """保存话题 tag（幂等，重复 tag 跳过）。返回新增数。"""
    now = int(time.time())
    uid = _current_uid.get()
    added = 0
    with _lock, _connect() as conn:
        for t in tags:
            t = (t or "").strip()
            if not t:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO topic_tags (user_id, topic_id, tag, kind, enabled, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (uid, topic_id, t, kind, 1 if enabled else 0, now),
            )
            added += cur.rowcount
    return added


def list_topic_tags(topic_id: int, kind: str = "",
                    enabled_only: bool = False) -> List[Dict[str, Any]]:
    """列出话题的扩展/discovered tag。"""
    where, params = ["topic_id=?"], [topic_id]
    _uw(where, params)
    if kind:
        where.append("kind=?")
        params.append(kind)
    if enabled_only:
        where.append("enabled=1")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM topic_tags WHERE {' AND '.join(where)} ORDER BY id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def delete_topic(topic_id: int) -> bool:
    where, params = ["id=?"], [topic_id]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM topics WHERE {' AND '.join(where)}", params)
        return cur.rowcount > 0


# ---------------- tasks ----------------

def create_task(topic_id: int, engine: str = "cortech_browser",
                task_type: str = "xhs",
                news_source_id: Optional[int] = None,
                account_id: Optional[int] = None,
                aggressive: int = 0) -> int:
    uid = _current_uid.get()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, topic_id, task_type, news_source_id, engine, status, started_at,"
            " account_id, aggressive)"
            " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (uid, topic_id, task_type, news_source_id, engine, int(time.time()),
             account_id, aggressive),
        )
        return cur.lastrowid


def update_task(task_id: int, **fields: Any) -> None:
    where, params = ["id=?"], [task_id]
    _uw(where, params, alias="")
    with _lock, _connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(f'{k}=?' for k in fields)} WHERE {' AND '.join(where)}",
            (*fields.values(), *params),
        )


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [task_id]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM tasks WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    where, params = [], []
    _uw(where, params, alias="t.")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT t.*, tp.name AS topic_name, ns.name AS news_source_name FROM tasks t"
            " LEFT JOIN topics tp ON tp.id=t.topic_id"
            " LEFT JOIN news_sources ns ON ns.id=t.news_source_id"
            f" {clause} ORDER BY t.id DESC LIMIT ?", (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def running_task_exists() -> bool:
    where, params = ["status IN ('queued','running')"], []
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT id FROM tasks WHERE {' AND '.join(where)} LIMIT 1", params
        ).fetchone()
        return row is not None


# ---------------- notes / comments ----------------

def upsert_note(note: Dict[str, Any]) -> bool:
    """插入帖子；(user_id, post_id) 已存在则跳过。返回 True 表示新增。"""
    uid = _current_uid.get()
    if uid is None:
        uid = note.get("user_id")  # 后台任务可直接在 dict 里带 user_id
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notes (user_id, post_id, topic_id, title, desc, author, author_id,"
            " fans_count, publish_time, publish_time_raw, ip_location, tags, media_type,"
            " liked_count, collected_count, comment_count, share_count, note_url, source_keyword,"
            " sentiment, sentiment_score, collected_at, created_at,"
            " post_status, share_count_available, field_meta, collected_account_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, note["post_id"], note["topic_id"], note.get("title", ""), note.get("desc", ""),
                note.get("author", ""), note.get("author_id", ""),
                note.get("fans_count", 0), note.get("publish_time"),
                note.get("publish_time_raw", ""),
                note.get("ip_location", ""), note.get("tags", ""), note.get("media_type", ""),
                note.get("liked_count", 0), note.get("collected_count", 0),
                note.get("comment_count", 0), note.get("share_count", 0),
                note.get("note_url", ""), note.get("source_keyword", ""),
                note.get("sentiment", "neutral"), note.get("sentiment_score", 0.5),
                note.get("collected_at", int(time.time())), int(time.time()),
                note.get("post_status") or "active",
                (1 if note.get("share_count_available") else 0),
                note.get("field_meta") if isinstance(note.get("field_meta"), str)
                else json.dumps(note.get("field_meta") or {}, ensure_ascii=False),
                note.get("collected_account_id"),
            ),
        )
        return cur.rowcount > 0


def upsert_comment(comment: Dict[str, Any]) -> bool:
    uid = _current_uid.get()
    if uid is None:
        uid = comment.get("user_id")
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO comments (user_id, comment_id, post_id, content, author, create_time,"
            " create_time_raw, like_count, reply_count, ip_location, sentiment, sentiment_score, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, comment["comment_id"], comment["post_id"], comment.get("content", ""),
                comment.get("author", ""), comment.get("create_time"),
                comment.get("create_time_raw", ""),
                comment.get("like_count", 0), comment.get("reply_count", 0),
                comment.get("ip_location", ""),
                comment.get("sentiment", "neutral"), comment.get("sentiment_score", 0.5),
                int(time.time()),
            ),
        )
        return cur.rowcount > 0


def upsert_author(author: Dict[str, Any]) -> None:
    """作者信息（有 author_id 才存；已存在则更新指标）"""
    aid = (author.get("author_id") or "").strip()
    if not aid:
        return
    uid = _current_uid.get()
    if uid is None:
        uid = author.get("user_id")
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO authors (user_id, author_id, nickname, fans_count, liked_count, notes_count,"
            " ip_location, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, author_id) DO UPDATE SET"
            " nickname=excluded.nickname, fans_count=excluded.fans_count,"
            " liked_count=excluded.liked_count, notes_count=excluded.notes_count,"
            " ip_location=excluded.ip_location, updated_at=excluded.updated_at",
            (uid, aid, author.get("nickname", ""), author.get("fans_count", 0),
             author.get("liked_count", 0), author.get("notes_count", 0),
             author.get("ip_location", ""), now),
        )


def list_posts(topic_id: Optional[int] = None, sentiment: Optional[str] = None,
               q: Optional[str] = None, page: int = 1, page_size: int = 20,
               date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    """date_from/date_to: YYYY-MM-DD，按发布时间过滤"""
    where, params = [], []
    _uw(where, params)
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    if sentiment:
        where.append("sentiment=?"); params.append(sentiment)
    if q:
        where.append("(title LIKE ? OR desc LIKE ? OR author LIKE ?)")
        params += [f"%{q}%"] * 3
    if date_from:
        where.append("publish_time>=?"); params.append(_day_start(date_from))
    if date_to:
        where.append("publish_time<=?"); params.append(_day_end(date_to))
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM notes {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM notes {clause} ORDER BY publish_time DESC NULLS LAST, id DESC"
            f" LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [_public_note(r) for r in rows]}


def list_comments(post_id: str, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    where, params = ["c.post_id=?"], [post_id]
    _uw(where, params, alias="c.")
    clause = f"WHERE {' AND '.join(where)}"
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM comments c {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT c.* FROM comments c {clause} ORDER BY like_count DESC, id"
            " LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [dict(r) for r in rows]}


def stats(topic_id: Optional[int] = None) -> Dict[str, Any]:
    uid = _current_uid.get()
    nw, np = [], []
    if topic_id:
        nw.append("topic_id=?"); np.append(topic_id)
    if uid is not None:
        nw.append("user_id=?"); np.append(uid)
    nwhere = f"WHERE {' AND '.join(nw)}" if nw else ""
    cw, cp = [], []
    if topic_id:
        cw.append("n.topic_id=?"); cp.append(topic_id)
    if uid is not None:
        cw.append("n.user_id=?"); cp.append(uid)
        cw.append("c.user_id=?"); cp.append(uid)
    cwhere = f"WHERE {' AND '.join(cw)}" if cw else ""
    with _connect() as conn:
        total_notes = conn.execute(
            f"SELECT COUNT(*) FROM notes {nwhere}", np).fetchone()[0]
        total_comments = conn.execute(
            "SELECT COUNT(*) FROM comments c JOIN notes n"
            " ON n.post_id=c.post_id AND IFNULL(n.user_id,-1)=IFNULL(c.user_id,-1)"
            f" {cwhere}", cp).fetchone()[0]
        dist = {}
        for label in ("negative", "neutral", "positive"):
            dist[label] = conn.execute(
                f"SELECT COUNT(*) FROM notes {nwhere}"
                + (" AND " if nwhere else " WHERE ") + "sentiment=?",
                (*np, label),
            ).fetchone()[0]
        active_clause = (" AND " if nwhere else " WHERE ") + (
            "IFNULL(post_status,'active')!='deleted'")
        active_notes = conn.execute(
            f"SELECT COUNT(*) FROM notes {nwhere}{active_clause}", np).fetchone()[0]
        deleted_notes = conn.execute(
            f"SELECT COUNT(*) FROM notes {nwhere}"
            + (" AND " if nwhere else " WHERE ") + "post_status='deleted'",
            np,
        ).fetchone()[0]
        share_not_on_web = conn.execute(
            f"SELECT COUNT(*) FROM notes {nwhere}{active_clause}"
            " AND share_count_available=0",
            np,
        ).fetchone()[0]
        neg_top = conn.execute(
            f"SELECT post_id, title, author, liked_count, comment_count, share_count,"
            f" share_count_available, note_url, source_keyword, publish_time, collected_at,"
            f" post_status FROM notes {nwhere}{active_clause}"
            + " AND sentiment='negative'"
            " ORDER BY (liked_count + comment_count * 3) DESC LIMIT 10",
            np,
        ).fetchall()
        return {
            "total_notes": total_notes,
            "active_notes": active_notes,
            "deleted_notes": deleted_notes,
            "share_not_on_web": share_not_on_web,
            "time_basis": {
                "publish_time": "发布时间",
                "collected_at": "采集时间",
            },
            "total_comments": total_comments,
            "sentiment_dist": dist,
            "negative_top10": [dict(r) for r in neg_top],
        }


# ---------------- news_sources ----------------

# 示例数据（太古汇是第一个用户），换品牌时在「全网资讯」页删除/修改即可
NEWS_SOURCE_PRESETS = [
    {"name": "新闻搜索：广州太古汇", "kind": "search",
     "keywords": "广州太古汇,太古汇十五周年", "url": "",
     "cron": "0 3 * * *"},
]


def seed_news_sources() -> None:
    init_db()
    with _lock, _connect() as conn:
        for s in NEWS_SOURCE_PRESETS:
            row = conn.execute("SELECT id FROM news_sources WHERE name=?",
                               (s["name"],)).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO news_sources (name, kind, keywords, url, cron, enabled, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (s["name"], s["kind"], s["keywords"], s["url"], s["cron"],
                 0, int(time.time())))
    # 纯手动触发为默认：资讯源也不启用定时


def list_news_sources() -> List[Dict[str, Any]]:
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        return [dict(r) for r in
                conn.execute(f"SELECT * FROM news_sources {clause} ORDER BY id", params).fetchall()]


def get_news_source(source_id: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [source_id]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM news_sources WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def create_news_source(name: str, kind: str, keywords: str = "",
                       url: str = "", cron: str = "0 3 * * *",
                       enabled: int = 1) -> int:
    if kind not in ("search", "rss"):
        raise ValueError("kind 必须为 search 或 rss")
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO news_sources (user_id, name, kind, keywords, url, cron, enabled, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_current_uid.get(), name, kind, keywords, url, cron, enabled, int(time.time())))
        return cur.lastrowid


def update_news_source(source_id: int, fields: Dict[str, Any]) -> bool:
    allowed = {"name", "kind", "keywords", "url", "cron", "enabled"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    where, wparams = ["id=?"], [source_id]
    _uw(where, wparams)
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE news_sources SET {', '.join(f'{k}=?' for k in sets)} WHERE {' AND '.join(where)}",
            (*sets.values(), *wparams))
        return cur.rowcount > 0


def delete_news_source(source_id: int) -> bool:
    where, params = ["id=?"], [source_id]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM news_sources WHERE {' AND '.join(where)}", params)
        return cur.rowcount > 0


# ---------------- xhs_accounts（小红书多账号，登录态加密存储） ----------------

def list_accounts() -> List[Dict[str, Any]]:
    """列出账号（不含加密登录数据）"""
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, nickname, xhs_user_id, avatar_url, status, added_at,"
            " last_used_at, last_check_at, check_note, daily_quota, quota_used,"
            f" quota_day, skip_until, fail_streak FROM xhs_accounts {clause} ORDER BY id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_account(account_id: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [account_id]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM xhs_accounts WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def find_account_by_xhs_user_id(xhs_user_id: str) -> Optional[Dict[str, Any]]:
    """按小红书用户 id 找当前用户名下的账号。找不到返回 None。"""
    xhs_user_id = (xhs_user_id or "").strip()
    if not xhs_user_id:
        return None
    where, params = ["xhs_user_id=?"], [xhs_user_id]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM xhs_accounts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None


def list_accounts_full() -> List[Dict[str, Any]]:
    """轮换用：含登录密文。不要把返回值交给前端。"""
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM xhs_accounts {clause} ORDER BY last_used_at ASC, id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def create_account(nickname: str = "", xhs_user_id: str = "") -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO xhs_accounts (user_id, nickname, xhs_user_id, status, added_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (_current_uid.get(), nickname, xhs_user_id, int(time.time())),
        )
        return cur.lastrowid


def update_account(account_id: int, fields: Dict[str, Any]) -> bool:
    allowed = {"nickname", "xhs_user_id", "avatar_url", "status",
               "login_data_enc", "last_used_at", "last_check_at", "check_note",
               "daily_quota", "quota_used", "quota_day", "skip_until", "fail_streak"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    where, wparams = ["id=?"], [account_id]
    _uw(where, wparams)
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE xhs_accounts SET {', '.join(f'{k}=?' for k in sets)} WHERE {' AND '.join(where)}",
            (*sets.values(), *wparams),
        )
        return cur.rowcount > 0


def delete_account(account_id: int) -> bool:
    where, params = ["id=?"], [account_id]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM xhs_accounts WHERE {' AND '.join(where)}", params)
        return cur.rowcount > 0


def get_default_account() -> Optional[Dict[str, Any]]:
    """默认账号：上次使用的有效账号"""
    where, params = ["status='valid'"], []
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM xhs_accounts WHERE {' AND '.join(where)}"
            " ORDER BY last_used_at DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None


def list_valid_accounts_lru() -> List[Dict[str, Any]]:
    """有效账号，按最久未使用排在前面，供采集轮询。"""
    where, params = ["status='valid'"], []
    _uw(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM xhs_accounts WHERE {' AND '.join(where)}"
            " ORDER BY last_used_at ASC, id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def next_collection_account() -> Optional[Dict[str, Any]]:
    """采集轮询：健康、未超配额、最久未使用的账号。没有则返回 None。"""
    from .account_rotation import first_eligible
    return first_eligible()


def record_note_collection(post_id: str, topic_id: Optional[int],
                           account_id: Optional[int], task_id: Optional[int] = None,
                           source: str = "account") -> None:
    """记下哪一个账号（或已登录的浏览器会话）采到了这条帖子。不存 Cookie。"""
    post_id = (post_id or "").strip()
    if not post_id:
        return
    source = source or "account"
    if not account_id and source != "browser_use_profile":
        return
    uid = _current_uid.get()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO note_collections (user_id, post_id, topic_id, account_id,"
            " task_id, source, collected_at) VALUES (?,?,?,?,?,?,?)",
            (uid, post_id, topic_id, account_id, task_id, source, int(time.time())),
        )


def list_note_collections(post_id: str) -> List[Dict[str, Any]]:
    where, params = ["post_id=?"], [post_id]
    _uw(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, post_id, topic_id, account_id, task_id, source, collected_at"
            f" FROM note_collections WHERE {' AND '.join(where)} ORDER BY id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def _public_note(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    raw = d.get("field_meta") or ""
    if isinstance(raw, str) and raw.strip():
        try:
            d["field_meta"] = json.loads(raw)
        except Exception:
            d["field_meta"] = {}
    elif not isinstance(raw, dict):
        d["field_meta"] = {}
    if "share_count_available" in d and d["share_count_available"] is not None:
        d["share_count_available"] = bool(d["share_count_available"])
    if not d.get("post_status"):
        d["post_status"] = "active"
    return d


# ---------------- news ----------------

def upsert_news(item: Dict[str, Any]) -> bool:
    """按 url 去重插入。返回 True 表示新增。"""
    with _lock, _connect() as conn:
        uid = _current_uid.get()
        if uid is None:
            uid = item.get("user_id")
        cur = conn.execute(
            "INSERT OR IGNORE INTO news (user_id, url, title, source, publish_time, publish_time_raw,"
            " summary, sentiment, sentiment_score, keyword, collected_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, item["url"], item.get("title", ""), item.get("source", ""),
             item.get("publish_time"), item.get("publish_time_raw", ""),
             item.get("summary", ""), item.get("sentiment", "neutral"),
             item.get("sentiment_score", 0.5), item.get("keyword", ""),
             item.get("collected_at", int(time.time())), int(time.time())))
        return cur.rowcount > 0


def recent_news_titles(limit: int = 500) -> List[str]:
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT title FROM news {clause} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [r["title"] for r in rows]


def list_news(q: Optional[str] = None, sentiment: Optional[str] = None,
              source: Optional[str] = None, days: Optional[int] = None,
              page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    where, params = [], []
    _uw(where, params)
    if q:
        where.append("(title LIKE ? OR summary LIKE ?)")
        params += [f"%{q}%"] * 2
    if sentiment:
        where.append("sentiment=?"); params.append(sentiment)
    if source:
        where.append("source=?"); params.append(source)
    if days:
        s, _ = _news_day_bounds(days)
        where.append("publish_time>=?"); params.append(s)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM news {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM news {clause} ORDER BY publish_time DESC NULLS LAST, id DESC"
            f" LIMIT ? OFFSET ?", (*params, page_size, (page - 1) * page_size)).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [dict(r) for r in rows]}


def _news_day_bounds(days: int):
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    today = _dt.datetime.now(tz).date()
    start = today - _dt.timedelta(days=days - 1)
    s = int(_dt.datetime(start.year, start.month, start.day, tzinfo=tz).timestamp())
    return s, None


def news_distinct_sources() -> List[str]:
    where, params = ["source<>''"], []
    _uw(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT source FROM news WHERE {' AND '.join(where)} ORDER BY source",
            params).fetchall()
        return [r["source"] for r in rows]


def news_timeline(days: int = 14) -> List[Dict[str, Any]]:
    """时间线：按天分组，每组含当天小红书声量/负面数 + 资讯列表"""
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=8))
    today = _dt.datetime.now(tz).date()
    out = []
    with _connect() as conn:
        for i in range(days):
            d = today - _dt.timedelta(days=i)
            ds = str(d)
            s = int(_dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
            e = s + 86399
            uw_sql, uw_params = uid_sql()
            xhs = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END)"
                f" FROM notes WHERE publish_time>=? AND publish_time<=?{uw_sql}",
                (s, e, *uw_params)).fetchone()
            items = conn.execute(
                "SELECT * FROM news WHERE publish_time>=? AND publish_time<=?"
                f"{uw_sql} ORDER BY publish_time DESC", (s, e, *uw_params)).fetchall()
            out.append({
                "date": ds,
                "xhs_posts": xhs[0] or 0,
                "xhs_negative": xhs[1] or 0,
                "news": [dict(r) for r in items],
            })
    return out


# ---------------- creations（内容创作中心） ----------------

def create_creation(ctype: str, title: str, inputs: Dict[str, Any],
                    content: str, parent_id: Optional[int] = None) -> int:
    import json as _json
    now = int(time.time())
    version = 1
    if parent_id:
        with _connect() as conn:
            row = conn.execute("SELECT version FROM creations WHERE id=?",
                               (parent_id,)).fetchone()
            if row:
                version = row["version"] + 1
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO creations (user_id, type, title, inputs_json, content, version,"
            " parent_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (_current_uid.get(), ctype, title, _json.dumps(inputs, ensure_ascii=False), content,
             version, parent_id, now, now))
        return cur.lastrowid


def list_creations(ctype: Optional[str] = None, q: Optional[str] = None,
                   page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    import json as _json
    where, params = [], []
    _uw(where, params)
    if ctype:
        where.append("type=?"); params.append(ctype)
    if q:
        where.append("(title LIKE ? OR content LIKE ?)")
        params += [f"%{q}%"] * 2
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM creations {clause}",
                             params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, type, title, version, parent_id, created_at, updated_at,"
            f" LENGTH(content) AS content_len FROM creations {clause}"
            f" ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size)).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "items": [dict(r) for r in rows]}


def get_creation(cid: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [cid]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM creations WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def creation_versions(cid: int) -> List[Dict[str, Any]]:
    """同一作品的所有版本（按 parent 链归集到根，再取全部子孙）"""
    uw_sql, uw_params = uid_sql()
    with _connect() as conn:
        root = cid
        seen = set()
        while root not in seen:
            seen.add(root)
            row = conn.execute(f"SELECT parent_id FROM creations WHERE id=?{uw_sql}",
                               (root, *uw_params)).fetchone()
            if not row or not row["parent_id"]:
                break
            root = row["parent_id"]
        fam = {root}
        changed = True
        while changed:
            changed = False
            for r in conn.execute(f"SELECT id, parent_id FROM creations WHERE 1=1{uw_sql}",
                                  uw_params).fetchall():
                if r["parent_id"] in fam and r["id"] not in fam:
                    fam.add(r["id"])
                    changed = True
        uw_sql2, uw_params2 = uid_sql()
        rows = conn.execute(
            f"SELECT id, title, version, created_at FROM creations"
            f" WHERE id IN ({','.join('?' * len(fam))}){uw_sql2} ORDER BY version",
            (*tuple(fam), *uw_params2)).fetchall()
        return [dict(r) for r in rows]


def delete_creation(cid: int) -> bool:
    where, params = ["id=?"], [cid]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM creations WHERE {' AND '.join(where)}", params)
        return cur.rowcount > 0


# ---------------- bids（方案比稿） ----------------

DEFAULT_BID_WEIGHTS = {"creativity": 0.3, "feasibility": 0.3,
                       "value": 0.25, "schedule": 0.15}


def create_bid(name: str, source_type: str, raw_text: str,
               filename: str = "") -> int:
    import json as _json
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO bids (user_id, name, source_type, filename, raw_text,"
            " extracted_json, scores_json, weights_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_current_uid.get(), name, source_type, filename, raw_text, "{}",
             _json.dumps({k: 0 for k in DEFAULT_BID_WEIGHTS}, ensure_ascii=False),
             _json.dumps(DEFAULT_BID_WEIGHTS, ensure_ascii=False),
             int(time.time())))
        return cur.lastrowid


def list_bids() -> List[Dict[str, Any]]:
    where, params = [], []
    _uw(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, source_type, filename, LENGTH(raw_text) AS text_len,"
            f" created_at FROM bids {clause} ORDER BY id DESC", params).fetchall()
        return [dict(r) for r in rows]


def get_bid(bid: int) -> Optional[Dict[str, Any]]:
    where, params = ["id=?"], [bid]
    _uw(where, params)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM bids WHERE {' AND '.join(where)}",
                           params).fetchone()
        return dict(row) if row else None


def update_bid_extracted(bid: int, extracted: Dict[str, Any]) -> bool:
    import json as _json
    where, params = ["id=?"], [bid]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE bids SET extracted_json=? WHERE {' AND '.join(where)}",
                           (_json.dumps(extracted, ensure_ascii=False), *params))
        return cur.rowcount > 0


def update_bid_scores(bid: int, scores: Dict[str, float],
                      weights: Optional[Dict[str, float]] = None) -> bool:
    import json as _json
    where, params = ["id=?"], [bid]
    _uw(where, params)
    clause = " AND ".join(where)
    with _lock, _connect() as conn:
        if weights:
            conn.execute(f"UPDATE bids SET weights_json=? WHERE {clause}",
                         (_json.dumps(weights, ensure_ascii=False), *params))
        cur = conn.execute(f"UPDATE bids SET scores_json=? WHERE {clause}",
                           (_json.dumps(scores, ensure_ascii=False), *params))
        return cur.rowcount > 0


def delete_bid(bid: int) -> bool:
    where, params = ["id=?"], [bid]
    _uw(where, params)
    with _lock, _connect() as conn:
        cur = conn.execute(f"DELETE FROM bids WHERE {' AND '.join(where)}", params)
        return cur.rowcount > 0


def finish_task(task_id: int, status: str, summary: str = "") -> None:
    """结束任务：状态 + 摘要写入 error/log_tail 字段。"""
    where, params = ["id=?"], [task_id]
    _uw(where, params)
    with _lock, _connect() as conn:
        conn.execute(
            f"UPDATE tasks SET status=?, finished_at=?, log_tail=log_tail || ? WHERE {' AND '.join(where)}",
            (status, int(time.time()), f"\n{summary}" if summary else "", *params))
