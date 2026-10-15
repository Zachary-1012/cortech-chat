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
    check("DirectAPIFailed 可捕获", issubclass(DirectAPIFailed, Exception))

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
