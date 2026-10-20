# -*- coding: utf-8 -*-
"""FastAPI 网站：话题管理 / 手动采集 / 任务监控 / 舆情浏览 / 趋势 / AI 指挥"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

from . import ai_chat, bid, crawler_driver, creator, db, email_verify, influence, reports, scheduler, site, trends
from .adapters import adapter_names, get_adapter, list_adapters
from .config import (BASE_DIR, DATA_DIR, STATIC_DIR, _is_placeholder, bind_host,
                     bind_port, ensure_dirs, get_ai_config, login_cache_dir,
                     public_ai_config, save_ai_config)
from .site import DynamicCORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("xhs-monitor.app")

app = FastAPI(title="Cortech Chat", version="2.0.0")
app.add_middleware(DynamicCORSMiddleware)  # 动态 CORS：保存域名后即时生效


# ---------------- auth（用户账号体系） ----------------

SESSION_COOKIE = "cortech_session"


def _session_token(request: Request) -> str:
    """网页用 httponly Cookie；浏览器插件在用户确认后改带 Bearer。

    Bearer 的值就是用户已经登录的 cortech_session，不另发密钥。
    """
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        bearer = auth[7:].strip()
        if bearer:
            return bearer
    return request.cookies.get(SESSION_COOKIE) or ""


def _require_csrf(request: Request) -> None:
    token = _session_token(request)
    provided = request.headers.get("x-csrf-token") or ""
    if not db.csrf_matches(token, provided):
        raise HTTPException(
            403, "安全校验未通过。请先在已登录的 Cortech Chat 里取得校验码，再确认发送。")

# 限流：内存字典 {ip: [timestamps]}
_login_attempts: Dict[str, List[float]] = {}
_register_attempts: Dict[str, List[float]] = {}


def _rate_ok(store: Dict[str, List[float]], key: str, limit: int, window_s: int) -> bool:
    now = time.time()
    lst = [t for t in store.get(key, []) if now - t < window_s]
    if len(lst) >= limit:
        return False
    lst.append(now)
    store[key] = lst
    return True


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthMiddleware(BaseHTTPMiddleware):
    """除 /api/auth/* 外，所有 /api/* 要求登录 session；设置 db 当前用户上下文。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (not path.startswith("/api/") or path.startswith("/api/auth/")
                or path == "/api/setup/status"):
            return await call_next(request)
        uid = None
        token = _session_token(request)
        if token:
            user = db.get_session_user(token)
            if user:
                uid = user["id"]
        if uid is None:
            return JSONResponse({"detail": "未登录，请先登录"}, status_code=401)
        db.set_current_user(uid)
        try:
            return await call_next(request)
        finally:
            db.set_current_user(None)


app.add_middleware(AuthMiddleware)


def _set_session_cookie(resp: JSONResponse, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, max_age=db.SESSION_TTL,
                    httponly=True, samesite="lax", path="/")


class AuthBody(BaseModel):
    email: str = ""
    password: str = ""
    code: str = ""  # 注册验证码


class SendCodeBody(BaseModel):
    email: str = ""


def _check_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "邮箱格式不正确")
    if len(email) > 120:
        raise HTTPException(400, "邮箱过长")
    return email


@app.get("/api/setup/status")
def api_setup_status(request: Request):
    """公开自检：库、数据目录、登录组件、当前用户是否已有小红书账号。"""
    from .setup_status import build_setup_status
    uid = None
    token = _session_token(request)
    if token:
        user = db.get_session_user(token)
        if user:
            uid = user["id"]
    return build_setup_status(uid)


@app.get("/api/auth/status")
def api_auth_status():
    """公开：是否有已注册用户（前端决定跳登录还是注册）。"""
    return {"has_users": db.count_users() > 0}


@app.post("/api/auth/send-code")
def api_auth_send_code(body: SendCodeBody, request: Request):
    """发送注册验证码。开发模式（未配 SMTP）时验证码打印到服务端控制台。
    注：本地单机版默认不强制验证码，此接口保留供云端版使用。"""
    ip = _client_ip(request)
    if not _rate_ok(_register_attempts, ip, 20, 3600):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    email = _check_email(body.email)
    ok, msg = email_verify.send_code(email)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg,
            "dev_mode": not email_verify.smtp_configured()}


@app.post("/api/auth/register")
def api_auth_register(body: AuthBody, request: Request):
    ip = _client_ip(request)
    if not _rate_ok(_register_attempts, ip, 20, 3600):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    email = _check_email(body.email)
    # 本地单机版：不强制邮箱验证码（email_verify 保留供云端版使用）
    password = body.password or ""
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if len(password) > 72:  # bcrypt 上限
        raise HTTPException(400, "密码过长")
    try:
        uid = db.create_user(email, password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = db.create_auth_session(uid)
    user = db.get_user(uid)
    resp = JSONResponse({"id": user["id"], "email": user["email"]})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def api_auth_login(body: AuthBody, request: Request):
    ip = _client_ip(request)
    if not _rate_ok(_login_attempts, ip, 10, 300):
        raise HTTPException(429, "登录尝试过多，请 5 分钟后再试")
    email = _check_email(body.email)
    user = db.verify_user(email, body.password or "")
    if not user:
        raise HTTPException(401, "邮箱或密码错误")
    token = db.create_auth_session(user["id"])
    resp = JSONResponse({"id": user["id"], "email": user["email"]})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    token = _session_token(request)
    if token:
        db.delete_auth_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    token = _session_token(request)
    user = db.get_session_user(token) if token else None
    if not user:
        raise HTTPException(401, "未登录")
    return {"id": user["id"], "email": user["email"],
            "created_at": user["created_at"]}


@app.get("/api/auth/csrf")
def api_auth_csrf(request: Request):
    """给已登录用户（含浏览器插件）签发一次性页面校验码。不返回会话本身。"""
    token = _session_token(request)
    csrf = db.issue_csrf(token) if token else None
    if not csrf:
        raise HTTPException(401, "未登录，请先登录 Cortech Chat")
    return {"csrf_token": csrf, "product": "Cortech Chat"}
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


# ---------------- models ----------------

def _content_disposition(fname: str) -> str:
    """中文文件名下载头（RFC 5987）：ascii 回退 + UTF-8 编码名"""
    import urllib.parse
    ascii_fallback = "export.csv"
    return (f"attachment; filename=\"{ascii_fallback}\"; "
            f"filename*=UTF-8''{urllib.parse.quote(fname)}")


class TopicIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    keywords: str = Field(min_length=1, description="多个关键词用英文逗号分隔")
    max_notes: int = Field(default=50, ge=1, le=500)
    enable_comments: int = Field(default=1, ge=0, le=1)
    cron: str = Field(default="0 2 * * *")
    enabled: int = Field(default=1, ge=0, le=1)
    engine: str = Field(default="cortech_direct")


class TopicPatch(BaseModel):
    name: Optional[str] = None
    keywords: Optional[str] = None
    max_notes: Optional[int] = Field(default=None, ge=1, le=500)
    enable_comments: Optional[int] = Field(default=None, ge=0, le=1)
    cron: Optional[str] = None
    enabled: Optional[int] = Field(default=None, ge=0, le=1)
    engine: Optional[str] = None


class CrawlIn(BaseModel):
    max_notes: Optional[int] = Field(default=None, ge=1, le=500)
    enable_comments: Optional[bool] = None
    engine: Optional[str] = None
    account_id: Optional[int] = None
    aggressive: bool = False
    use_browser_profile: bool = False


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: Optional[list] = None
    confirmation_id: Optional[str] = None
    confirmed: Optional[bool] = None


class AIConfigIn(BaseModel):
    active: Optional[str] = None
    providers: Optional[Dict[str, Dict[str, str]]] = None


class AITestIn(BaseModel):
    provider: Optional[str] = None


class NewsSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="search", description="search=新闻搜索, rss=RSS订阅")
    keywords: str = Field(default="", description="多个关键词用英文逗号分隔")
    url: str = Field(default="", description="kind=rss 时为 RSS 地址（多个换行分隔）")
    cron: str = Field(default="0 3 * * *")
    enabled: int = Field(default=1, ge=0, le=1)


class NewsSourcePatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    keywords: Optional[str] = None
    url: Optional[str] = None
    cron: Optional[str] = None
    enabled: Optional[int] = Field(default=None, ge=0, le=1)


# ---------------- lifecycle ----------------

@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    db.init_db()
    db.seed_topics()
    db.seed_news_sources()
    # 内置种子数据（幂等）：十五周年 85 条真实帖子 + DFS 48 个嫌疑账号
    try:
        from .seed_anniversary import seed_anniversary
        r = seed_anniversary()
        logger.info("种子数据：十五周年帖子 total=%d inserted=%d skipped=%d",
                    r["total"], r["inserted"], r["skipped"])
    except Exception as e:
        logger.warning("十五周年种子数据导入跳过：%s", e)
    try:
        from .seed_dfs_suspects import seed_dfs_suspects
        r = seed_dfs_suspects()
        logger.info("种子数据：DFS 嫌疑账号 total=%d inserted=%d skipped=%d",
                    r["total"], r["inserted"], r["skipped"])
    except Exception as e:
        logger.warning("DFS 嫌疑账号种子数据导入跳过：%s", e)
    # P0 升级：存量品牌补导入判定模式特征（幂等）
    try:
        from .fakecheck import ensure_pattern_features
        r = ensure_pattern_features()
        logger.info("判定模式特征补导入：%d 条", r["imported"])
    except Exception as e:
        logger.warning("判定模式特征补导入跳过：%s", e)
    scheduler.start()
    if (FRONTEND_DIST / "index.html").exists():
        logger.info("检测到前端构建产物，将托管 frontend/dist")


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.shutdown()


# ---------------- topics ----------------

@app.get("/api/topics")
def api_list_topics():
    items = db.list_topics()
    # 竞品话题打标（帮手 G：名称前缀"竞品对标·"），前端可据此展示徽章或过滤
    for t in items:
        t["is_competitor"] = str(t.get("name") or "").startswith("竞品对标·")
    return {"items": items}


@app.post("/api/topics")
def api_create_topic(body: TopicIn):
    topic_id = db.create_topic(body.name, body.keywords, body.max_notes,
                               body.enable_comments, body.cron, body.enabled,
                               body.engine)
    scheduler.refresh_jobs()
    return {"id": topic_id}


@app.patch("/api/topics/{topic_id}")
def api_update_topic(topic_id: int, body: TopicPatch):
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not db.update_topic(topic_id, fields):
        raise HTTPException(404, "话题不存在")
    scheduler.refresh_jobs()
    return {"ok": True}


@app.delete("/api/topics/{topic_id}")
def api_delete_topic(topic_id: int):
    if not db.delete_topic(topic_id):
        raise HTTPException(404, "话题不存在")
    scheduler.refresh_jobs()
    return {"ok": True}


# ---------------- engines ----------------

@app.get("/api/engines")
def api_list_engines():
    items = []
    for a in list_adapters():
        ok, hint = a.check_login()
        items.append({
            "name": a.name, "title": a.title, "description": a.description,
            "supports_keyword_search": a.supports_keyword_search,
            "supports_comments": a.supports_comments,
            "supports_author_info": a.supports_author_info,
            "needs_browser": a.needs_browser, "needs_node": a.needs_node,
            "login_method": a.login_method,
            "login_ok": ok, "login_hint": hint,
        })
    return {"items": items}


# ---------------- crawl / tasks ----------------

@app.post("/api/topics/{topic_id}/crawl")
def api_trigger_crawl(topic_id: int, body: CrawlIn = CrawlIn()):
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "话题不存在")
    engine = body.engine or topic.get("engine") or "cortech_direct"
    if engine not in adapter_names():
        raise HTTPException(400, f"未知引擎 {engine}")
    if db.running_task_exists() or crawler_driver.is_running():
        raise HTTPException(409, "已有采集任务在运行，请稍后再试")
    try:
        if body.use_browser_profile and body.account_id:
            raise HTTPException(400, "请只选择一种登录态：指定账号，或使用你已经登录的浏览器 profile。")
        task_id = crawler_driver.trigger_crawl(
            topic_id, engine=engine, max_notes=body.max_notes,
            enable_comments=body.enable_comments,
            account_id=body.account_id, aggressive=body.aggressive,
            use_browser_profile=body.use_browser_profile)
    except ValueError as e:
        message = str(e)
        code = "need_xhs_session" if "need_xhs_session" in message else (
            "need_xhs_rescan" if "need_xhs_rescan" in message else "crawl_rejected")
        return JSONResponse(status_code=400, content={
            "detail": message,
            "code": code,
            "message": message,
            "needs_real_xhs_session": code.startswith("need_xhs"),
        })
    from .config import collection_fixture_enabled
    task = db.get_task(task_id) or {}
    if collection_fixture_enabled():
        message = "已启动本地样例采集。这不是真实小红书会话，真实采集仍需本人扫码。"
    else:
        message = "采集任务已开始。"
    return {"task_id": task_id, "engine": "cortech_crawler",
            "account_id": task.get("account_id"), "aggressive": body.aggressive,
            "needs_real_xhs_session": False,
            "message": message}


# ---------------- 话题全量采集（tag 扩展 + 一次性深采） ----------------

@app.post("/api/topics/{topic_id}/expand-tags")
def api_expand_tags(topic_id: int):
    """话题关键词扩展到 20 个 tag（可预览调整）"""
    from . import topic_crawl
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "话题不存在")
    try:
        tags = topic_crawl.expand_topic_tags(topic_id, target=20)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"topic_id": topic_id, "tags": tags, "count": len(tags)}


@app.get("/api/topics/{topic_id}/discovered-tags")
def api_discovered_tags(topic_id: int):
    """从已采集帖子中自动发现的新 tag（需用户确认后加入采集）"""
    from . import topic_crawl
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "话题不存在")
    tags = topic_crawl.discover_tags(topic_id)
    saved = db.list_topic_tags(topic_id, kind="discovered")
    return {"topic_id": topic_id, "discovered": tags, "saved": saved}


@app.post("/api/topics/{topic_id}/deep-crawl")
def api_deep_crawl(topic_id: int, body: CrawlIn = CrawlIn()):
    """一个话题一次性全量采集：20 个 tag 逐个搜索 → 去重 → 详情+评论+作者 → 情感分析 → 入库"""
    from . import topic_crawl
    from .account_rotation import block_reason, first_eligible, mark_success
    from .browser_use_worker import BrowserUseError, can_use, ensure_ready
    from .config import collection_fixture_enabled
    topic = db.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "话题不存在")
    if db.running_task_exists() or crawler_driver.is_running():
        raise HTTPException(409, "已有采集任务在运行，请稍后再试")
    if body.use_browser_profile and body.account_id:
        raise HTTPException(400, "请只选择一种登录态：指定账号，或使用你已经登录的浏览器 profile。")
    resolved = body.account_id
    use_profile = bool(body.use_browser_profile)
    if not collection_fixture_enabled():
        if use_profile:
            try:
                ensure_ready()
            except BrowserUseError as e:
                raise HTTPException(400, str(e))
        elif resolved:
            reason = block_reason(db.get_account(resolved))
            if reason:
                raise HTTPException(400, f"这个账号这次不参与采集：{reason}")
        else:
            picked = first_eligible()
            if picked:
                resolved = picked["id"]
            elif can_use():
                use_profile = True
            else:
                try:
                    crawler = topic_crawl._get_crawler().get_crawler()
                    ok, hint = crawler.check_login()
                except Exception as e:
                    raise HTTPException(500, f"采集器初始化失败：{e}")
                if not ok:
                    raise HTTPException(
                        401, f"未登录小红书：{hint}。请先在「账号管理」扫码，或用插件确认发送你自己的登录态。")
    # 确保有可用的 tag
    tags = topic_crawl.get_crawl_tags(topic_id)
    if not tags:
        raise HTTPException(400, "话题没有可用的采集 tag，请先调用 /expand-tags 扩展")
    # 后台线程执行（复用 crawler_driver 的任务机制）
    import threading
    uid = db.current_user_id()

    def _run():
        db.set_current_user(uid)
        task_id = db.create_task(topic_id, task_type="deep_crawl", account_id=resolved)
        try:
            db.update_task(task_id, status="running",
                           started_at=int(time.time()))
            def _progress(pct: int, msg: str):
                if pct >= 0:
                    db.update_task(task_id, log_tail=f"[{pct}%] {msg}")
                else:
                    db.update_task(task_id, log_tail=msg)
            if use_profile:
                from .browser_use_worker import session_scope
                with session_scope() as profile_dir:
                    result = topic_crawl.deep_crawl_topic(
                        topic_id, progress_cb=_progress,
                        max_notes_per_tag=body.max_notes or 50,
                        enable_comments=body.enable_comments
                        if body.enable_comments is not None else True,
                        profile_dir=profile_dir)
            else:
                result = topic_crawl.deep_crawl_topic(
                    topic_id, progress_cb=_progress,
                    max_notes_per_tag=body.max_notes or 50,
                    enable_comments=body.enable_comments
                    if body.enable_comments is not None else True,
                    account_id=resolved)
            if resolved and not collection_fixture_enabled():
                mark_success(resolved, result.get("notes_total") or 0)
            db.update_task(task_id, status="done",
                           finished_at=int(time.time()),
                           notes_count=result.get("notes_stored", 0),
                           log_tail=f"完成：{result.get('notes_stored', 0)} 帖 / "
                                    f"{result.get('comments_stored', 0)} 评论")
        except Exception as e:
            db.update_task(task_id, status="failed",
                           finished_at=int(time.time()),
                           error=str(e)[:500])
        finally:
            db.set_current_user(None)
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "topic_id": topic_id, "tags_count": len(tags),
            "message": f"已启动全量采集，共 {len(tags)} 个 tag"}


@app.post("/api/tasks/{task_id}/stop")
def api_stop_task(task_id: int):
    """停止采集任务（进度条旁的"停止"按钮）"""
    if not crawler_driver.request_stop(task_id):
        raise HTTPException(400, "任务不存在或已结束，无法停止")
    return {"ok": True, "task_id": task_id}


@app.get("/api/tasks")
def api_list_tasks(limit: int = Query(50, ge=1, le=200)):
    return {"items": db.list_tasks(limit)}


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


# ---------------- notes / comments ----------------

@app.get("/api/posts")
def api_list_posts(topic_id: Optional[int] = None,
                   sentiment: Optional[str] = None,
                   q: Optional[str] = None,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    if sentiment and sentiment not in ("negative", "neutral", "positive"):
        raise HTTPException(400, "sentiment 非法")
    return db.list_posts(topic_id, sentiment, q, page, page_size,
                         date_from, date_to)


@app.get("/api/posts/{post_id}/comments")
def api_list_comments(post_id: str,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(50, ge=1, le=200)):
    return db.list_comments(post_id, page, page_size)


# ---------------- stats / trends / briefing / export ----------------

@app.get("/api/stats")
def api_stats(topic_id: Optional[int] = None):
    return db.stats(topic_id)


@app.get("/api/trends")
def api_trends(topic_id: Optional[int] = None,
               days: int = Query(30, ge=1, le=365),
               rank_type: str = Query("total", pattern="^(total|incremental)$")):
    """趋势。rank_type: total 总量榜 | incremental 增量榜（对标千瓜 rankType）。"""
    return trends.get_trends(topic_id, days, rank_type)


@app.get("/api/timeline")
def api_timeline(topic_id: int,
                 granularity: str = Query("auto", pattern="^(auto|day|week|month)$")):
    """话题时间线：按发布时间分档的趋势（方案A：知微式三层）。"""
    from . import timeline
    return timeline.get_timeline(topic_id, granularity)


@app.get("/api/hot-words/detail")
def api_hot_word_detail(word: str,
                        topic_id: Optional[int] = None,
                        days: int = Query(30, ge=1, le=365)):
    """热词详情：关联品牌 + 关联达人（对标千瓜三维关联）。"""
    return trends.hot_word_detail(word, topic_id, days)


@app.get("/api/influence")
def api_influence(topic_id: Optional[int] = None,
                  days: int = Query(30, ge=1, le=365),
                  top_n: int = Query(50, ge=1, le=200)):
    """达人影响力榜：新榜指数公式 → 小红书影响力分。"""
    return influence.author_influence(topic_id, days, top_n)


@app.get("/api/sov")
def api_sov(topic_id: int,
            days: int = Query(30, ge=1, le=365)):
    """SOV 声量份额：品牌声量 / 品类总声量。"""
    return influence.brand_sov(topic_id, days)


@app.get("/api/briefing")
def api_briefing(topic_id: Optional[int] = None,
                 days: int = Query(7, ge=1, le=90)):
    return trends.build_briefing(topic_id, days)


@app.get("/api/export")
def api_export(topic_id: Optional[int] = None,
               sentiment: Optional[str] = None,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None,
               format: str = Query("csv", pattern="^(csv|xlsx|json|md|html|pdf)$")):
    """帖子导出（多格式）。format=csv|xlsx|json|md|html|pdf"""
    ext, media_type, fn, is_binary = reports.CORTECH_EXPORT_FORMATS[format]
    fname, data = fn(topic_id, sentiment, date_from, date_to)
    body = data if is_binary else data.encode("utf-8")
    return StreamingResponse(
        iter([body]),
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(fname)},
    )


@app.get("/api/export.csv")
def api_export_csv(topic_id: Optional[int] = None,
                   sentiment: Optional[str] = None,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None):
    """帖子导出 CSV（兼容旧路由）。"""
    return api_export(topic_id, sentiment, date_from, date_to, format="csv")


@app.get("/api/export-comments.csv")
def api_export_comments_csv(post_id: Optional[str] = None,
                            topic_id: Optional[int] = None):
    """评论导出（规范列名 + 规范文件名）"""
    fname, text = reports.export_comments_csv(post_id, topic_id)
    return StreamingResponse(
        iter([text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(fname)},
    )


@app.get("/api/reports/briefing")
def api_report_briefing(topic_id: Optional[int] = None,
                        days: int = Query(7, ge=1, le=90)):
    """规范化舆情简报"""
    return reports.build_briefing_report(topic_id, days)


@app.get("/api/reports/special")
def api_report_special(topic_id: Optional[int] = None,
                       days: int = Query(30, ge=1, le=180),
                       title: str = Query("专项舆情报告", max_length=100)):
    """规范化专项报告"""
    return reports.build_special_report(topic_id, days, title)


@app.get("/api/download/{filename}")
def api_download(filename: str):
    safe = Path(filename).name
    if not safe.endswith(".csv") or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = DATA_DIR / "exports" / safe
    if not path.exists():
        raise HTTPException(404, "文件不存在或已过期")
    return FileResponse(str(path), media_type="text/csv; charset=utf-8",
                        filename=safe)


# ---------------- 全网资讯 ----------------
@app.get("/api/news/sources")
def api_list_news_sources():
    return {"items": db.list_news_sources()}


@app.post("/api/news/sources")
def api_create_news_source(body: NewsSourceIn):
    sid = db.create_news_source(body.name, body.kind, body.keywords,
                                body.url, body.cron, body.enabled)
    scheduler.refresh_jobs()
    return {"id": sid}


@app.patch("/api/news/sources/{source_id}")
def api_update_news_source(source_id: int, body: NewsSourcePatch):
    fields = {k: v for k, v in body.dict().items() if v is not None}
    ok = db.update_news_source(source_id, fields)
    if not ok:
        raise HTTPException(404, "资讯源不存在")
    scheduler.refresh_jobs()
    return {"ok": True}


@app.delete("/api/news/sources/{source_id}")
def api_delete_news_source(source_id: int):
    if not db.delete_news_source(source_id):
        raise HTTPException(404, "资讯源不存在")
    scheduler.refresh_jobs()
    return {"ok": True}


@app.post("/api/news/sources/{source_id}/collect")
def api_collect_news(source_id: int):
    """手动触发资讯采集（与 xhs 采集共用单任务锁）"""
    from .news import driver as news_driver
    try:
        return news_driver.trigger_news_collection(source_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/news")
def api_list_news(q: Optional[str] = None,
                  sentiment: Optional[str] = None,
                  source: Optional[str] = None,
                  days: Optional[int] = Query(None, ge=1, le=365),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    return db.list_news(q, sentiment, source, days, page, page_size)


@app.get("/api/news/sources/distinct")
def api_news_sources_distinct():
    return {"items": db.news_distinct_sources()}


@app.get("/api/news/briefing-section")
def api_news_briefing_section(days: int = Query(7, ge=1, le=90)):
    """全网资讯动态一节（供简报/前端调用）"""
    from .news import briefing as news_briefing
    return {"section": news_briefing.briefing_section(days)}


@app.get("/api/news/timeline")
def api_news_timeline(days: int = Query(14, ge=1, le=60)):
    """资讯时间线：按天倒序分组，顶部叠加小红书站内声量迷你条"""
    return {"days": days, "items": db.news_timeline(days)}


# ---------------- 内容创作中心 ----------------

class CreatorGenerateIn(BaseModel):
    type: str
    inputs: Dict[str, str]
    inspiration: Optional[str] = ""


class CreatorSaveIn(BaseModel):
    type: str
    title: str = Field(default="", max_length=100)
    inputs: Dict[str, str] = {}
    content: str
    parent_id: Optional[int] = None


@app.get("/api/creator/types")
def api_creator_types():
    return {"items": creator.CREATION_TYPES}


@app.get("/api/creator/prompts/{type_id}")
def api_creator_prompt(type_id: str):
    """查看/运营可改的 prompt 模板（文件在 monitor/prompts/）"""
    try:
        return {"type": type_id, "template": creator.load_template(type_id)}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/creator/inspiration")
def api_creator_inspiration(days: int = Query(7, ge=1, le=90),
                            topic_id: Optional[int] = None):
    return {"text": creator.inspiration_text(days, topic_id)}


@app.post("/api/creator/generate")
def api_creator_generate(body: CreatorGenerateIn):
    try:
        return creator.generate(body.type, body.inputs, body.inspiration or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


class CreatorFreeIn(BaseModel):
    request: str
    inspiration: Optional[str] = ""


@app.post("/api/creator/generate-free")
def api_creator_generate_free(body: CreatorFreeIn):
    """自由创作：用户自然语言直接生成，不强制选模板填表单。"""
    try:
        return creator.generate_free(body.request, body.inspiration or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/api/creator/items")
def api_creator_items(type: Optional[str] = None, q: Optional[str] = None,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(20, ge=1, le=100)):
    return db.list_creations(type, q, page, page_size)


@app.post("/api/creator/items")
def api_creator_save(body: CreatorSaveIn):
    title = body.title or (body.inputs.get("theme", "")[:30] or "未命名创作")
    cid = db.create_creation(body.type, title, body.inputs, body.content,
                             body.parent_id)
    return {"id": cid}


@app.get("/api/creator/items/{cid}")
def api_creator_get(cid: int):
    item = db.get_creation(cid)
    if not item:
        raise HTTPException(404, "创作不存在")
    return item


@app.get("/api/creator/items/{cid}/versions")
def api_creator_versions(cid: int):
    return {"items": db.creation_versions(cid)}


@app.delete("/api/creator/items/{cid}")
def api_creator_delete(cid: int):
    if not db.delete_creation(cid):
        raise HTTPException(404, "创作不存在")
    return {"ok": True}


# ---------------- 方案比稿 ----------------

class BidTextIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1)


class BidScoresIn(BaseModel):
    scores: Dict[str, float]
    weights: Optional[Dict[str, float]] = None


@app.get("/api/bids")
def api_list_bids():
    return {"items": db.list_bids()}


@app.post("/api/bids/text")
def api_create_bid_text(body: BidTextIn):
    """粘贴文本录入比稿方案"""
    bid_id = db.create_bid(body.name, "text", body.text)
    try:
        extracted = bid.ai_extract(body.text)
    except Exception as e:  # noqa: BLE001
        extracted = {"_mode": "error", "_error": f"{type(e).__name__}: {e}"}
    db.update_bid_extracted(bid_id, extracted)
    return {"id": bid_id, "extracted": extracted}


@app.post("/api/bids/upload")
async def api_create_bid_upload(name: str = Query(..., max_length=100),
                                file: UploadFile = File(...)):
    """上传方案文件录入比稿（支持 pdf/docx/xlsx/csv/txt/md/html/json，文件仅存本地 data/bids/）"""
    from .bid import BIDS_DIR, extract_text_from_file
    BIDS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload").name
    dest = BIDS_DIR / f"{int(time.time())}_{safe_name}"
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件过大（限 20MB）")
    dest.write_bytes(content)
    try:
        text = extract_text_from_file(dest)
    except ValueError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(e))
    if not text.strip():
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "未能从文件中提取到文本")
    bid_id = db.create_bid(name, "file", text, filename=safe_name)
    try:
        extracted = bid.ai_extract(text)
    except Exception as e:  # noqa: BLE001
        extracted = {"_mode": "error", "_error": f"{type(e).__name__}: {e}"}
    db.update_bid_extracted(bid_id, extracted)
    return {"id": bid_id, "extracted": extracted, "file": safe_name}


@app.get("/api/bids/compare")
def api_bids_compare():
    """横向对比表 + 雷达图数据 + 排名"""
    return bid.compare()


@app.get("/api/bids/report")
def api_bids_report():
    """比稿评审报告（统一报告规范）"""
    return bid.build_report()


@app.get("/api/bids/{bid_id}")
def api_get_bid(bid_id: int):
    item = db.get_bid(bid_id)
    if not item:
        raise HTTPException(404, "比稿方案不存在")
    return item


@app.delete("/api/bids/{bid_id}")
def api_delete_bid(bid_id: int):
    if not db.delete_bid(bid_id):
        raise HTTPException(404, "比稿方案不存在")
    return {"ok": True}


@app.put("/api/bids/{bid_id}/extracted")
def api_bid_save_extracted(bid_id: int, body: dict):
    """编辑拆解/保存（六维 key 白名单校验在 bid.save_extracted 内）"""
    from . import bid as bid_mod
    if not db.get_bid(bid_id):
        raise HTTPException(404, "比稿方案不存在")
    extracted = body.get("extracted") if isinstance(body, dict) else None
    if not bid_mod.save_extracted(bid_id, extracted or {}):
        raise HTTPException(400, "无有效拆解维度")
    return {"ok": True, "bid_id": bid_id}


@app.post("/api/bids/{bid_id}/reextract")
def api_bid_reextract(bid_id: int):
    """重新拆解（AI 六维提取，直接返回 extracted 字典）"""
    from . import bid as bid_mod
    if not db.get_bid(bid_id):
        raise HTTPException(404, "比稿方案不存在")
    return bid_mod.reextract(bid_id)


@app.put("/api/bids/{bid_id}/scores")
def api_bid_scores(bid_id: int, body: BidScoresIn):
    """四维评分（权重可调）"""
    valid = {k for k, _ in bid.SCORE_DIMS}
    scores = {k: max(0, min(10, float(v))) for k, v in body.scores.items()
              if k in valid}
    weights = None
    if body.weights:
        weights = {k: float(v) for k, v in body.weights.items() if k in valid}
        if not weights:
            weights = None
    if not db.update_bid_scores(bid_id, scores, weights):
        raise HTTPException(404, "比稿方案不存在")
    return {"ok": True,
            "total": bid.weighted_total(scores,
                                        weights or db.DEFAULT_BID_WEIGHTS)}


# ---------------- 签名漂移检测（REA v2 D6） ----------------

@app.get("/api/sign-drift/status")
def api_sign_drift_status():
    """签名健康状态（green/yellow/red）"""
    from . import sign_drift
    return sign_drift.get_sign_status()


@app.post("/api/sign-drift/probe")
def api_sign_drift_probe(body: dict = None):
    """手动触发 L2 探针（2 个请求）"""
    import asyncio
    from . import sign_drift
    b = body or {}
    result = asyncio.run(sign_drift.l2_probe(b.get("account_id")))
    return result


# ---------------- 品牌档案（通用化：网站通用，品牌只是配置） ----------------

@app.get("/api/brand")
def api_get_brand():
    """当前品牌档案（名称/简称），换品牌时修改此处即可"""
    return site.get_brand()


@app.put("/api/brand")
def api_put_brand(body: dict):
    b = body or {}
    try:
        return site.save_brand(b.get("name", ""), b.get("short", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- 域名绑定（自助接入） ----------------

class SiteConfigIn(BaseModel):
    domain: str = Field(default="", max_length=253, description="如 monitor.example.com，空字符串=解绑")
    cors_origins: Optional[list] = Field(default=None, description="不填则自动按域名生成")
    api_base: str = Field(default="/api", description="前端 API 基地址，默认相对路径同源")


@app.get("/api/site-config")
def api_get_site_config():
    return site.public_site_config()


@app.put("/api/site-config")
def api_put_site_config(body: SiteConfigIn):
    """填域名自动配置：CORS 白名单 + nginx 配置生成 + certbot 命令（旧配置自动备份）"""
    try:
        return site.save_site_config(body.domain, body.cors_origins, body.api_base)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/site-config/check")
def api_check_site(domain: Optional[str] = Query(None)):
    """接入状态自检：DNS 解析 → 本机公网 IP 比对 → HTTPS 访问本站"""
    target = domain or site.get_site_config().get("domain", "")
    if not target:
        raise HTTPException(400, "尚未绑定域名")
    try:
        return site.check_domain(target)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/site-config/nginx")
def api_download_nginx_conf():
    """下载当前域名的 nginx 配置文件"""
    cfg = site.get_site_config()
    if not cfg.get("domain"):
        raise HTTPException(400, "尚未绑定域名")
    path = site.NGINX_DIR / f"{cfg['domain']}.conf"
    if not path.exists():
        site.generate_nginx_conf(cfg["domain"])
    return FileResponse(str(path), media_type="text/plain; charset=utf-8",
                        filename=path.name)


# ---------------- P0：分级预警中心（帮手 H：monitor/alerts.py） ----------------

@app.get("/api/alerts/tiered")
def api_tiered_alerts(topic_id: Optional[int] = None):
    """分级预警（即时计算版）：critical 紧急 / high 严重 / medium 关注 / normal 正常。

    注意：必须定义在 /api/alerts/{alert_id} 之前，否则 "tiered" 会被误判为 alert_id。
    """
    from . import analysis
    return analysis.tiered_alerts(topic_id)


@app.get("/api/alerts")
def api_list_alerts(status: Optional[str] = None, level: Optional[str] = None,
                    limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0)):
    from . import alerts
    return {"items": alerts.list_alerts(status, level, limit, offset)}


@app.get("/api/alerts/unread_count")
def api_alerts_unread():
    from . import alerts
    return {"count": alerts.unread_count()}

@app.get("/api/alerts/rules")
def api_alert_rules():
    from . import alerts
    return {"items": alerts.list_rules()}

@app.post("/api/alerts/rules")
def api_save_alert_rule(body: dict):
    from . import alerts
    try:
        return alerts.save_rule(body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get("/api/alerts/push_config")
def api_push_config():
    from . import alerts
    return alerts.get_push_config_masked()

@app.post("/api/alerts/push_config")
def api_save_push_config(body: dict):
    from . import alerts
    return alerts.save_push_config(body or {})

@app.post("/api/alerts/push_test")
def api_push_test(body: dict):
    from . import alerts
    ok, msg = alerts.test_push((body or {}).get("channel", ""))
    return {"ok": ok, "message": msg}



@app.get("/api/alerts/{alert_id}")
def api_get_alert(alert_id: int):
    from . import alerts
    a = alerts.get_alert(alert_id)
    if not a:
        raise HTTPException(404, "预警不存在")
    return a


@app.post("/api/alerts/{alert_id}/status")
def api_alert_status(alert_id: int, body: dict):
    from . import alerts
    st = (body or {}).get("status")
    if st not in ("unread", "read", "handled", "ignored"):
        raise HTTPException(400, "status 非法")
    if not alerts.set_alert_status(alert_id, st):
        raise HTTPException(404, "预警不存在")
    return {"ok": True}


@app.post("/api/alerts/{alert_id}/push")
def api_alert_push(alert_id: int, body: dict):
    from . import alerts
    ok, msg = alerts.manual_push(alert_id, (body or {}).get("channel", ""))
    return {"ok": ok, "message": msg}


@app.delete("/api/alerts/rules/{rule_id}")
def api_delete_alert_rule(rule_id: int):
    from . import alerts
    if not alerts.delete_rule(rule_id):
        raise HTTPException(404, "规则不存在")
    return {"ok": True}


@app.post("/api/alerts/rules/{rule_id}/enable")
def api_alert_rule_enable(rule_id: int, body: dict):
    from . import alerts
    if not alerts.set_rule_enabled(rule_id, bool((body or {}).get("enabled", True))):
        raise HTTPException(404, "规则不存在")
    return {"ok": True}

# ---------------- P0：竞品对标（帮手 G：monitor/competitors.py） ----------------

@app.get("/api/competitors")
def api_list_competitors():
    """竞品列表（竞品=站内普通话题，口径一致）"""
    from . import competitors
    return {"items": competitors.list_competitors()}


@app.post("/api/competitors")
def api_create_competitor(body: dict):
    from . import competitors
    b = body or {}
    if not b.get("name") or not b.get("keywords"):
        raise HTTPException(400, "缺少 name 或 keywords")
    return competitors.create_competitor(
        b["name"], b["keywords"],
        is_self=bool(b.get("is_self", False)),
        max_notes=int(b.get("max_notes", 50)))


@app.put("/api/competitors/{cid}")
def api_update_competitor(cid: int, body: dict):
    from . import competitors
    try:
        return competitors.update_competitor(cid, body or {})
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/competitors/{cid}")
def api_delete_competitor(cid: int):
    from . import competitors
    if not competitors.delete_competitor(cid):
        raise HTTPException(404, "竞品不存在")
    return {"ok": True}


@app.post("/api/competitors/{cid}/crawl")
def api_crawl_competitor(cid: int, body: dict = None):
    from . import competitors
    b = body or {}
    try:
        return competitors.crawl_competitor(
            cid, account_id=b.get("account_id"),
            aggressive=bool(b.get("aggressive", False)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/competitors/dashboard")
def api_competitors_dashboard(days: int = Query(30, ge=1, le=90)):
    from . import competitors
    return competitors.dashboard(days)


@app.get("/api/competitors/commentary")
def api_competitors_commentary(days: int = Query(30, ge=1, le=90)):
    """AI 解读（真实 key，规则兜底）"""
    from . import competitors
    return competitors.commentary(days)


# ---------------- P0：事件聚类（帮手 F：monitor/events.py） ----------------

import threading as _threading

_event_jobs: Dict[str, Dict] = {}


@app.get("/api/events/cluster")
def api_cluster_events(topic_id: Optional[int] = None, days: int = Query(14, ge=1, le=90)):
    """负面事件聚类（即时计算版）：按共享关键词把近期负面帖子聚成事件。

    注意：必须定义在 /api/events/{event_id} 之前，否则 "cluster" 会被误判为 event_id。
    """
    from . import analysis
    return analysis.cluster_events(topic_id, days)


@app.get("/api/events")
def api_list_events(topic_id: Optional[int] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """事件列表（持久化聚类结果）"""
    from . import events
    return events.list_events(topic_id, page, page_size)


@app.get("/api/events/{event_id}")
def api_get_event(event_id: int):
    from . import events
    ev = events.get_event(event_id)
    if not ev:
        raise HTTPException(404, "事件不存在")
    return ev


@app.post("/api/events/generate")
def api_generate_events(body: dict):
    """手动触发事件聚类（后台线程，结果可通过 GET /api/events 查看）"""
    from . import events
    topic_id = (body or {}).get("topic_id")
    days = (body or {}).get("days", 14)
    if not topic_id:
        raise HTTPException(400, "缺少 topic_id")
    job_id = f"ev_{topic_id}_{int(time.time())}"
    _event_jobs[job_id] = {"status": "running", "started_at": int(time.time())}

    def _run():
        try:
            res = events.generate_events(int(topic_id), int(days))
            _event_jobs[job_id].update({"status": "done", **res})
        except Exception as e:  # noqa: BLE001
            _event_jobs[job_id].update({"status": "failed", "error": str(e)})

    _threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "message": "事件聚类已在后台开始，完成后可在事件列表查看"}


@app.get("/api/events/jobs/{job_id}")
def api_event_job(job_id: str):
    job = _event_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.delete("/api/events/{event_id}")
def api_delete_event(event_id: int):
    from . import events
    if not events.delete_event(event_id):
        raise HTTPException(404, "事件不存在")
    return {"ok": True}


@app.get("/api/events/{event_id}/propagation")
def api_event_propagation(event_id: int):
    """P0：传播路径图谱（三层：趋势/关键节点/画像+图谱）。返回事件下帖子的传播关系有向图。"""
    from . import propagation
    return propagation.build_propagation(event_id)


@app.get("/api/events/{event_id}/propagation/timeline")
def api_event_propagation_timeline(event_id: int, granularity: str = "day"):
    """P0：传播趋势时间线。granularity=day|hour。"""
    from . import propagation
    return propagation.get_timeline(event_id, granularity)


# ---------------- P0：达人库（monitor/influencers.py） ----------------


@app.get("/api/influencers")
def api_list_influencers(q: Optional[str] = None,
                         domain: Optional[str] = None,
                         coop_status: Optional[str] = None,
                         min_fans: int = Query(0, ge=0),
                         page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=100)):
    from . import influencers
    return influencers.list_influencers(q, domain, coop_status, min_fans, page, page_size)


@app.get("/api/influencers/domains")
def api_influencer_domains():
    from . import influencers
    return {"items": influencers.domain_stats()}


@app.get("/api/influencers/{author_id}")
def api_get_influencer(author_id: str):
    from . import influencers
    r = influencers.get_influencer(author_id)
    if not r:
        raise HTTPException(404, "达人不存在")
    return r


class InfluencerIn(BaseModel):
    author_id: str
    nickname: str = ""
    fans_count: int = 0
    liked_count: int = 0
    notes_count: int = 0
    ip_location: str = ""
    domain_tags: List[str] = []
    coop_status: str = "none"
    coop_notes: str = ""
    xiaohongshu_id: str = ""


class InfluencerPatch(BaseModel):
    nickname: Optional[str] = None
    fans_count: Optional[int] = None
    liked_count: Optional[int] = None
    notes_count: Optional[int] = None
    ip_location: Optional[str] = None
    domain_tags: Optional[List[str]] = None
    coop_status: Optional[str] = None
    coop_notes: Optional[str] = None
    xiaohongshu_id: Optional[str] = None


@app.post("/api/influencers")
def api_add_influencer(body: InfluencerIn):
    from . import influencers
    try:
        return influencers.add_influencer(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/influencers/{author_id}")
def api_update_influencer(author_id: str, body: InfluencerPatch):
    from . import influencers
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        r = influencers.update_influencer(author_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not r:
        raise HTTPException(404, "达人不存在")
    return r


@app.delete("/api/influencers/{author_id}")
def api_delete_influencer(author_id: str):
    from . import influencers
    if not influencers.delete_influencer(author_id):
        raise HTTPException(404, "达人不存在")
    return {"ok": True}


@app.get("/api/influencers/{author_id}/similar")
def api_similar_influencers(author_id: str,
                            limit: int = Query(10, ge=1, le=30)):
    """相似达人推荐（灰豚逆向：/anchor/detail/likeAnchors）。"""
    from . import influencers
    return {"items": influencers.similar_influencers(author_id, limit)}


@app.get("/api/hotwords/related-brands")
def api_hotword_related_brands(topic_id: Optional[int] = None,
                               word: str = Query(..., min_length=1),
                               days: int = Query(30, ge=1, le=90)):
    """热词关联品牌（灰豚逆向：/hotword/detail/relatedBrand）。"""
    from . import trends
    return {"word": word, "items": trends.related_brands(topic_id, word, days)}


class SensitiveCheckIn(BaseModel):
    text: str = ""


@app.post("/api/sensitive/check")
def api_sensitive_check(body: SensitiveCheckIn):
    """敏感词检测（灰豚逆向：/tools/sensitive_words）。"""
    from . import sensitive
    return sensitive.check(body.text)


# ---------------- 竞品对比（轻量版，analysis.py） ----------------


@app.get("/api/compare")
def api_compare_topics(a: int, b: int, days: int = Query(7, ge=1, le=90)):
    """竞品对标：两个话题声量/情感/负面率并排对比"""
    from . import analysis
    r = analysis.compare_topics(a, b, days)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error"))
    return r


# ---------------- AI ----------------

@app.get("/api/ai-config")
def api_get_ai_config():
    return public_ai_config()


@app.put("/api/ai-config")
def api_put_ai_config(body: AIConfigIn):
    save_ai_config(body.active, body.providers)
    ai_chat._mode_cache["ts"] = 0  # 强制重新检测
    return public_ai_config()


@app.post("/api/ai-config/test")
def api_test_ai(body: AITestIn = AITestIn()):
    """测试连接（深度测试：鉴权 + 真实生成，给出诚实结论）"""
    from .config import get_active_provider
    name = body.provider or get_active_provider()["name"]
    cfg = get_ai_config()
    if name not in cfg["providers"]:
        raise HTTPException(400, f"未知 provider {name}")
    p = cfg["providers"][name]
    t0 = time.time()
    ok, msg = ai_chat.test_provider_deep(name)
    return {"ok": ok, "message": msg,
            "latency_ms": int((time.time() - t0) * 1000),
            "provider": name, "model": p["model"]}


@app.get("/api/chat/mode")
def api_chat_mode():
    return ai_chat.current_mode()


@app.get("/api/ai/models")
def api_ai_models(provider: Optional[str] = None):
    """模型列表：优先从 provider 的 /v1/models 拉取，失败则返回预设列表。

    ChatGPT 同款模型选择器用：{provider, models[], current, source}
    """
    from .config import PRESET_MODELS, get_active_provider, get_ai_config
    from .netutil import http_client
    name = provider or get_active_provider()["name"]
    cfg = get_ai_config()
    if name not in cfg["providers"]:
        raise HTTPException(400, f"未知 provider {name}")
    p = cfg["providers"][name]
    presets = PRESET_MODELS.get(name, [])
    models, source = list(presets), "preset"
    if p["base_url"] and p["api_key"] and not _is_placeholder(p["base_url"]):
        try:
            with http_client(15.0) as c:
                r = c.get(p["base_url"].rstrip("/") + "/models",
                          headers={"Authorization": f"Bearer {p['api_key']}"})
                if r.status_code < 400:
                    ids = [m.get("id") for m in r.json().get("data", [])
                           if m.get("id")]
                    if ids:
                        models, source = ids, "remote"
        except Exception:
            pass
    # 当前选中的模型一定在列表里（没有就补到首位）
    if p["model"] and p["model"] not in models:
        models.insert(0, p["model"])
    return {"provider": name, "models": models, "current": p["model"],
            "source": source}


@app.get("/api/chat/chips")
def api_chat_chips():
    """AI 聊天快捷指令（从话题动态生成，通用化）"""
    from . import ai_chat
    return {"chips": ai_chat._build_chips()}


@app.post("/api/chat")
def api_chat(body: ChatIn):
    return ai_chat.chat(body.message, body.history,
                        confirmation_id=body.confirmation_id,
                        confirmed=body.confirmed)


@app.post("/api/chat/stream")
def api_chat_stream(body: ChatIn):
    """SSE 流式聊天：首 token 90ms 级渲染。

    复用现有 chat() 完整逻辑（含工具调用/确认流），生成完整回复后
    按 chunk 通过 SSE 推送，前端逐 chunk 即时渲染。
    事件：data: {"type":"token","text":"..."} ... data: {"type":"done","payload":{...}}
    """
    import json

    def gen():
        try:
            res = ai_chat.chat(body.message, body.history,
                               confirmation_id=body.confirmation_id,
                               confirmed=body.confirmed)
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
            return
        reply = res.get("reply") or ""
        # 首个事件立即发出（<90ms 感知）
        yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"
        # 按 60 字符切片推送，模拟流式
        chunk_size = 60
        for i in range(0, len(reply), chunk_size):
            chunk = reply[i:i + chunk_size]
            yield f"data: {json.dumps({'type': 'token', 'text': chunk}, ensure_ascii=False)}\n\n"
        # 最终 payload（含 actions/notice/pending_confirmation 等完整结构）
        payload = {k: v for k, v in res.items() if k != "reply"}
        yield f"data: {json.dumps({'type': 'done', 'payload': payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})


# ---------------- xhs 账号管理（多账号，站内扫码） ----------------

@app.get("/api/accounts")
def api_list_accounts():
    """账号列表（不含加密登录数据）"""
    return {"items": db.list_accounts(),
            "default_id": (db.get_default_account() or {}).get("id")}


@app.post("/api/accounts")
def api_create_account():
    """添加账号：创建记录并启动站内扫码（前端弹窗展示二维码）"""
    from .login_flow import get_manager
    account_id = db.create_account()
    sess = get_manager().start(account_id)
    return {"account_id": account_id, "login": sess}


@app.delete("/api/accounts/{account_id}")
def api_delete_account(account_id: int):
    from .login_flow import get_manager
    get_manager().cancel(account_id)
    if not db.delete_account(account_id):
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/rescan")
def api_rescan_account(account_id: int):
    """重新扫码（登录失效时一键重登）"""
    from .login_flow import get_manager
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    db.update_account(account_id, {"status": "pending", "check_note": "等待重新扫码"})
    sess = get_manager().start(account_id)
    return {"account_id": account_id, "login": sess}


@app.post("/api/accounts/{account_id}/check")
def api_check_account(account_id: int):
    """检查账号登录态是否有效"""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    from .login_flow import account_health
    health = account_health(acc)
    db.update_account(account_id, {
        "status": health["status"],
        "last_check_at": int(__import__("time").time()),
        "check_note": health["message"][:180],
    })
    return health


@app.get("/api/login/qrcode")
def api_login_qrcode(account_id: int):
    """获取扫码登录二维码（base64，前端弹窗展示）"""
    from .login_flow import get_manager
    b64 = get_manager().qrcode(account_id)
    if not b64:
        raise HTTPException(404, "二维码尚未生成或已过期，请重新发起登录")
    return {"image": f"data:image/png;base64,{b64}"}


class BrowserImportIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@app.get("/api/accounts/browser-available")
def api_browser_available():
    """检测本地浏览器中的小红书登录态。

    仅在用户点击"从浏览器导入"时触发；只返回昵称/头像，不暴露 Cookie。
    未经用户点击，后端绝不自动扫描。"""
    from . import browser_import
    try:
        items = browser_import.scan_browser_logins()
    except browser_import.BrowserImportError as e:
        raise HTTPException(400, str(e))
    return {"items": items}


@app.post("/api/accounts/browser-import")
def api_browser_import(body: BrowserImportIn):
    """用户在检测列表中选择账号并二次确认后，导入浏览器登录态（加密入库）。"""
    from . import browser_import
    try:
        return browser_import.import_login(body.key)
    except browser_import.BrowserImportError as e:
        raise HTTPException(400, str(e))


@app.get("/api/login/status")
def api_login_status(account_id: int):
    """轮询扫码登录状态"""
    from .login_flow import get_manager
    return get_manager().status(account_id)


@app.post("/api/login/cancel")
def api_login_cancel(account_id: int):
    from .login_flow import get_manager
    return get_manager().cancel(account_id)


# ---------------- login status ----------------

@app.get("/api/accounts/cookie-handshake")
def api_cookie_handshake_status():
    """是否允许用户主动提交本人 Cookie。默认关闭，不返回任何 Cookie。"""
    from .cookie_handshake import status
    return status()


@app.post("/api/accounts/cookie-handshake")
async def api_cookie_handshake(request: Request):
    """保存用户本人确认发送的 Cookie。需要已登录会话和 CSRF。响应不回显 Cookie。"""
    import json
    from .cookie_handshake import HandshakeDisabled, HandshakeRejected, accept_user_cookies
    _require_csrf(request)
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(400, "请求格式不正确。")
    if len(raw) > 65536:
        raise HTTPException(400, "提交内容过大。请只发送登录所需的 Cookie。")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        raise HTTPException(400, "请求格式不正确。")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求格式不正确。")
    extra = body.get("cookies") if isinstance(body.get("cookies"), dict) else {}
    try:
        account_id = body.get("account_id")
        if account_id is not None and account_id != "":
            account_id = int(account_id)
        else:
            account_id = None
    except (TypeError, ValueError):
        raise HTTPException(400, "账号编号不正确。")
    try:
        return accept_user_cookies(
            a1=str(body.get("a1") or ""),
            web_session=str(body.get("web_session") or ""),
            nickname=str(body.get("nickname") or ""),
            account_id=account_id,
            xhs_user_id=str(body.get("xhs_user_id") or ""),
            extra_cookies=extra,
        )
    except HandshakeDisabled as e:
        raise HTTPException(404, str(e))
    except HandshakeRejected as e:
        raise HTTPException(400, str(e))


class BrowserUseJobIn(BaseModel):
    topic_id: int
    max_notes: Optional[int] = Field(default=None, ge=1, le=500)
    enable_comments: Optional[bool] = None


@app.get("/api/browser-use/status")
def api_browser_use_status():
    """是否允许复用你已经登录的 profile。不返回 Cookie。"""
    from .browser_use_worker import status
    return status()


@app.post("/api/browser-use/jobs")
def api_browser_use_job(body: BrowserUseJobIn):
    """用你指定目录里已经登录的会话排队采集。未开启或未登录则拒绝。"""
    from .browser_use_worker import BrowserUseDisabled, BrowserUseError, ensure_ready
    try:
        ensure_ready()
    except BrowserUseDisabled as e:
        raise HTTPException(404, str(e))
    except BrowserUseError as e:
        raise HTTPException(400, str(e))
    if db.running_task_exists() or crawler_driver.is_running():
        raise HTTPException(409, "已有采集任务在运行，请稍后再试")
    topic = db.get_topic(body.topic_id)
    if not topic:
        raise HTTPException(404, "话题不存在")
    try:
        task_id = crawler_driver.trigger_crawl(
            body.topic_id, max_notes=body.max_notes,
            enable_comments=body.enable_comments,
            use_browser_profile=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "task_id": task_id,
        "message": "已加入队列。将使用你指定的、已经登录的浏览器会话，不会代为登录。",
    }


@app.get("/api/login-status")
def api_login_status():
    """检测各引擎登录态"""
    engines = []
    for a in list_adapters():
        ok, hint = a.check_login()
        engines.append({"name": a.name, "title": a.title,
                        "login_ok": ok, "login_hint": hint})
    accounts = db.list_accounts()
    valid = [a for a in accounts if a.get("status") == "valid"]
    return {
        "engines": engines,
        "accounts": accounts,
        "default_account_id": (db.next_collection_account() or {}).get("id"),
        "needs_real_xhs_session": not bool(valid),
        "message": (
            "已有可采集的小红书登录态。" if valid else
            "需要真实小红书登录态：请用本人账号扫码。服务端不能代替这一步。"
        ),
        # 账号安全建议（用户已指定香港号为采集账号）
        "account_advice": (
            "建议使用专用采集账号（如你的香港号），不要用运营主号登录采集，"
            "避免风控影响正常业务。强攻模式会提高请求频率，可能触发小红书"
            "风控导致账号受限，请勿对主力账号使用强攻模式。"
        ),
    }


# ---------------- frontend ----------------

# 旧版单页仪表盘保留在 /static（备用）
# ---------------- V1.1：伪冒账号反查（monitor/fakecheck.py，自包含） ----------------
# 接线清单见 monitor/fakecheck.py 底部 WIRING。ValueError → 400（中文提示直接透出）。

def _fc():
    from . import fakecheck
    return fakecheck


@app.get("/api/fakecheck/brands")
def api_fc_list_brands():
    return {"items": _fc().list_brands()}


@app.post("/api/fakecheck/brands")
def api_fc_create_brand(body: dict):
    b = body or {}
    if not (b.get("name") or "").strip():
        raise HTTPException(400, "品牌名称不能为空")
    try:
        bid = _fc().create_brand(b["name"].strip(), str(b.get("keywords") or ""),
                                 str(b.get("remark") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _fc().get_brand(bid)


@app.put("/api/fakecheck/brands/{bid}")
def api_fc_update_brand(bid: int, body: dict):
    try:
        return _fc().update_brand(bid, body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/fakecheck/brands/{bid}")
def api_fc_delete_brand(bid: int):
    if not _fc().delete_brand(bid):
        raise HTTPException(404, "品牌不存在")
    return {"ok": True}


@app.get("/api/fakecheck/brands/{bid}/features")
def api_fc_list_features(bid: int, kind: str = Query("")):
    return {"items": _fc().list_features(bid, kind)}


@app.post("/api/fakecheck/brands/{bid}/features")
def api_fc_save_feature(bid: int, body: dict):
    b = body or {}
    try:
        return _fc().save_feature(bid, str(b.get("kind") or ""),
                                  str(b.get("value") or ""),
                                  int(b.get("weight") or 0))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/fakecheck/features/{fid}")
def api_fc_update_feature(fid: int, body: dict):
    try:
        return _fc().update_feature(fid, body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/fakecheck/features/{fid}")
def api_fc_delete_feature(fid: int):
    if not _fc().delete_feature(fid):
        raise HTTPException(404, "特征不存在")
    return {"ok": True}


@app.get("/api/fakecheck/brands/{bid}/suspects")
def api_fc_list_suspects(bid: int, level: str = Query(""),
                         verified: str = Query(""), source: str = Query(""),
                         keyword: str = Query(""),
                         limit: int = Query(50, ge=1, le=200),
                         offset: int = Query(0, ge=0)):
    return _fc().list_suspects(bid, level, verified, source, keyword, limit, offset)


@app.post("/api/fakecheck/brands/{bid}/suspects")
def api_fc_create_suspect(bid: int, body: dict):
    try:
        return _fc().create_suspect(bid, body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fakecheck/suspects/{sid}")
def api_fc_get_suspect(sid: int):
    s = _fc().get_suspect(sid)
    if not s:
        raise HTTPException(404, "嫌疑账号不存在")
    return s


@app.put("/api/fakecheck/suspects/{sid}")
def api_fc_update_suspect(sid: int, body: dict):
    try:
        return _fc().update_suspect(sid, body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/fakecheck/suspects/{sid}")
def api_fc_delete_suspect(sid: int):
    if not _fc().delete_suspect(sid):
        raise HTTPException(404, "嫌疑账号不存在")
    return {"ok": True}


@app.get("/api/fakecheck/brands/{bid}/stats")
def api_fc_suspect_stats(bid: int):
    return _fc().suspect_stats(bid)


@app.post("/api/fakecheck/brands/{bid}/import")
async def api_fc_import_excel(bid: int, file: UploadFile = File(...)):
    """名单 Excel 导入（文件仅作临时解析，不入库原文件）"""
    import tempfile
    safe = Path(file.filename or "import.xlsx").name
    if not safe.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "请上传 .xlsx 格式文件")
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        tmp.write(await file.read())
        tmp.close()
        try:
            return _fc().import_excel(bid, tmp.name)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.get("/api/fakecheck/brands/{bid}/import-template")
def api_fc_import_template(bid: int):
    data = _fc().import_template()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _content_disposition("伪冒名单导入模板.xlsx")})


@app.get("/api/fakecheck/brands/{bid}/export.csv")
def api_fc_export_csv(bid: int, level: str = Query(""), verified: str = Query("")):
    data = _fc().export_csv(bid, level, verified)
    return StreamingResponse(
        iter([data]), media_type="text/csv",
        headers={"Content-Disposition": _content_disposition("嫌疑名单.csv")})


@app.get("/api/fakecheck/suspects/{sid}/material")
def api_fc_complaint_material(sid: int):
    return _fc().complaint_material(sid)


@app.get("/api/fakecheck/evidence/{sid}")
def api_fc_evidence_markdown(sid: int):
    """导出 Markdown 格式的举报材料。"""
    from fastapi.responses import PlainTextResponse
    try:
        md = _fc().complaint_material_markdown(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")


@app.get("/api/fakecheck/sweep-login")
def api_fc_sweep_login():
    ok, msg = _fc().check_sweep_login()
    return {"ok": ok, "message": msg}


@app.post("/api/fakecheck/brands/{bid}/sweep")
def api_fc_start_sweep(bid: int, body: dict = None):
    b = body or {}
    try:
        tid = _fc().start_sweep(
            bid,
            keywords=b.get("keywords"),
            max_notes=int(b.get("max_notes") or 30),
            account_id=b.get("account_id"),
            name=str(b.get("name") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"task_id": tid}


@app.get("/api/fakecheck/brands/{bid}/sweeps")
def api_fc_list_sweeps(bid: int):
    return {"items": _fc().list_sweep_tasks(bid)}


@app.get("/api/fakecheck/sweeps/{tid}")
def api_fc_get_sweep(tid: int):
    t = _fc().get_sweep_task(tid)
    if not t:
        raise HTTPException(404, "Sweep 任务不存在")
    return t


@app.get("/api/fakecheck/brands/{bid}/clusters")
def api_fc_clusters(bid: int):
    return _fc().cluster_subjects(bid)


@app.get("/api/fakecheck/brands/{bid}/gangs")
def api_fc_gangs(bid: int, sim_threshold: float = Query(0.8, ge=0.5, le=1.0),
                 batch_days: int = Query(7, ge=1, le=30)):
    return _fc().analyze_gangs(bid, sim_threshold, batch_days)


@app.get("/api/fakecheck/brands/{bid}/gangs/report")
def api_fc_gang_report(bid: int, gang_key: str = Query(...)):
    try:
        return _fc().gang_report(bid, gang_key)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fakecheck/qcc-lookup")
def api_fc_qcc_lookup(name: str = Query(...)):
    return _fc().qcc_lookup(name)


@app.post("/api/fakecheck/brands/{bid}/batches")
def api_fc_create_batch(bid: int, body: dict):
    b = body or {}
    try:
        return _fc().create_batch(bid, str(b.get("name") or ""),
                                 list(b.get("suspect_ids") or []),
                                 int(b.get("batch_size") or 5),
                                 int(b.get("interval_days") or 3))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fakecheck/brands/{bid}/batches")
def api_fc_list_batches(bid: int):
    return {"items": _fc().list_batches(bid)}


@app.get("/api/fakecheck/batches/{bid}")
def api_fc_get_batch(bid: int):
    b = _fc().get_batch(bid)
    if not b:
        raise HTTPException(404, "投诉批次不存在")
    return b


@app.put("/api/fakecheck/batch-items/{iid}")
def api_fc_update_batch_item(iid: int, body: dict):
    b = body or {}
    try:
        return _fc().update_batch_item(iid, str(b.get("status") or ""),
                                       str(b.get("note") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/fakecheck/batches/{bid}")
def api_fc_delete_batch(bid: int):
    if not _fc().delete_batch(bid):
        raise HTTPException(404, "投诉批次不存在")
    return {"ok": True}


@app.get("/api/fakecheck/rights-guide")
def api_fc_rights_guide():
    return {"steps": _fc().get_rights_guide()}


@app.get("/api/fakecheck/brands/{bid}/rights-holder")
def api_fc_get_rights_holder(bid: int):
    return _fc().get_rights_holder(bid)


@app.post("/api/fakecheck/brands/{bid}/rights-holder")
def api_fc_save_rights_holder(bid: int, body: dict):
    try:
        return _fc().save_rights_holder(bid, body or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/fakecheck/brands/{bid}/trademark-cert")
async def api_fc_save_trademark_cert(bid: int, file: UploadFile = File(...)):
    """商标证明上传（PDF/PNG/JPG，仅存本地 data/fakecheck/certs/）"""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False,
                                      suffix=Path(file.filename or "").suffix)
    try:
        tmp.write(await file.read())
        tmp.close()
        try:
            return _fc().save_trademark_cert(bid, tmp.name, file.filename or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@app.get("/api/fakecheck/brands/{bid}/trademark-cert")
def api_fc_get_trademark_cert(bid: int):
    return _fc().get_trademark_cert(bid)


@app.delete("/api/fakecheck/brands/{bid}/trademark-cert")
def api_fc_delete_trademark_cert(bid: int):
    if not _fc().delete_trademark_cert(bid):
        raise HTTPException(404, "商标证明不存在")
    return {"ok": True}


@app.get("/api/fakecheck/batches/{bid}/package")
def api_fc_complaint_package(bid: int):
    data, fname = _fc().export_complaint_package(bid)
    return StreamingResponse(
        iter([data]), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(fname)})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")),
              name="frontend-assets")

# 前端生产构建 base=/v3-admin-vite/（见 frontend/.env.production），挂载对应路径
# 否则 index.html 引用的 /v3-admin-vite/assets/*.js 404，页面空白
if FRONTEND_DIST.exists():
    app.mount("/v3-admin-vite", StaticFiles(directory=str(FRONTEND_DIST)),
              name="frontend-dist")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    dist_index = FRONTEND_DIST / "index.html"
    if dist_index.exists():
        return HTMLResponse(dist_index.read_text(encoding="utf-8"))
    legacy = STATIC_DIR / "index.html"
    if legacy.exists():
        return HTMLResponse(legacy.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>前端尚未构建</h1>", status_code=500)


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str):
    """Vue history 路由回退：非 /api 路径返回前端 index.html"""
    if path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    dist_index = FRONTEND_DIST / "index.html"
    if dist_index.exists():
        return HTMLResponse(dist_index.read_text(encoding="utf-8"))
    raise HTTPException(404, "页面不存在")


def main() -> None:
    import uvicorn
    host, port = bind_host(), bind_port()
    logger.info("Cortech Chat 监听 %s:%s 数据目录 %s", host, port, DATA_DIR)
    uvicorn.run("monitor.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
