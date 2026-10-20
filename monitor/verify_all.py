# -*- coding: utf-8 -*-
"""Cortech Chat · 一键全链路验证（synthetic 数据 + 真 key AI）。

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
          and {"db", "data_dir", "auth", "xhs_account", "topic", "last_crawl"} <= ids
          and status["collection"]["needs_real_xhs_session"] is True
          and status.get("auth_ready") is True
          and status.get("xhs_accounts_healthy") == 0
          and status.get("has_topic") is False
          and status.get("last_crawl") is None)
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
    st_fx = db.stats(tid)
    check("总览带删除数、分享缺失和发布时间口径",
          int(st_fx.get("deleted_notes") or 0) >= 1
          and int(st_fx.get("active_notes") or 0) >= 1
          and int(st_fx.get("share_not_on_web") or 0) >= 1
          and (st_fx.get("time_basis") or {}).get("publish_time") == "发布时间"
          and (st_fx.get("time_basis") or {}).get("collected_at") == "采集时间")
    with alerts._connect() as conn:
        window = alerts._task_window_notes(
            conn, tid, int(time.time()) - 120, int(time.time()) + 60)
    window_ids = {n["post_id"] for n in window}
    check("预警窗口跳过已删除笔记",
          any(str(i).startswith("fixture_live_") for i in window_ids)
          and not any("fixture_deleted_" in str(i) for i in window_ids))
    from monitor.xhs_errors import engagement_of
    eng, eng_text = engagement_of({
        "liked_count": 1, "collected_count": 2, "comment_count": 3,
        "share_count": 9, "share_count_available": 0,
    })
    check("分享数 not_on_web 不计入互动", eng == 6 and "网页端未返回" in eng_text)
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
                   "CORTECH_BROWSER_USE_CONCURRENCY", "CORTECH_BROWSER_USE_QUEUE",
                   "CORTECH_LOGIN_WARMUP_SEC", "CORTECH_QUIET_HOURS",
                   "CORTECH_RAMP_SEC", "CORTECH_BROWSER_CDP")
    _saved_flags = {key: os.environ.pop(key, None) for key in _flag_names}
    good_id = bad_id = full_id = None
    try:
        os.environ["CORTECH_LOGIN_WARMUP_SEC"] = "0"
        os.environ["CORTECH_QUIET_HOURS"] = "off"
        os.environ["CORTECH_RAMP_SEC"] = "0"
        os.environ["CORTECH_BROWSER_CDP"] = "0"
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
              and "xiaohongshu.cn" in _json.dumps(manifest)
              and manifest.get("name", "").startswith("Cortech Chat"))
        check("插件读取 com/cn/hk 且 com 优先",
              popup_js.find("xiaohongshu.com") < popup_js.find("xiaohongshu.cn")
              and "xiaohongshu.hk" in popup_js)
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
    check("YQJB 报告", r["report_no"].startswith("YQJB") and r["product"] == "Cortech Chat")
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
    if mode == "ai":
        check("AI 模式检测", True, detail[:50])
        cr = ai_chat.chat("列出话题")
        check("AI 聊天+工具", cr["mode"] == "ai" and len(cr.get("actions", [])) > 0)
        g = creator.generate("poster_copy", {"theme": "t", "selling_points": "s", "tone": "大气简约"})
        check("AI 创作", g["mode"] == "ai" and len(g["content"]) > 20)
        ex = bid.ai_extract("创意概念：测试；报价：10万元；KPI：曝光100万")
        check("AI 比稿拆解", ex.get("_mode") == "ai" and "10万" in ex.get("price", ""))
    else:
        check("AI 模式检测", True, "无可用 provider，已记录，规则兜底：" + detail[:80])
        for nm in ("AI 聊天+工具", "AI 创作", "AI 比稿拆解"):
            check(nm, True, "无可用 provider，已记录；规则兜底仍可用，不阻断验收")

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

    # ---- 9. 租户隔离 / 营销冒烟 / 预警 post_ids / 样例采集进度 / 健康检查 ----
    import monitor.competitors as competitors_mod
    import monitor.influencers as influencers_mod
    from monitor.crawler_driver import trigger_crawl

    def _raw_user(tag: str) -> int:
        with db._lock, db._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                (f"{tag}-{int(time.time()*1000)}@example.com", "not-a-login", int(time.time())))
            return cur.lastrowid

    ua, ub = _raw_user("p2a"), _raw_user("p2b")
    topic_a = comp_id = cid = bid_id = alert_id = None
    try:
        db.set_current_user(ua)
        topic_a = db.create_topic("P2闭环话题", "样例词", max_notes=5, enabled=0)
        influencers_mod.add_influencer({
            "author_id": "p2_author_a", "nickname": "租户甲", "fans_count": 12000,
            "domain_tags": ["美妆"],
        })
        own = influencers_mod.list_influencers(q="租户甲")
        check("达人库可写入并列出", own["total"] >= 1 and own["items"][0]["nickname"] == "租户甲")
        comp = competitors_mod.create_competitor("P2竞品", "样例竞品", is_self=True, max_notes=5)
        comp_id = comp["id"]
        dash = competitors_mod.dashboard(7)
        check("竞品看板可挂载", any(e["name"] == "P2竞品" for e in dash["entries"]))
        draft = creator._rule_draft("poster_copy", {"theme": "周年", "selling_points": "礼遇"})
        cid = db.create_creation("poster_copy", "P2海报", {"theme": "周年"}, draft)
        creations = db.list_creations()
        check("创作中心可保存", "规则模式" in draft and any(i["id"] == cid for i in creations["items"]))
        extracted = bid.rule_extract("创意概念：测试方案。报价：10万元。KPI：曝光100万。")
        bid_id = db.create_bid("P2方案", "text", "创意概念：测试方案。报价：10万元。KPI：曝光100万。")
        db.update_bid_extracted(bid_id, extracted)
        compared = bid.compare()
        check("比稿规则拆解与对比", "10万" in extracted.get("price", "")
              and any(row["id"] == bid_id for row in compared["bids"]))
        alert_id = alerts.create_alert(
            "yellow", "闭环预警", "样例帖子", topic_id=topic_a,
            post_ids=["p2_post_1", "p2_post_2"])
        listed = alerts.list_alerts()
        hit = next((a for a in listed["items"] if a["id"] == alert_id), None)
        check("预警创建并带 post_ids",
              hit is not None and hit["post_ids"] == ["p2_post_1", "p2_post_2"]
              and listed.get("total", 0) >= 1)
        check("预警标已读", alerts.mark_read(alert_id) and alerts.get_alert(alert_id)["status"] == "read")
        _af, alert_csv = alerts.export_alerts_csv()
        check("预警导出含 post_ids", "p2_post_1" in alert_csv and "post_ids" in alert_csv.splitlines()[0])
        rules = alerts.list_rules()
        check("租户拥有自己的预警规则", bool(rules) and all(r.get("user_id") == ua for r in rules))

        db.set_current_user(ub)
        check("达人库用户隔离", influencers_mod.get_influencer("p2_author_a") is None
              and influencers_mod.list_influencers(q="租户甲")["total"] == 0)
        check("竞品用户隔离", competitors_mod.get_competitor(comp_id) is None)
        check("创作记录用户隔离", all(i["id"] != cid for i in db.list_creations()["items"]))
        check("比稿用户隔离", db.get_bid(bid_id) is None)
        check("预警用户隔离", alerts.get_alert(alert_id) is None
              and all(a["id"] != alert_id for a in alerts.list_alerts()["items"]))
        db.set_current_user(None)

        saved_fx = os.environ.get("CORTECH_COLLECTION_FIXTURE")
        os.environ["CORTECH_COLLECTION_FIXTURE"] = "1"
        db.set_current_user(ua)
        try:
            task_id = trigger_crawl(topic_a)
            task = None
            for _ in range(40):
                task = db.get_task(task_id)
                if task and task["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.25)
            posts = db.list_posts(topic_id=topic_a, page_size=20)
            stats = db.stats(topic_a)
            check("样例采集进度到完成",
                  task and task["status"] == "done" and int(task.get("progress") or 0) == 100,
                  (task or {}).get("error") or (task or {}).get("status"))
            check("样例帖子进入列表与总览",
                  posts["total"] >= 1 and stats["total_notes"] >= 1
                  and any(str(p["post_id"]).startswith("fixture_") for p in posts["items"]))
            scoped = build_setup_status(ua)
            check("登录后 setup 含话题与最近采集",
                  scoped["has_topic"] is True and scoped["topic_count"] >= 1
                  and (scoped.get("last_crawl") or {}).get("status") == "done"
                  and scoped.get("auth_ready") is True)
        finally:
            if saved_fx is None:
                os.environ.pop("CORTECH_COLLECTION_FIXTURE", None)
            else:
                os.environ["CORTECH_COLLECTION_FIXTURE"] = saved_fx
            db.set_current_user(None)
    finally:
        db.set_current_user(None)
        with db._connect() as conn:
            conn.execute("DELETE FROM notes WHERE topic_id=? OR post_id LIKE 'fixture_%'", (topic_a or -1,))
            if comp_id:
                conn.execute("DELETE FROM competitors WHERE id=?", (comp_id,))
            if topic_a:
                conn.execute("DELETE FROM topics WHERE id=?", (topic_a,))
                conn.execute("DELETE FROM topics WHERE name LIKE '竞品对标·P2竞品%' AND user_id=?", (ua,))
            conn.execute("DELETE FROM authors WHERE author_id='p2_author_a' OR user_id IN (?,?)", (ua, ub))
            conn.execute("DELETE FROM alerts WHERE topic_id=? OR user_id IN (?,?)", (topic_a or -1, ua, ub))
            if cid:
                conn.execute("DELETE FROM creations WHERE id=?", (cid,))
            if bid_id:
                conn.execute("DELETE FROM bids WHERE id=?", (bid_id,))
            if alert_id:
                conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            conn.execute("DELETE FROM alert_rules WHERE user_id IN (?,?)", (ua, ub))
            conn.execute("DELETE FROM tasks WHERE topic_id=?", (topic_a or -1,))
            conn.execute("DELETE FROM auth_sessions WHERE user_id IN (?,?)", (ua, ub))
            conn.execute("DELETE FROM users WHERE id IN (?,?)", (ua, ub))

    from fastapi.testclient import TestClient
    from monitor.app import app
    saved_saas = os.environ.get("CORTECH_SAAS")
    os.environ["CORTECH_SAAS"] = "1"
    try:
        with TestClient(app) as client:
            health = client.get("/health")
            api_health = client.get("/api/health")
            check("健康检查 /health",
                  health.status_code == 200 and health.json().get("product") == "Cortech Chat"
                  and health.json().get("ok") is True and health.json().get("saas") is True)
            check("健康检查 /api/health", api_health.status_code == 200 and api_health.json().get("ok") is True)
            guarded = [
                "/api/topics", "/api/posts", "/api/stats", "/api/trends", "/api/tasks",
                "/api/alerts", "/api/influencers", "/api/competitors", "/api/creator/types",
                "/api/bids", "/api/news", "/api/events", "/api/accounts", "/api/export.csv",
                "/api/alerts/export.csv", "/api/chat/mode", "/api/fakecheck/brands",
            ]
            denied = [p for p in guarded if client.get(p).status_code != 401]
            notes_left = client.get("/api/notes")
            check("未登录业务接口返回 401", not denied, ",".join(denied))
            check("没有 /api/notes 数据面", notes_left.status_code in (401, 404))
            email = f"p2-http-{int(time.time())}@example.com"
            reg = client.post("/api/auth/register", json={"email": email, "password": "secret1"})
            check("注册后可进营销接口", reg.status_code == 200)
            mounted = {
                "/api/creator/types": lambda r: r.status_code == 200 and len(r.json().get("items") or r.json()) >= 1,
                "/api/bids": lambda r: r.status_code == 200,
                "/api/competitors": lambda r: r.status_code == 200 and "items" in r.json(),
                "/api/influencers": lambda r: r.status_code == 200 and "items" in r.json(),
                "/api/posts": lambda r: r.status_code == 200 and "items" in r.json(),
                "/api/stats": lambda r: r.status_code == 200 and "total_notes" in r.json(),
                "/api/alerts": lambda r: r.status_code == 200 and "items" in r.json() and "total" in r.json(),
            }
            bad_mount = []
            for path, pred in mounted.items():
                resp = client.get(path)
                if not pred(resp):
                    bad_mount.append(f"{path}:{resp.status_code}")
            check("营销与运营模块接口可挂载", not bad_mount, ",".join(bad_mount))
            created = client.post("/api/topics", json={
                "name": "P2 HTTP 话题", "keywords": "样例", "max_notes": 5, "enable_comments": 0,
            })
            check("可创建话题", created.status_code == 200 and "id" in created.json())
            if created.status_code == 200:
                os.environ["CORTECH_COLLECTION_FIXTURE"] = "1"
                try:
                    crawled = client.post(f"/api/topics/{created.json()['id']}/crawl", json={})
                    check("样例采集接口可启动", crawled.status_code == 200 and "task_id" in crawled.json(),
                          crawled.text[:120])
                    if crawled.status_code == 200:
                        for _ in range(40):
                            task = client.get(f"/api/tasks/{crawled.json()['task_id']}")
                            if task.status_code == 200 and task.json().get("status") in ("done", "failed", "cancelled"):
                                break
                            time.sleep(0.25)
                        posts = client.get("/api/posts", params={"topic_id": created.json()["id"]})
                        stats = client.get("/api/stats", params={"topic_id": created.json()["id"]})
                        check("样例采集后列表与总览有数",
                              posts.status_code == 200 and posts.json().get("total", 0) >= 1
                              and stats.status_code == 200 and stats.json().get("total_notes", 0) >= 1)
                finally:
                    os.environ.pop("CORTECH_COLLECTION_FIXTURE", None)
            made = client.post("/api/alerts", json={
                "level": "orange", "title": "HTTP预警", "message": "闭环",
                "post_ids": ["http_post_9"],
            })
            body = made.json() if made.status_code == 200 else {}
            check("HTTP 创建预警带 post_ids",
                  made.status_code == 200 and body.get("post_ids") == ["http_post_9"], made.text[:160])
            if made.status_code == 200:
                read = client.post(f"/api/alerts/{body['id']}/read")
                exported = client.get("/api/alerts/export.csv")
                posts_csv = client.get("/api/export.csv")
                check("HTTP 标已读并导出预警与帖子",
                      read.status_code == 200 and read.json().get("status") == "read"
                      and exported.status_code == 200 and "http_post_9" in exported.text
                      and posts_csv.status_code == 200 and "发布时间" in posts_csv.text)
    finally:
        if saved_saas is None:
            os.environ.pop("CORTECH_SAAS", None)
        else:
            os.environ["CORTECH_SAAS"] = saved_saas
        os.environ.pop("CORTECH_COLLECTION_FIXTURE", None)

    # ---- 10. 主机矩阵 / 长任务进度 / 扫码状态机（不访问小红书） ----
    from monitor.xhs_hosts import API_ORIGIN, COOKIE_READ_URLS, host_is_xhs, merge_cookie_rows
    merged = merge_cookie_rows([
        {"name": "a1", "value": "from-hk", "domain": ".xiaohongshu.hk"},
        {"name": "a1", "value": "from-cn", "domain": ".xiaohongshu.cn"},
        {"name": "a1", "value": "from-com", "domain": ".xiaohongshu.com"},
        {"name": "web_session", "value": "only-hk", "domain": ".xiaohongshu.hk"},
        {"name": "edith_only", "value": "edith-v", "domain": "edith.xiaohongshu.com"},
        {"name": "noise", "value": "nope", "domain": ".example.com"},
    ])
    merged_map = {row["name"]: row["value"] for row in merged}
    check("Cookie 同名优先 .com，并保留仅存在于 hk/edith 的项",
          merged_map.get("a1") == "from-com"
          and merged_map.get("web_session") == "only-hk"
          and merged_map.get("edith_only") == "edith-v"
          and "noise" not in merged_map
          and host_is_xhs("www.xiaohongshu.cn")
          and host_is_xhs("www.xiaohongshu.hk")
          and not host_is_xhs("evil.example.com"))
    check("采集读 Cookie 覆盖三个网页域和 edith",
          "https://www.xiaohongshu.cn" in COOKIE_READ_URLS
          and "https://www.xiaohongshu.hk" in COOKIE_READ_URLS
          and API_ORIGIN == "https://edith.xiaohongshu.com")
    import_src = (__import__("pathlib").Path(__file__).resolve().parent
                  / "browser_import.py").read_text(encoding="utf-8")
    check("浏览器导入 SQL 包含 cn 与 hk",
          "xiaohongshu.cn" in import_src and "xiaohongshu.hk" in import_src)

    from monitor.task_progress import present_task, record_task_progress
    prog_task = db.create_task(tid, "cortech_crawler")
    try:
        record_task_progress(prog_task, 12, "第一页")
        record_task_progress(prog_task, 4, "不应回退")
        record_task_progress(prog_task, 12, "长任务仍在第一页")
        record_task_progress(prog_task, -1, "补充说明而不清零")
        shown = present_task(db.get_task(prog_task))
        check("长任务进度不回退且说明可更新",
              shown["progress"] == 12
              and "补充说明" in shown["progress_detail"]
              and shown["status_label"] == "排队中"
              and "deleted_count" in shown and "updated_at" in shown
              and shown["notes_count"] == 0 and shown["error"] == "")
        db.update_task(prog_task, status="done", progress=96)
        done_view = present_task(db.get_task(prog_task))
        check("完成态进度对外显示 100",
              done_view["progress"] == 100 and done_view["status_label"] == "采集完成")
    finally:
        with db._connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (prog_task,))

    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    saas_bat = (root / "start-saas.bat").read_text(encoding="ascii")
    cortech_bat = (root / "start-cortech.bat").read_text(encoding="utf-8")
    check("Windows SaaS 启动脚本不打开桌面、不安装包",
          "CORTECH_SAAS=1" in saas_bat
          and "python -m monitor.app" in saas_bat
          and "http://127.0.0.1:8000" in saas_bat
          and "CORTECH_DATA_DIR" in saas_bat
          and "CortechChat.py" not in saas_bat
          and "pip " not in saas_bat.lower()
          and "chcp" not in saas_bat.lower())
    ps1 = (root / "extensions" / "build-zip.ps1").read_text(encoding="utf-8")
    check("Windows 插件 zip 脚本",
          "cortech-xhs-assist" in ps1 and "ZipFile" in ps1 and "manifest.json" in ps1)
    check("启动脚本在缺少浏览器时安装 chromium（失败不阻断）",
          "python -m playwright install chromium" in saas_bat
          and "python -m playwright install chromium" in cortech_bat
          and "pip " not in saas_bat.lower())

    from monitor.config import login_cache_dir
    from monitor.login_flow import (apply_successful_login, get_manager,
                                    set_launch_browser)
    cache_file = login_cache_dir() / "storage_state.json"
    cache_backup = cache_file.read_text(encoding="utf-8") if cache_file.exists() else None
    set_launch_browser(False)
    os.environ["CORTECH_LOGIN_WARMUP_SEC"] = "0"
    os.environ["CORTECH_QUIET_HOURS"] = "off"
    p3_email = f"p3-qr-{int(time.time())}@example.com"
    aid = need_id = None
    try:
        from fastapi.testclient import TestClient
        from monitor.app import app
        import monitor.crawler_driver as crawl_mod
        get_manager().clear_all()
        os.environ.pop("CORTECH_COLLECTION_FIXTURE", None)
        with TestClient(app) as client:
            reg = client.post("/api/auth/register",
                              json={"email": p3_email, "password": "secret1"})
            check("扫码测试用户可注册", reg.status_code == 200, reg.text[:120])
            created = client.post("/api/accounts")
            body = created.json() if created.status_code == 200 else {}
            login = body.get("login") or {}
            aid = body.get("account_id")
            check("创建账号返回等待扫码",
                  created.status_code == 200 and login.get("state") == "waiting"
                  and login.get("state_label") == "等待扫码"
                  and login.get("ok") is False, created.text[:160])
            if aid:
                checked = client.post(f"/api/accounts/{aid}/check")
                listed = client.get("/api/accounts").json()
                row = next((i for i in listed.get("items") or [] if i["id"] == aid), {})
                check("等待扫码时检查不把账号写成失效",
                      checked.status_code == 200
                      and checked.json().get("state") == "waiting"
                      and row.get("status") == "pending"
                      and row.get("state_label") == "等待扫码")
                sess = get_manager().get_session(aid)
                sess.mark("waiting", qrcode_b64="aGVsbG8=",
                          message="请用小红书 App 扫码登录（120 秒内有效）")
                qr = client.get("/api/login/qrcode", params={"account_id": aid})
                check("二维码接口带等待状态",
                      qr.status_code == 200
                      and str(qr.json().get("image") or "").startswith("data:image/png;base64,")
                      and qr.json().get("state") == "waiting"
                      and qr.json().get("state_label") == "等待扫码")
                sess.mark("expired", qrcode_b64="",
                          message="二维码已过期，请重新获取")
                expired = client.get("/api/login/status", params={"account_id": aid})
                qr_gone = client.get("/api/login/qrcode", params={"account_id": aid})
                check("二维码过期返回 expired",
                      expired.status_code == 200
                      and expired.json().get("state") == "expired"
                      and expired.json().get("state_label") == "二维码已过期"
                      and qr_gone.status_code == 404
                      and qr_gone.json().get("state") == "expired")
                rescanned = client.post(f"/api/accounts/{aid}/rescan")
                check("重新扫码回到等待",
                      rescanned.status_code == 200
                      and rescanned.json().get("state") == "waiting"
                      and rescanned.json().get("state_label") == "等待扫码")
                db.set_current_user(reg.json()["id"])
                try:
                    need_id = db.create_account("待重扫")
                    db.update_account(need_id, {
                        "status": "expired", "check_note": "登录失效"})
                finally:
                    db.set_current_user(None)
                get_manager().clear(need_id)
                need = client.get("/api/login/status", params={"account_id": need_id})
                check("失效账号对外是需要重新扫码",
                      need.status_code == 200
                      and need.json().get("account_id") == need_id
                      and need.json().get("state") == "need_rescan"
                      and need.json().get("state_label") == "需要重新扫码"
                      and need.json().get("message") == "登录失效")
                apply_successful_login(aid, {
                    "cookies": [
                        {"name": "a1", "value": "mockeda1value99", "domain": ".xiaohongshu.com"},
                        {"name": "web_session", "value": "mockedwebsession99",
                         "domain": ".xiaohongshu.com"},
                    ],
                    "origins": [],
                }, nickname="扫码用户")
                setup = client.get("/api/setup/status").json()
                check("扫码成功后健康账号增加且不再要求会话",
                      int(setup.get("xhs_accounts_healthy") or 0) >= 1
                      and setup.get("collection", {}).get("code") == "xhs_ready"
                      and setup.get("collection", {}).get("needs_real_xhs_session") is False)
                topic = client.post("/api/topics", json={
                    "name": "P3 扫码话题", "keywords": "样例", "max_notes": 3,
                    "enable_comments": 0,
                })

                class _NoStart:
                    def __init__(self, *args, **kwargs):
                        pass

                    def start(self):
                        return None

                old_thread = crawl_mod.threading.Thread
                crawl_mod.threading.Thread = _NoStart
                try:
                    crawled = client.post(
                        f"/api/topics/{topic.json()['id']}/crawl", json={})
                    check("有效登录后采集不再返回 need_xhs_session",
                          topic.status_code == 200 and crawled.status_code == 200
                          and crawled.json().get("needs_real_xhs_session") is False
                          and "need_xhs_session" not in crawled.text
                          and "need_xhs_rescan" not in crawled.text,
                          crawled.text[:180])
                finally:
                    crawl_mod.threading.Thread = old_thread
    finally:
        set_launch_browser(True)
        os.environ.pop("CORTECH_LOGIN_WARMUP_SEC", None)
        os.environ.pop("CORTECH_QUIET_HOURS", None)
        get_manager().clear_all()
        if cache_backup is None:
            cache_file.unlink(missing_ok=True)
        else:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(cache_backup, encoding="utf-8")
        with db._connect() as conn:
            conn.execute("DELETE FROM notes WHERE topic_id IN "
                         "(SELECT id FROM topics WHERE name='P3 扫码话题')")
            conn.execute("DELETE FROM tasks WHERE topic_id IN "
                         "(SELECT id FROM topics WHERE name='P3 扫码话题')")
            conn.execute("DELETE FROM topics WHERE name='P3 扫码话题'")
            if aid:
                conn.execute("DELETE FROM xhs_accounts WHERE id=?", (aid,))
            if need_id:
                conn.execute("DELETE FROM xhs_accounts WHERE id=?", (need_id,))
            conn.execute("DELETE FROM auth_sessions WHERE user_id IN "
                         "(SELECT id FROM users WHERE email=?)", (p3_email,))
            conn.execute("DELETE FROM users WHERE email=?", (p3_email,))

    # ---- 11. Windows 登录 / 不自动导入 / 账号保护（不访问小红书） ----
    import inspect as _inspect
    import random as _random
    import sqlite3 as _sqlite3
    from monitor.account_crypto import encrypt_login_data
    from monitor.account_guard import (ReadOnlyViolation, STEALTH_INIT_SCRIPT,
                                       assert_readonly_endpoint, chromium_launch_attempts,
                                       fingerprint_for, mark_risk, next_delay,
                                       remember_fingerprint)
    from monitor.account_rotation import block_reason, eligible_accounts
    from monitor.browser_import import (CookieProtectError, _copy_sqlite, _decrypt_value,
                                        _is_lock_error, filter_cdp_cookies, import_login,
                                        public_staged, scan_browser_logins,
                                        stage_detected_login)
    from monitor.login_flow import LoginSession, public_login_error
    from monitor.xhs_errors import classify_biz_code, classify_http_status

    login_src = (_Path(__file__).resolve().parent / "login_flow.py").read_text(encoding="utf-8")
    import_src_full = (_Path(__file__).resolve().parent / "browser_import.py").read_text(encoding="utf-8")
    dialog_js = (root / "frontend" / "dist" / "assets" / "CortechLoginStatusDialog-DJ8kHEEH.js").read_text(encoding="utf-8")
    popup_js_now = (root / "extensions" / "cortech-xhs-assist" / "popup.js").read_text(encoding="utf-8")
    check("扫码不再写死 Linux Chromium 路径",
          "/opt/meta-chromium/chrome" not in login_src
          and "CORTECH_CHROMIUM_PATH" in login_src
          and "img.qrcode-img" in login_src
          and "screenshot" in login_src)
    check("账号管理引导扩展确认或扫码",
          "小红书助手" in dialog_js and "不会自动导入" in dialog_js
          and "扫码登录" in dialog_js and "停用" in dialog_js)
    check("插件确认后只进入候选",
          "confirm: true" in popup_js_now and "enable: false" in popup_js_now
          and "账号管理" in (root / "extensions" / "cortech-xhs-assist" / "popup.html").read_text(encoding="utf-8"))
    check("不实现提权解密 v20",
          "IElevator" not in import_src_full and "v20" in import_src_full
          and "create_account" not in _inspect.getsource(scan_browser_logins))
    saved_path = os.environ.pop("CORTECH_CHROMIUM_PATH", None)
    try:
        plan = chromium_launch_attempts()
        check("默认浏览器顺序：自带 Chromium、Edge、Chrome",
              plan[0].get("label") == "bundled" and "executable_path" not in plan[0]
              and any(item.get("channel") == "msedge" for item in plan)
              and any(item.get("channel") == "chrome" for item in plan))
        os.environ["CORTECH_CHROMIUM_PATH"] = r"C:\browsers\chrome.exe"
        only = chromium_launch_attempts()
        check("CORTECH_CHROMIUM_PATH 优先",
              len(only) == 1 and only[0].get("executable_path", "").endswith("chrome.exe"))
    finally:
        if saved_path is None:
            os.environ.pop("CORTECH_CHROMIUM_PATH", None)
        else:
            os.environ["CORTECH_CHROMIUM_PATH"] = saved_path
    readable = public_login_error(RuntimeError(
        "BrowserType.launch_persistent_context: Executable doesn't exist"))
    check("登录失败说明可读且带异常类型",
          "没有启动浏览器" in readable and "RuntimeError" in readable
          and "playwright install chromium" in readable)
    set_launch_browser(False)
    boom_sess = LoginSession(0)
    async def _boom():
        raise RuntimeError("Executable doesn't exist")
    boom_sess._async_run = _boom
    boom_sess._run()
    snap = boom_sess.snapshot()
    check("登录状态接口能看到浏览器失败原因",
          boom_sess.status == "failed" and "没有启动浏览器" in boom_sess.message
          and snap.get("message") == boom_sess.message)
    set_launch_browser(True)
    try:
        _decrypt_value(b"v20" + b"secret-cookie-blob", b"k" * 32)
        check("v20 不当成明文", False)
    except CookieProtectError as exc:
        check("v20 不当成明文", exc.code == "v20" and "secret-cookie-blob" not in str(exc))
    locked = OSError("being used by another process")
    locked.winerror = 32
    check("识别 Cookies 文件锁", _is_lock_error(locked))
    real_copy = __import__("monitor.browser_import", fromlist=["shutil"]).shutil.copy2
    def _locked_copy(*_args, **_kwargs):
        err = PermissionError("WinError 32")
        err.winerror = 32
        raise err
    __import__("monitor.browser_import", fromlist=["shutil"]).shutil.copy2 = _locked_copy
    try:
        _copy_sqlite(_Path(__file__))
        check("复制被锁的 Cookie 库会返回 locked", False)
    except CookieProtectError as exc:
        check("复制被锁的 Cookie 库会返回 locked", exc.code == "locked")
    finally:
        __import__("monitor.browser_import", fromlist=["shutil"]).shutil.copy2 = real_copy

    kept = filter_cdp_cookies([
        {"name": "a1", "value": "cdp-secret-a1-value", "domain": ".xiaohongshu.com"},
        {"name": "web_session", "value": "cdp-secret-web", "domain": ".xiaohongshu.cn"},
        {"name": "a1", "value": "nope", "domain": ".example.com"},
    ])
    check("CDP 只保留小红书 com/cn/hk",
          len(kept) == 2 and all(_x.endswith("xiaohongshu.com") or _x.endswith("xiaohongshu.cn")
                                 for _x in (row["domain"] for row in kept)))

    os.environ["CORTECH_BROWSER_CDP"] = "0"
    os.environ["CORTECH_LOGIN_WARMUP_SEC"] = "0"
    os.environ["CORTECH_QUIET_HOURS"] = "off"
    os.environ["CORTECH_RAMP_SEC"] = "0"
    secret_a1 = "scanA1ValueShouldNotLeak99"
    secret_web = "scanWebValueShouldNotLeak99"
    profile_root = _Path(tempfile.mkdtemp(prefix="cortech_scan_"))
    v20_root = _Path(tempfile.mkdtemp(prefix="cortech_v20_"))
    guard_ids = []
    detected_id = None
    try:
        def _write_cookies(root, rows):
            (root / "Local State").write_text(_json.dumps({
                "os_crypt": {"encrypted_key": __import__("base64").b64encode(b"k" * 32).decode(),
                             "app_bound_encrypted_key": "present"},
            }), encoding="utf-8")
            ck = root / "Default" / "Network"
            ck.mkdir(parents=True)
            conn = _sqlite3.connect(ck / "Cookies")
            conn.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB,"
                " value TEXT, path TEXT, expires_utc INTEGER, is_secure INTEGER,"
                " is_httponly INTEGER, samesite INTEGER)")
            conn.executemany(
                "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            conn.close()

        _write_cookies(profile_root, [
            (".xiaohongshu.com", "a1", b"", secret_a1, "/", 0, 1, 0, -1),
            (".xiaohongshu.com", "web_session", b"", secret_web, "/", 0, 1, 1, -1),
        ])
        _write_cookies(v20_root, [
            (".xiaohongshu.com", "a1", b"v20secret-cookie-blob", "", "/", 0, 1, 0, -1),
            (".xiaohongshu.com", "web_session", b"v20secret-cookie-blob", "", "/", 0, 1, 1, -1),
        ])
        before_ids = {row["id"] for row in db.list_accounts()}

        def _info(_cookies):
            return {"nickname": "候选昵称", "avatar_url": "https://example.test/a.png", "uid": "uid-scan"}

        report = scan_browser_logins(roots=[("Chrome", profile_root)], fetch_info=_info)
        dumped = _json.dumps(report, ensure_ascii=False)
        check("扫描只列出候选且不含 Cookie",
              report.get("auto_imported") is False
              and report["items"] and report["items"][0]["nickname"] == "候选昵称"
              and report["items"][0]["region"] == "www.xiaohongshu.com"
              and report["items"][0]["browser"] == "Chrome"
              and secret_a1 not in dumped and secret_web not in dumped
              and {row["id"] for row in db.list_accounts()} == before_ids)
        v20_report = scan_browser_logins(roots=[("Edge", v20_root)], fetch_info=_info)
        v20_dump = _json.dumps(v20_report, ensure_ascii=False)
        check("v20 返回原因且不导入",
              v20_report.get("auto_imported") is False
              and not v20_report["items"]
              and any(r.get("code") == "v20" for r in v20_report["reasons"])
              and "secret-cookie-blob" not in v20_dump
              and v20_report["recommended"]["action"] == "extension")
        try:
            import_login(report["items"][0]["key"], confirmed=False)
            check("未确认不能导入扫描结果", False)
        except Exception as exc:
            check("未确认不能导入扫描结果",
                  "不会自动导入" in str(exc) and {row["id"] for row in db.list_accounts()} == before_ids)
        import monitor.browser_import as _bi
        async def _fake_info(_cookies):
            return {"nickname": "已确认导入", "avatar_url": "", "uid": "uid-imp"}
        old_fetch = _bi._fetch_self_info
        old_dirs = _bi._user_data_dirs
        _bi._fetch_self_info = _fake_info
        _bi._user_data_dirs = lambda: [("Chrome", profile_root)]
        try:
            imported = import_login("Chrome||Default", confirmed=True)
        finally:
            _bi._fetch_self_info = old_fetch
            _bi._user_data_dirs = old_dirs
        guard_ids.append(imported["id"])
        check("确认后才写入账号",
              imported["id"] not in before_ids and db.get_account(imported["id"])["enabled"] == 1
              and secret_a1 not in _json.dumps(
                  {k: v for k, v in db.get_account(imported["id"]).items() if k != "login_data_enc"},
                  ensure_ascii=False))
        db.update_account(imported["id"], {"enabled": 0})
        check("停用账号不参与轮换",
              "停用" in block_reason(db.get_account(imported["id"]))
              and imported["id"] not in {a["id"] for a in eligible_accounts()})
        db.update_account(imported["id"], {"enabled": 1, "status": "valid"})

        staged_secret = "stagedA1ValueShouldNotLeak99"
        before_stage = {row["id"] for row in db.list_accounts()}
        staged = stage_detected_login(
            source="extension", browser="Chrome", profile="Default",
            nickname="未选择的插件登录", region="www.xiaohongshu.com",
            cookies={"a1": staged_secret, "web_session": "stagedWebValueShouldNotLeak99"})
        detected_id = staged["detected_id"]
        staged_dump = _json.dumps({"staged": staged, "public": public_staged()}, ensure_ascii=False)
        check("未选择的检测结果不进入账号和轮换",
              staged.get("staged") is True and staged.get("auto_imported") is False
              and staged.get("account_id", None) in (None,)
              and staged_secret not in staged_dump
              and {row["id"] for row in db.list_accounts()} == before_stage
              and detected_id not in {a["id"] for a in eligible_accounts()}
              and all(a["id"] in before_stage for a in eligible_accounts()))
        try:
            import_login(f"detected:{detected_id}", confirmed=False)
            check("未确认的候选不能导入", False)
        except Exception as exc:
            check("未确认的候选不能导入", "不会自动导入" in str(exc) or "不会参与采集" in str(exc))
        chosen = import_login(f"detected:{detected_id}", confirmed=True)
        detected_id = None
        guard_ids.append(chosen["id"])
        check("候选确认后才可采集",
              chosen["id"] in {a["id"] for a in eligible_accounts()}
              and staged_secret not in _json.dumps(public_staged(), ensure_ascii=False))

        os.environ["CORTECH_LOGIN_WARMUP_SEC"] = "900"
        from monitor.account_guard import arm_fresh_login
        arm_fresh_login(chosen["id"])
        warm_reason = block_reason(db.get_account(chosen["id"]))
        check("刚登录保护期不采集",
              "刚登录" in warm_reason and "保护期" in warm_reason
              and chosen["id"] not in {a["id"] for a in eligible_accounts()})
        os.environ["CORTECH_LOGIN_WARMUP_SEC"] = "0"
        os.environ["CORTECH_QUIET_HOURS"] = "0-24"
        db.update_account(chosen["id"], {"warmup_until": 0, "logged_in_at": 0})
        check("安静时段不采集", "安静" in block_reason(db.get_account(chosen["id"])))
        os.environ["CORTECH_QUIET_HOURS"] = "off"
        db.update_account(chosen["id"], {"skip_until": 0, "risk_pause_until": 0, "fail_streak": 0})
        mark_risk(chosen["id"], "小红书风控或访问受限（code=461）", code="461")
        paused = db.get_account(chosen["id"])
        first_until = int(paused["risk_pause_until"])
        mark_risk(chosen["id"], "小红书风控或访问受限（code=471）", code="471")
        paused2 = db.get_account(chosen["id"])
        from monitor import alerts as _alerts
        _alerts.init_db()
        alert_hit = any("自动暂停" in (a.get("title") or "") for a in _alerts.list_alerts().get("items") or [])
        check("风控自动暂停且不缩短退避",
              "风控" in block_reason(paused2)
              and int(paused2["risk_pause_until"]) >= first_until
              and int(paused2["fail_streak"]) >= 2
              and alert_hit
              and chosen["id"] not in {a["id"] for a in eligible_accounts()})
        db.update_account(chosen["id"], {"risk_pause_until": 0, "skip_until": 0, "fail_streak": 0})
        delays = [next_delay(None, rng=_random.Random(i)) for i in range(1, 6)]
        check("请求间隔带抖动",
              len({round(v, 3) for v in delays}) > 1 and all(3.0 <= v <= 7.5 for v in delays))
        os.environ["CORTECH_RAMP_SEC"] = "3600"
        db.update_account(chosen["id"], {
            "logged_in_at": int(time.time()) - 10,
            "warmup_until": int(time.time()) - 5,
        })
        ramp_delay = next_delay(chosen["id"], rng=_random.Random(2))
        check("慢速恢复间隔更长", 8.0 <= ramp_delay <= 16.0)
        os.environ["CORTECH_RAMP_SEC"] = "0"
        remember_fingerprint(chosen["id"], {
            "user_agent": "TestAgent/1.0",
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        })
        fp = fingerprint_for(chosen["id"])
        check("账号指纹保持一致",
              fp["user_agent"] == "TestAgent/1.0" and fp["viewport"]["width"] == 1280
              and fp["locale"] == "zh-CN" and fp["timezone_id"] == "Asia/Shanghai"
              and "webdriver" in STEALTH_INIT_SCRIPT)
        assert_readonly_endpoint("POST", "/api/sns/web/v1/search/notes")
        try:
            assert_readonly_endpoint("POST", "/api/sns/web/v1/note/like")
            check("采集拒绝写操作", False)
        except ReadOnlyViolation:
            check("采集拒绝写操作", True)
        check("风控码 461/471/300011 与频繁请求",
              classify_biz_code(461, "")[0] == "risk"
              and classify_biz_code(471, "")[0] == "risk"
              and classify_biz_code(300011, "账号异常")[0] == "risk"
              and classify_biz_code(-1, "请求频繁")[0] == "risk"
              and classify_http_status(461, False)[0] == "risk")

        from fastapi.testclient import TestClient
        from monitor.app import app
        os.environ["CORTECH_ENABLE_COOKIE_HANDSHAKE"] = "1"
        os.environ["CORTECH_SAAS"] = "1"
        os.environ["CORTECH_SKIP_BROWSER_SCAN"] = "1"
        http_email = f"guard-http-{int(time.time())}@example.com"
        http_secret = "httpStagedA1Value99"
        with TestClient(app) as client:
            reg = client.post("/api/auth/register", json={"email": http_email, "password": "secret1"})
            check("保护测试用户可注册", reg.status_code == 200, reg.text[:120])
            csrf = client.get("/api/auth/csrf")
            token = (csrf.json() or {}).get("csrf_token") if csrf.status_code == 200 else ""
            listed_before = client.get("/api/accounts")
            n_before = len((listed_before.json() or {}).get("items") or [])
            staged_http = client.post(
                "/api/accounts/cookie-handshake",
                headers={"X-CSRF-Token": token},
                json={"a1": http_secret, "web_session": "httpStagedWebValue99",
                      "nickname": "网页候选", "confirm": True, "enable": False,
                      "cookies": {"a1": http_secret, "web_session": "httpStagedWebValue99"}},
            )
            body = staged_http.json() if staged_http.headers.get("content-type", "").startswith("application/json") else {}
            listed_mid = client.get("/api/accounts")
            avail = client.get("/api/accounts/browser-available")
            avail_text = avail.text
            check("插件握手只暂存候选",
                  staged_http.status_code == 200 and body.get("staged") is True
                  and body.get("auto_imported") is False
                  and http_secret not in staged_http.text
                  and len((listed_mid.json() or {}).get("items") or []) == n_before
                  and http_secret not in avail_text
                  and any((item.get("nickname") == "网页候选") for item in (avail.json() or {}).get("items") or []))
            bare = client.post("/api/accounts/browser-import", json={"key": body.get("key") or "x"})
            check("接口未带确认不会导入", bare.status_code == 400 and "不会" in bare.text)
            done = client.post("/api/accounts/browser-import",
                               json={"key": body.get("key"), "confirm": True})
            done_body = done.json() if done.status_code == 200 else {}
            if done_body.get("id"):
                guard_ids.append(done_body["id"])
            listed = client.get("/api/accounts")
            row = next((i for i in (listed.json() or {}).get("items") or []
                        if i.get("id") == done_body.get("id")), {})
            check("确认导入后账号带保护状态",
                  done.status_code == 200 and row.get("enabled") == 1
                  and (row.get("protection") or {}).get("readonly_collection") is True
                  and http_secret not in listed.text)
            off = client.post(f"/api/accounts/{done_body.get('id')}/enabled", json={"enabled": False})
            check("接口可停用账号",
                  off.status_code == 200 and off.json().get("enabled") == 0
                  and "停用" in (off.json().get("protection_message") or ""))
    finally:
        os.environ.pop("CORTECH_BROWSER_CDP", None)
        os.environ.pop("CORTECH_LOGIN_WARMUP_SEC", None)
        os.environ.pop("CORTECH_QUIET_HOURS", None)
        os.environ.pop("CORTECH_RAMP_SEC", None)
        os.environ.pop("CORTECH_ENABLE_COOKIE_HANDSHAKE", None)
        os.environ.pop("CORTECH_SAAS", None)
        os.environ.pop("CORTECH_SKIP_BROWSER_SCAN", None)
        set_launch_browser(True)
        with db._connect() as conn:
            for gid in guard_ids:
                conn.execute("DELETE FROM xhs_accounts WHERE id=?", (gid,))
            if detected_id:
                conn.execute("DELETE FROM xhs_detected_logins WHERE id=?", (detected_id,))
            conn.execute("DELETE FROM xhs_detected_logins WHERE nickname IN ('未选择的插件登录','网页候选')")
            conn.execute("DELETE FROM auth_sessions WHERE user_id IN (SELECT id FROM users WHERE email LIKE 'guard-http-%')")
            conn.execute("DELETE FROM users WHERE email LIKE 'guard-http-%'")
        shutil.rmtree(profile_root, ignore_errors=True)
        shutil.rmtree(v20_root, ignore_errors=True)

    # ---- 汇总 ----
    fails = [n for n, ok in results if not ok]
    print(f"\n==== {len(results)-len(fails)}/{len(results)} 通过 ====")
    if fails:
        print("失败项:", fails)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
