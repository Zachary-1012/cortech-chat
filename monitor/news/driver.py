# -*- coding: utf-8 -*-
"""monitor/news/driver.py — web-news 采集任务执行器。

trigger_news_collection(source_id): 选 adapter（kind=search→googlenews_rss,
kind=rss→rss）→ 抓取 → 去重（URL 精确 + 标题相似度 >=0.85）→ 情感打标 → 入库。
与 xhs 采集共用 tasks 表（task_type='web-news'），单任务锁复用 crawler_driver 的锁。
"""
from __future__ import annotations

import difflib
import logging
import time
from typing import Dict, List, Tuple

from .. import db
from .. import crawler_driver as xhs_driver  # 复用全局单任务锁
from ..sentiment import analyze
from . import base as news_base

logger = logging.getLogger("xhs-monitor.news")

_KIND_TO_ADAPTER = {"search": "googlenews_rss", "rss": "rss"}
_TITLE_SIM_THRESHOLD = 0.85


def _title_duplicate(title: str, recent_titles: List[str]) -> bool:
    for t in recent_titles:
        if t and difflib.SequenceMatcher(None, title, t).ratio() >= _TITLE_SIM_THRESHOLD:
            return True
    return False


def trigger_news_collection(source_id: int) -> Dict:
    """执行一次资讯采集。返回 {notes_added, skipped_dup, source, news_source_id}"""
    news_base.import_all()
    src = db.get_news_source(source_id)
    if not src:
        raise ValueError(f"资讯源不存在: id={source_id}")
    adapter_name = _KIND_TO_ADAPTER.get(src["kind"])
    if not adapter_name:
        raise ValueError(f"未知资讯源类型: {src['kind']}")
    adapter = news_base.get(adapter_name)

    lock = xhs_driver._run_lock
    if not lock.acquire(blocking=False):
        raise RuntimeError("已有采集任务在运行（xhs/web-news 共用单锁），请稍后再试")
    task_id = db.create_task(None, engine=adapter_name, task_type="web-news",
                             news_source_id=source_id)
    run_dir = xhs_driver.RUNS_DIR / f"news_{task_id}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "news.log"

    def _log(s: str) -> None:
        xhs_driver._append_log(task_id, log_file, s)

    try:
        _log(f"[news] 资讯源「{src['name']}」kind={src['kind']} adapter={adapter_name} 开始采集")
        keywords = [k.strip() for k in (src["keywords"] or "").split(",") if k.strip()]
        raw_items = adapter.collect(keywords, url=src["url"] or "")
        _log(f"[news] 抓取到 {len(raw_items)} 条候选")
        recent = db.recent_news_titles(500)
        added = skipped = 0
        for it in raw_items:
            if not it.url:
                skipped += 1
                continue
            if _title_duplicate(it.title, recent):
                skipped += 1
                continue
            label, score = analyze(f"{it.title}\n{it.summary}".strip())
            ok = db.upsert_news({
                "url": it.url, "title": it.title, "source": it.source,
                "publish_time": it.publish_time,
                "publish_time_raw": it.publish_time_raw,
                "summary": it.summary, "sentiment": label,
                "sentiment_score": score, "keyword": it.keyword,
                "collected_at": int(time.time()),
            })
            if ok:
                added += 1
                recent.append(it.title)
            else:
                skipped += 1
        _log(f"[news] 完成：新增 {added} 条，去重跳过 {skipped} 条")
        db.finish_task(task_id, "done", f"新增资讯 {added} 条")
        return {"added": added, "skipped_dup": skipped,
                "source": src["name"], "news_source_id": source_id}
    except Exception as e:
        _log(f"[news] 失败：{e}")
        db.finish_task(task_id, "failed", str(e)[:200])
        raise
    finally:
        lock.release()
