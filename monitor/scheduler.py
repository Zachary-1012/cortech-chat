# -*- coding: utf-8 -*-
"""定时调度：APScheduler，按话题 cron 规则自动触发采集"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import crawler_driver, db

logger = logging.getLogger("xhs-monitor.scheduler")

_scheduler: BackgroundScheduler | None = None


def _job(topic_id: int) -> None:
    try:
        # 系统上下文：按话题归属设置用户，采集数据记到话题所有者名下
        topic = db.get_topic(topic_id)
        db.set_current_user(topic["user_id"] if topic else None)
        try:
            task_id = crawler_driver.trigger_crawl(topic_id)
        finally:
            db.set_current_user(None)
        logger.info("定时采集已触发：topic_id=%s task_id=%s", topic_id, task_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时采集触发失败 topic_id=%s: %s", topic_id, e)


def _news_job(source_id: int) -> None:
    from .news import driver as news_driver
    try:
        src = db.get_news_source(source_id)
        db.set_current_user(src["user_id"] if src else None)
        try:
            res = news_driver.trigger_news_collection(source_id)
        finally:
            db.set_current_user(None)
        logger.info("定时资讯采集已触发：source_id=%s 新增 %s 条",
                    source_id, res["added"])
    except Exception as e:  # noqa: BLE001
        logger.exception("定时资讯采集失败 source_id=%s: %s", source_id, e)


def refresh_jobs() -> None:
    """按当前 topics 表 + news_sources 表重建所有定时任务（启用中的才排期）"""
    global _scheduler
    if _scheduler is None:
        return
    for job in list(_scheduler.get_jobs()):
        _scheduler.remove_job(job.id)
    for t in db.list_topics():
        if not t["enabled"]:
            continue
        try:
            trigger = CronTrigger.from_crontab(t["cron"] or "0 2 * * *")
        except Exception as e:  # noqa: BLE001
            logger.warning("话题 %s cron 表达式非法(%s)，跳过排期", t["id"], e)
            continue
        _scheduler.add_job(_job, trigger, args=(t["id"],),
                           id=f"topic_{t['id']}", replace_existing=True,
                           name=f"采集:{t['name']}")
        logger.info("已排期：%s -> %s", t["name"], t["cron"])
    for s in db.list_news_sources():
        if not s["enabled"]:
            continue
        try:
            trigger = CronTrigger.from_crontab(s["cron"] or "0 3 * * *")
        except Exception as e:  # noqa: BLE001
            logger.warning("资讯源 %s cron 表达式非法(%s)，跳过排期", s["id"], e)
            continue
        _scheduler.add_job(_news_job, trigger, args=(s["id"],),
                           id=f"news_{s['id']}", replace_existing=True,
                           name=f"资讯:{s['name']}")
        logger.info("已排期资讯源：%s -> %s", s["name"], s["cron"])


def _sign_probe_job() -> None:
    """L2 主动探针（每 30 分钟）：2 个请求，测签名健康。"""
    try:
        import asyncio
        from . import sign_drift
        result = asyncio.run(sign_drift.l2_probe())
        logger.info("[sign_drift] L2 探针：%s", result.get("detail", "")[:80])
    except Exception as e:  # noqa: BLE001
        logger.warning("[sign_drift] L2 探针异常：%s", e)


def _fakecheck_sweep_job() -> None:
    """伪冒反查每日巡检：对所有品牌跑关键词矩阵 Sweep，新发现推送到预警。"""
    try:
        from . import fakecheck
        for b in fakecheck.list_brands():
            bid = b["id"]
            # 只对启用自动巡检的品牌跑（默认开，可在品牌备注里关）
            try:
                tid = fakecheck.start_sweep(bid, name=f"每日巡检-{b['name']}")
                logger.info("伪冒每日巡检已启动：品牌=%s task=%s", b["name"], tid)
            except ValueError as e:
                # 无登录态或无关键词时跳过，不报错
                logger.info("伪冒巡检跳过 %s：%s", b["name"], e)
            except Exception:
                logger.exception("伪冒巡检启动失败 brand_id=%s", bid)
    except Exception:
        logger.exception("伪冒每日巡检异常")


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    refresh_jobs()
    # L2 签名探针：每 30 分钟（REA v2 D6）
    _scheduler.add_job(_sign_probe_job, "interval", minutes=30,
                       id="sign_probe", replace_existing=True)
    # 伪冒反查每日巡检：每天凌晨 4 点
    _scheduler.add_job(_fakecheck_sweep_job, "cron", hour=4, minute=0,
                       id="fakecheck_daily_sweep", replace_existing=True)
    _scheduler.start()
    logger.info("APScheduler 已启动（含签名探针、伪冒每日巡检）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
