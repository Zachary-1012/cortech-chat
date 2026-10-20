# -*- coding: utf-8 -*-
"""Chat Cortech · 一键全链路验证（synthetic 数据 + 真 key AI）。

用法：cd ~/workspace/xhs-monitor && .venv/bin/python -m monitor.verify_all
真实小红书采集需扫码登录，不在自动验证范围内（留给用户操作）。
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main():
    from monitor import ai_chat, bid, creator, db, reports, trends
    from monitor.adapters import adapter_names
    from monitor.adapters.cortech_direct import DirectAPIAdapter, DirectAPIFailed
    from monitor.adapters.base import AuthorData, CrawlResult, NoteData
    from monitor.adapters.cortech_browser import _to_ts
    from monitor.news import base as news_base
    from monitor.news.base import NewsItem

    TZ = timezone(timedelta(hours=8))
    db.init_db(); db.seed_topics(); db.seed_news_sources()

    # ---- 1. 引擎注册 ----
    names = adapter_names()
    check("引擎注册", {"cortech_direct", "cortech_browser", "cortech_spider", "cortech_downloader"} <= set(names), str(names))
    news_base.import_all()
    check("资讯采集器注册", set(news_base.available()) == {"googlenews_rss", "rss"})

    # ---- 2. 时间铁律 ----
    tid = db.list_topics()[0]["id"]
    ago = datetime.now(TZ) - timedelta(days=3)
    ms = int(ago.timestamp() * 1000)
    n = NoteData(post_id="verify_time_1", title="时间验证", author=AuthorData(nickname="t"),
                 publish_time=_to_ts(ms), publish_time_raw=str(ms))
    db.upsert_note({"post_id": n.post_id, "topic_id": tid, "title": n.title, "author": "t",
                    "publish_time": n.publish_time, "publish_time_raw": n.publish_time_raw,
                    "collected_at": int(time.time())})
    vol = {d["period"]: d["count"] for d in trends.volume_trend(tid, days=7)}
    check("发布时间落在3天前柱子", vol.get(ago.strftime("%Y-%m-%d"), 0) >= 1)
    check("坏时间戳→NULL不冒充", _to_ts("not-a-time") is None)
    with db._connect() as c:
        c.execute("DELETE FROM notes WHERE post_id='verify_time_1'")

    # ---- 3. 情感 ----
    from monitor.sentiment import analyze
    cases = [("太古汇服务太差，排队两小时，避雷！", "negative"),
             ("十五周年抽中购物卡，太开心了", "positive"),
             ("今天去了太古汇", "neutral")]
    ok = all(analyze(t)[0] == exp for t, exp in cases)
    check("情感判定", ok)

    # ---- 4. direct-api 映射 + 降级 ----
    card = {"post_id": "v1", "title": "t", "user": {"nickname": "u"},
            "interact_info": {"liked_count": "5"}, "time": ms, "tag_list": [{"name": "广州太古汇"}]}
    m = DirectAPIAdapter._map_note(card, "v1", "tok", "pc_search", "kw")
    check("direct-api 映射", m.publish_time_raw == str(ms) and m.tags == ["广州太古汇"])
    check("分享数缺字段时标记 not_on_web",
          m.share_count_available is False
          and (m.field_meta.get("share_count") or {}).get("source") == "not_on_web")
    check("DirectAPIFailed 可捕获", issubclass(DirectAPIFailed, Exception))

    # ---- 4b. SaaS 运行时 / 采集语义 / 样例闭环 ----
    import os
    from monitor.config import bind_host, resolve_data_dir
    from monitor.setup_status import build_setup_status
    from monitor.xhs_errors import classify_biz_code
    from monitor.collection_fixture import ingest_fixture
    from monitor import alerts

    saved_host = os.environ.pop("CORTECH_HOST", None)
    saved_xhs_host = os.environ.pop("XHS_HOST", None)
    try:
        check("默认监听 0.0.0.0", bind_host() == "0.0.0.0")
    finally:
        if saved_host is not None:
            os.environ["CORTECH_HOST"] = saved_host
        if saved_xhs_host is not None:
            os.environ["XHS_HOST"] = saved_xhs_host
    os.environ["CORTECH_DATA_DIR"] = "/tmp/cortech-data-dir-check"
    check("数据目录读 CORTECH_DATA_DIR", str(resolve_data_dir()) == "/tmp/cortech-data-dir-check")
    os.environ.pop("CORTECH_DATA_DIR", None)
    try:
        import bcrypt  # noqa: F401
        bcrypt_ok = True
    except ImportError:
        bcrypt_ok = False
    check("bcrypt 可导入", bcrypt_ok)
    status = build_setup_status(None)
    ids = {c["id"] for c in status["checks"]}
    check("setup status 结构",
          status.get("product") == "Cortech Chat"
          and {"db", "data_dir", "auth", "xhs_account"} <= ids
          and status["collection"]["needs_real_xhs_session"] is True)
    kind, human = classify_biz_code(-3, "")
    check("code=-3 视为删除", kind == "deleted" and "不计入采集失败" in human)
    kind_login, _ = classify_biz_code(-101, "")
    check("code=-101 视为登录失效", kind_login == "login")

    alerts.init_db()
    with alerts._connect() as conn:
        alert_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    check("预警列是 post_ids_json",
          "post_ids_json" in alert_cols and "note_ids_json" not in alert_cols)

    acc_old = db.create_account("轮询-旧")
    acc_new = db.create_account("轮询-新")
    db.update_account(acc_old, {"status": "valid", "last_used_at": 10})
    db.update_account(acc_new, {"status": "valid", "last_used_at": 50})
    ordered = [a["id"] for a in db.list_valid_accounts_lru()]
    check("多账号轮询按最久未用排序",
          acc_old in ordered and acc_new in ordered
          and ordered.index(acc_old) < ordered.index(acc_new))
    db.delete_account(acc_old)
    db.delete_account(acc_new)

    saved_fixture = os.environ.pop("CORTECH_COLLECTION_FIXTURE", None)
    try:
        crawler_mod = __import__("monitor.crawler_driver", fromlist=["trigger_crawl"])
        crawler_mod.trigger_crawl(tid, account_id=10**9)
        check("无登录态时拒绝采集", False)
    except ValueError as e:
        check("无登录态时拒绝采集", "need_xhs_session" in str(e) or "need_xhs_rescan" in str(e) or "登录" in str(e))
    finally:
        if saved_fixture is not None:
            os.environ["CORTECH_COLLECTION_FIXTURE"] = saved_fixture

    fx = ingest_fixture(tid, "闭环样例")
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT post_id, publish_time, collected_at, post_status, field_meta"
            " FROM notes WHERE post_id LIKE 'fixture_%'"
        ).fetchall()
    live = [r for r in rows if r["post_status"] != "deleted" and r["publish_time"]]
    deleted = [r for r in rows if r["post_status"] == "deleted"]
    check("样例采集发布时间不等于采集时间",
          bool(live) and all(r["publish_time"] != r["collected_at"] for r in live),
          f"added={fx['notes']}")
    check("删除笔记单独标记", bool(deleted))
    with db._connect() as conn:
        conn.execute("DELETE FROM notes WHERE post_id LIKE 'fixture_%'")

    # ---- 4c. 本人握手 / 轮换 v2 / 已登录浏览器会话 / 插件形态 ----
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from monitor.account_crypto import decrypt_login_data, encrypt_login_data
    from monitor.account_rotation import beijing_day, block_reason, eligible_accounts
    from monitor.browser_use_worker import (BrowserUseRejected, can_use, reset_for_tests,
                                           session_scope, status as bu_status)
    from monitor.config import cookie_handshake_enabled
    from monitor.cookie_handshake import (HandshakeDisabled, HandshakeRejected,
                                          accept_user_cookies)

    _flag_names = ("CORTECH_SAAS", "CORTECH_ENABLE_COOKIE_HANDSHAKE",
                   "CORTECH_ENABLE_BROWSER_USE", "CORTECH_BROWSER_USE_PROFILE",
                   "CORTECH_BROWSER_USE_CONCURRENCY", "CORTECH_BROWSER_USE_QUEUE")
    _saved_flags = {key: os.environ.pop(key, None) for key in _flag_names}
    good_id = bad_id = full_id = None
    try:
        check("本地默认关闭握手", cookie_handshake_enabled() is False)
        os.environ["CORTECH_SAAS"] = "1"
        check("SaaS 默认开启握手", cookie_handshake_enabled() is True)
        os.environ["CORTECH_ENABLE_COOKIE_HANDSHAKE"] = "0"
        check("显式 0 关闭握手", cookie_handshake_enabled() is False)
        try:
            accept_user_cookies(a1="12345678", web_session="12345678")
            check("关闭时拒绝握手", False)
        except HandshakeDisabled as e:
            check("关闭时拒绝握手", "未开启" in str(e))
        os.environ["CORTECH_ENABLE_COOKIE_HANDSHAKE"] = "1"
        try:
            accept_user_cookies(a1="short", web_session="also-short")
            check("过短 Cookie 被拒绝", False)
        except HandshakeRejected as e:
            check("过短 Cookie 被拒绝", "a1" in str(e) and "short" not in str(e))

        secret_a1 = "handshakeA1Value99"
        secret_web = "handshakeWebSession99"
        res = accept_user_cookies(
            a1=secret_a1, web_session=secret_web, nickname="插件用户",
            xhs_user_id="p1-xhs-user",
            extra_cookies={"webId": "webidvalue99", "not_allowed": "nope"},
        )
        dumped = _json.dumps(res, ensure_ascii=False)
        check("握手成功且中文说明",
              res.get("ok") is True and "加密" in res.get("message", "")
              and "健康检查" in res["message"])
        check("握手响应不含 Cookie",
              secret_a1 not in dumped and secret_web not in dumped and "webidvalue99" not in dumped)
        good_id = res["account_id"]
        plain = decrypt_login_data(db.get_account(good_id)["login_data_enc"])
        names = {c["name"] for c in _json.loads(plain)["cookies"]}
        check("握手加密入库且丢弃未知 Cookie",
              {"a1", "web_session", "webId"} <= names and "not_allowed" not in names)
        check("握手后健康检查为有效", db.get_account(good_id)["status"] == "valid")
        res2 = accept_user_cookies(
            a1=secret_a1, web_session=secret_web, nickname="插件用户",
            xhs_user_id="p1-xhs-user")
        same = [a["id"] for a in db.list_accounts() if a.get("xhs_user_id") == "p1-xhs-user"]
        check("同一小红书用户更新而不是新建",
              res2["updated"] is True and res2["account_id"] == good_id and same == [good_id])

        email = f"p1-csrf-{int(time.time())}@example.com"
        with db._lock, db._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                (email, "not-a-login", int(time.time())))
            csrf_uid = cur.lastrowid
        token = db.create_auth_session(csrf_uid)
        csrf = db.issue_csrf(token)
        check("CSRF 可签发并校验",
              bool(csrf) and db.csrf_matches(token, csrf)
              and not db.csrf_matches(token, "not-the-token"))
        db.delete_user_sessions(csrf_uid)
        with db._lock, db._connect() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (csrf_uid,))

        bad_id = db.create_account("不健康")
        db.update_account(bad_id, {"status": "valid"})
        full_id = db.create_account("配额满")
        db.update_account(full_id, {
            "status": "valid",
            "login_data_enc": encrypt_login_data(plain),
            "daily_quota": 1,
            "quota_used": 1,
            "quota_day": beijing_day(),
        })
        eligible = {a["id"] for a in eligible_accounts()}
        bad_reason = block_reason(db.get_account(bad_id))
        full_reason = block_reason(db.get_account(full_id))
        check("轮换跳过不健康和超配额",
              good_id in eligible and bad_id not in eligible and full_id not in eligible
              and bool(bad_reason) and "配额" in full_reason)

        from monitor.adapters.base import AuthorData, NoteData
        from monitor.ingester import store_unified
        store_unified(tid, [NoteData(
            post_id="p1_rot_note", title="轮换记录",
            author=AuthorData(nickname="t"), publish_time=1700000000)],
            [], account_id=good_id)
        cols = db.list_note_collections("p1_rot_note")
        with db._connect() as conn:
            row = conn.execute(
                "SELECT collected_account_id FROM notes WHERE post_id='p1_rot_note'"
            ).fetchone()
        check("记下采集账号",
              bool(cols) and cols[0]["account_id"] == good_id
              and row["collected_account_id"] == good_id)

        os.environ["CORTECH_ENABLE_BROWSER_USE"] = "0"
        reset_for_tests()
        check("浏览器会话默认关闭", bu_status()["enabled"] is False and can_use() is False)
        os.environ["CORTECH_ENABLE_BROWSER_USE"] = "1"
        os.environ.pop("CORTECH_BROWSER_USE_PROFILE", None)
        reset_for_tests()
        check("未指定 profile 时拒绝", can_use() is False)
        tmp = _Path(tempfile.mkdtemp(prefix="cortech_p1_profile_"))
        (tmp / "storage_state.json").write_text(_json.dumps({
            "cookies": [
                {"name": "a1", "value": "profileA1Value99", "domain": ".xiaohongshu.com"},
                {"name": "web_session", "value": "profileWeb99", "domain": ".xiaohongshu.com"},
            ],
            "origins": [],
        }), encoding="utf-8")
        os.environ["CORTECH_BROWSER_USE_PROFILE"] = str(tmp)
        os.environ["CORTECH_BROWSER_USE_CONCURRENCY"] = "1"
        os.environ["CORTECH_BROWSER_USE_QUEUE"] = "0"
        reset_for_tests()
        ready = bu_status()
        check("已登录 profile 可用",
              can_use() is True and ready["profile_ready"] is True
              and "不会代为登录" in ready["message"]
              and "profileA1Value99" not in _json.dumps(ready))
        held = session_scope()
        held_dir = held.__enter__()
        second = None
        try:
            check("会话目录含 storage_state", (_Path(held_dir) / "storage_state.json").is_file())
            try:
                second = session_scope()
                second.__enter__()
                check("并发满且队列为 0 时拒绝", False)
            except BrowserUseRejected:
                check("并发满且队列为 0 时拒绝", True)
                second = None
        finally:
            if second is not None:
                second.__exit__(None, None, None)
            held.__exit__(None, None, None)
        shutil.rmtree(tmp, ignore_errors=True)

        ext = _Path(__file__).resolve().parent.parent / "extensions" / "cortech-xhs-assist"
        manifest = _json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
        popup_js = (ext / "popup.js").read_text(encoding="utf-8")
        popup_html = (ext / "popup.html").read_text(encoding="utf-8")
        check("插件为 MV3 且不注入页面",
              manifest.get("manifest_version") == 3 and "content_scripts" not in manifest
              and "xiaohongshu.hk" in _json.dumps(manifest)
              and manifest.get("name", "").startswith("Cortech Chat"))
        check("插件需确认后才发送且不后台监听",
              "确认发送" in popup_html and "onChanged" not in popup_js
              and "chrome.alarms" not in popup_js and "cortechOrigin" in popup_js)

        import monitor.bid
        import monitor.competitors
        import monitor.creator
        import monitor.fakecheck
        import monitor.influencers
        check("运营与营销模块仍在", all((
            monitor.fakecheck, monitor.creator, monitor.bid,
            monitor.competitors, monitor.influencers)))
    finally:
        for aid in (good_id, bad_id, full_id):
            if aid:
                db.delete_account(aid)
        with db._connect() as conn:
            conn.execute("DELETE FROM notes WHERE post_id='p1_rot_note'")
            conn.execute("DELETE FROM note_collections WHERE post_id='p1_rot_note'")
        for key, val in _saved_flags.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        reset_for_tests()

    static_html = ( __import__("pathlib").Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    dist_api = ( __import__("pathlib").Path(__file__).resolve().parent.parent / "frontend" / "dist" / "assets" / "index-VG6IEY1v.js").read_text(encoding="utf-8")
    check("旧 /api/notes 已改到 /api/posts", "/api/notes" not in static_html and 'url:"/notes"' not in dist_api)

    # ---- 5. 趋势/预警/简报/报告 ----
    t = trends.get_trends(tid, 7)
    check("趋势接口", all(k in t for k in ("volume", "sentiment", "hot_words", "spike")))
    b = trends.build_briefing(tid, 7)
    check("简报含资讯节", "news_top3" in b)
    r = reports.build_briefing_report(tid, 7)
    check("YQJB 报告", r["report_no"].startswith("YQJB") and r["product"] == "Chat Cortech")
    r2 = reports.build_special_report(tid, 7)
    check("ZXBG 报告", r2["report_no"].startswith("ZXBG"))

    # ---- 6. 导出 ----
    fname, text = reports.export_notes_csv(tid)
    # 品牌简称可配置（默认「示例」），此处只校验命名规范，不对品牌硬编码
    check("CSV 导出", "发布时间" in text and "采集时间" in text
          and re.fullmatch(r".+舆情_帖子_\d{8}_\d{6}\.csv", fname) is not None, fname)

    # ---- 7. AI 真 key 全链路 ----
    ai_chat._mode_cache["ts"] = 0
    mode, detail = ai_chat.check_ai_ready(force=True)
    check("AI 模式检测", mode == "ai", detail[:50])
    if mode == "ai":
        cr = ai_chat.chat("列出话题")
        check("AI 聊天+工具", cr["mode"] == "ai" and len(cr.get("actions", [])) > 0)
        g = creator.generate("poster_copy", {"theme": "t", "selling_points": "s", "tone": "大气简约"})
        check("AI 创作", g["mode"] == "ai" and len(g["content"]) > 20)
        ex = bid.ai_extract("创意概念：测试；报价：10万元；KPI：曝光100万")
        check("AI 比稿拆解", ex.get("_mode") == "ai" and "10万" in ex.get("price", ""))
    else:
        for nm in ("AI 聊天+工具", "AI 创作", "AI 比稿拆解"):
            check(nm, False, "无可用 provider（网络或 key 问题）")

    # ---- 8. 资讯去重 ----
    from monitor.news import driver as news_driver
    from monitor.news.base import NewsAdapter, register
    class MockNews(NewsAdapter):
        name = "verify_mock"
        def collect(self, keywords, url="", max_items=50):
            return [NewsItem(url="https://e.com/1", title="广州太古汇开业", publish_time=1700000000),
                    NewsItem(url="https://e.com/1", title="广州太古汇开业", publish_time=1700000000),
                    NewsItem(url="https://e.com/2", title="广州太古汇开幕", publish_time=1700000000)]
    register(MockNews())
    news_driver._KIND_TO_ADAPTER["search"] = "verify_mock"
    sid = db.create_news_source("验证源", "search", "广州太古汇", "", "0 3 * * *", 1)
    res = news_driver.trigger_news_collection(sid)
    check("资讯去重", res["added"] == 1 and res["skipped_dup"] == 2, str(res))
    with db._connect() as c:
        c.execute("DELETE FROM news"); c.execute("DELETE FROM tasks WHERE task_type='web-news'")
    db.delete_news_source(sid)

    # ---- 汇总 ----
    fails = [n for n, ok in results if not ok]
    print(f"\n==== {len(results)-len(fails)}/{len(results)} 通过 ====")
    if fails:
        print("失败项:", fails)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
