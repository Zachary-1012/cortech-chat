# -*- coding: utf-8 -*-
"""AI 聊天指挥中心：自然语言指令 -> 工具执行 -> 结果返回。

模式：
  - AI 模式：OpenAI 兼容协议（POST /v1/chat/completions）+ function calling，
    provider 多选一（Groq / Gemini / Brio 免费网关备用 / 自定义）。
  - 规则模式：网关不可达 / 未配置 key 时，正则意图解析兜底，保证"下指令网站就跟"。
key 安全：永不打日志（mask），永不经 GET 接口返回。
"""
from __future__ import annotations

import logging
import re
import time
import uuid as _uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ai_knowledge, crawler_driver, db, trends
from .config import (AI_TIMEOUT_SEC, DATA_DIR, _is_placeholder, get_active_provider,
                     get_ai_config, mask_key)

logger = logging.getLogger("xhs-monitor.ai_chat")

def _build_system_prompt() -> str:
    """系统提示词：话题列表动态生成（通用化，不 hardcode 品牌）+ 功能知识库。"""
    names = [t["name"] for t in db.list_topics()[:10]]
    topics_text = "、".join(names) if names else "（暂无话题）"
    features_text = ai_knowledge.knowledge_text()
    return f"""你是 Cortech Chat，一个帮用户干活的真人助理。用户说什么，你就做什么。

规矩只有两条：
1. 不要编造数据。查不到就说查不到，不要猜。
2. 需要操作（采集/导出/查询）时，调用提供的工具函数，不要只说不做。

工作方式：
- 用户说"分析一下这些帖子"→直接调工具分析，不要反问一堆问题。
- 用户说"写个文案"→直接写。
- 采集前可以先确认一下关键词（建议，不是强制）。用户已经说得很清楚时，直接干。
- 说话简洁直接，像真人助理，不要套话、不要模式化流程。

你了解产品的全部功能（用户问起时，自然地提到入口，不要背菜单）：
{features_text}

可用话题：{topics_text}（可按名模糊匹配）。"""


def _detect_shortcuts(text: str, top_n: int = 3) -> List[Dict[str, Any]]:
    """自然语言 → 功能快捷入口。AI 模式和规则模式共用。"""
    return ai_knowledge.to_shortcuts(ai_knowledge.match_features(text, top_n))



# ---------------- 工具实现 ----------------

def _resolve_topic(topic_id=None, topic_name=None) -> Optional[dict]:
    if topic_id:
        return db.get_topic(int(topic_id))
    if topic_name:
        name = re.sub(r"\s+", "", str(topic_name))
        best = None
        for t in db.list_topics():
            tn = re.sub(r"\s+", "", t["name"])
            if name in tn or tn in name:
                best = t
                break
        return best
    return None


def tool_list_topics() -> Dict[str, Any]:
    return {"topics": [
        {"id": t["id"], "name": t["name"], "keywords": t["keywords"],
         "engine": t.get("engine"), "enabled": t["enabled"]}
        for t in db.list_topics()]}


# ---- 采集确认流（AI 聊天是指令主入口：先复述确认，用户确认后才启动） ----

PENDING_COLLECTIONS: Dict[str, Dict[str, Any]] = {}
PENDING_TTL_SEC = 600
# 最近一次提案（规则模式"开始/取消"口令确认用；单用户本地应用足够）
_latest_pending_cid: Optional[str] = None

def _build_chips():
    """快捷 chips：从话题动态生成（通用化，不 hardcode 品牌）。"""
    chips = []
    for i, t in enumerate(db.list_topics()[:2]):
        label = t["name"][:8]
        chips.append({"id": f"c{i+1}", "label": f"采{label}",
                      "message": f"采集{t['name']}"})
    chips += [
        {"id": "c3", "label": "最近7天负面分析", "message": "分析一下最近7天负面舆情"},
        {"id": "c4", "label": "导出上周负面", "message": "导出上周负面帖子"},
    ]
    return chips




def _account_label(account_id=None) -> str:
    if account_id:
        acc = db.get_account(int(account_id))
        if acc:
            return acc["nickname"] or f"账号{account_id}"
    d = db.get_default_account()
    return (d["nickname"] or f"账号{d['id']}") if d else "默认账号"


def tool_propose_collection(keywords=None, topic_name=None, topic_id=None,
                            account_id=None, aggressive=False,
                            max_notes=None) -> Dict[str, Any]:
    """解析采集意图 → 生成待确认提案（不直接启动）。

    keywords：新主题关键词（逗号分隔或列表），无需预先配置话题。
    topic_name/topic_id：已存在的话题。
    """
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,，、]", keywords) if k.strip()]
    keywords = keywords or []
    t = None
    if topic_id or topic_name:
        # _resolve_topic 在下方定义，运行时解析
        t = _resolve_topic(topic_id, topic_name)
    # 新主题：自动生成话题名（沉淀用）
    topic_label = t["name"] if t else (
        f"聊天采集：{'/'.join(keywords[:3])}" if keywords else "未命名采集")
    cid = _uuid.uuid4().hex[:12]
    PENDING_COLLECTIONS[cid] = {
        "keywords": keywords, "topic_id": t["id"] if t else None,
        "topic_name": topic_label, "account_id": account_id,
        "aggressive": bool(aggressive), "max_notes": max_notes,
        "created_at": time.time(),
    }
    # 清理过期
    for k in [k for k, v in PENDING_COLLECTIONS.items()
              if time.time() - v["created_at"] > PENDING_TTL_SEC]:
        PENDING_COLLECTIONS.pop(k, None)
    global _latest_pending_cid
    _latest_pending_cid = cid
    summary = (f"我理解你要采：{topic_label}"
               f"（关键词：{'、'.join(keywords) if keywords else t['keywords'] if t else '—'}）"
               f"，用账号「{_account_label(account_id)}」"
               f"{'，⚠️强攻模式（高频，有风控风险）' if aggressive else ''}"
               f"——开始吗？")
    return {"ok": True, "confirmation_id": cid, "summary": summary,
            "params": PENDING_COLLECTIONS[cid]}


def tool_start_collection(topic_id=None, topic_name=None, keywords=None,
                          engine=None, account_id=None, aggressive=False,
                          max_notes=None, confirmation_id=None) -> Dict[str, Any]:
    """启动采集。支持三种方式：
    1) confirmation_id：执行已确认的提案；
    2) topic_id/topic_name：已有话题；
    3) keywords：新主题 → 自动创建话题沉淀（来源=聊天），方便下次复用/对比。
    """
    if confirmation_id:
        p = PENDING_COLLECTIONS.pop(confirmation_id, None)
        if not p:
            return {"ok": False, "error": "确认已过期，请重新下达采集指令"}
        topic_id, keywords = p["topic_id"], p["keywords"]
        account_id, aggressive = p["account_id"], p["aggressive"]
        max_notes = p["max_notes"]
        topic_name = p["topic_name"]
    t = _resolve_topic(topic_id, topic_name) if (topic_id or topic_name) else None
    if not t and keywords:
        if isinstance(keywords, str):
            keywords = [k.strip() for k in re.split(r"[,，、]", keywords) if k.strip()]
        name = (topic_name or f"聊天采集：{'/'.join(keywords[:3])}").strip()
        tid = db.create_topic(name, ",".join(keywords),
                              max_notes=max_notes or 50, enabled=0,
                              engine=engine or "cortech_direct")
        t = db.get_topic(tid)
        auto_created = True
    elif t:
        auto_created = False
    else:
        names = [x["name"] for x in db.list_topics()]
        return {"ok": False, "error": f"找不到话题（现有：{names}），或请直接给关键词"}
    if db.running_task_exists() or crawler_driver.is_running():
        return {"ok": False, "error": "已有采集任务在运行，请稍后再试"}
    try:
        task_id = crawler_driver.trigger_crawl(
            t["id"], engine=engine, max_notes=max_notes,
            account_id=account_id, aggressive=bool(aggressive))
    except ValueError as e:
        return {"ok": False, "error": str(e),
                "need_login": "重新扫码" in str(e)}
    return {"ok": True, "task_id": task_id, "topic": t["name"],
            "topic_id": t["id"], "auto_created_topic": auto_created,
            "account": _account_label(account_id),
            "aggressive": bool(aggressive),
            "engine": engine or t.get("engine") or "cortech_direct"}


def tool_collection_report(task_id) -> Dict[str, Any]:
    """采集完成后在聊天里汇报：数量、负面数、Top3 链接"""
    t = db.get_task(int(task_id))
    if not t:
        return {"ok": False, "error": "任务不存在"}
    if t["status"] not in ("done", "cancelled", "failed"):
        return {"ok": False, "status": t["status"],
                "progress": t.get("progress", 0),
                "detail": t.get("progress_detail", ""),
                "error": "任务尚未完成"}
    topic_id = t.get("topic_id")
    _pg = db.list_posts(topic_id=topic_id, page=1, page_size=1000) if topic_id else {}
    notes = _pg.get("items", []) if isinstance(_pg, dict) else []
    neg = [n for n in notes if n.get("sentiment") == "negative"]
    neg_sorted = sorted(neg, key=lambda n: (n.get("liked_count", 0) or 0),
                        reverse=True)[:3]
    return {
        "ok": True, "status": t["status"], "topic_id": topic_id,
        "notes_total": len(notes),
        "negative_count": len(neg),
        "negative_ratio": round(len(neg) / len(notes) * 100, 1) if notes else 0,
        "top3": [{"title": n.get("title") or "(无标题)",
                  "url": n.get("note_url") or "",
                  "liked": n.get("liked_count", 0)}
                 for n in neg_sorted],
    }


def tool_collection_status(task_id=None) -> Dict[str, Any]:
    if task_id:
        t = db.get_task(int(task_id))
        return {"task": t} if t else {"error": "任务不存在"}
    return {"tasks": db.list_tasks(5)}


def tool_query_notes(topic_id=None, topic_name=None, sentiment=None,
                     keyword=None, days=None, limit=10) -> Dict[str, Any]:
    t = _resolve_topic(topic_id, topic_name)
    tid = t["id"] if t else None
    date_from = date_to = None
    if days:
        from datetime import date, timedelta
        today = date.today()
        date_from = str(today - timedelta(days=int(days) - 1))
        date_to = str(today)
    data = db.list_posts(tid, sentiment, keyword, page=1,
                         page_size=min(int(limit or 10), 50),
                         date_from=date_from, date_to=date_to)
    return {"total": data["total"], "notes": [
        {"post_id": n["post_id"], "title": n["title"], "author": n["author"],
         "sentiment": n["sentiment"], "liked_count": n["liked_count"],
         "comment_count": n["comment_count"], "note_url": n["note_url"]}
        for n in data["items"]]}


def tool_sentiment_stats(topic_id=None, topic_name=None, days=7) -> Dict[str, Any]:
    t = _resolve_topic(topic_id, topic_name)
    return trends.build_briefing(t["id"] if t else None, int(days or 7))


def tool_hot_words(topic_id=None, topic_name=None, days=7, top_n=20) -> Dict[str, Any]:
    t = _resolve_topic(topic_id, topic_name)
    return {"hot_words": trends.hot_words(t["id"] if t else None, int(days or 7),
                                          int(top_n or 20))}


def tool_generate_brief(topic_id=None, topic_name=None, days=7) -> Dict[str, Any]:
    return tool_sentiment_stats(topic_id, topic_name, days)


def tool_export_csv(topic_id=None, topic_name=None, sentiment=None,
                    days=None) -> Dict[str, Any]:
    from . import reports as _reports
    t = _resolve_topic(topic_id, topic_name)
    tid = t["id"] if t else None
    date_from = date_to = None
    if days:
        from datetime import date, timedelta
        today = date.today()
        date_from = str(today - timedelta(days=int(days) - 1))
        date_to = str(today)
    fname, text = _reports.export_notes_csv(tid, sentiment, date_from, date_to)
    exp_dir = DATA_DIR / "exports"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / fname).write_text(text, encoding="utf-8")
    rows = max(0, text.count("\n") - 1)
    return {"ok": True, "rows": rows,
            "download_url": f"/api/download/{fname}", "filename": fname}


def tool_negative_alerts(topic_id=None, topic_name=None) -> Dict[str, Any]:
    topics = [t for t in db.list_topics()
              if (not topic_id and not topic_name)
              or (t["id"] == (int(topic_id) if topic_id else -1))
              or (_resolve_topic(topic_id, topic_name) or {}).get("id") == t["id"]]
    alerts = []
    for t in topics:
        s = trends.spike_alert(t["id"])
        alerts.append({"topic_id": t["id"], "topic_name": t["name"], **s})
    actives = [a for a in alerts if a["alert"]]
    return {"alerts": alerts, "has_alert": bool(actives),
            "summary": f"共 {len(alerts)} 个话题，{len(actives)} 个触发突增预警"}


TOOLS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "list_topics": tool_list_topics,
    "propose_collection": tool_propose_collection,
    "start_collection": tool_start_collection,
    "collection_status": tool_collection_status,
    "collection_report": tool_collection_report,
    "query_notes": tool_query_notes,
    "sentiment_stats": tool_sentiment_stats,
    "hot_words": tool_hot_words,
    "generate_brief": tool_generate_brief,
    "export_csv": tool_export_csv,
    "negative_alerts": tool_negative_alerts,
}

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "list_topics",
        "description": "列出所有监控话题（id/名称/关键词/引擎）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_collection",
        "description": ("生成采集提案（建议流程：先提案、用户确认后再启动）。"
                        "用户意图明确时也可直接用 start_collection 启动。"
                        "新主题直接给 keywords，无需预先配置话题。"),
        "parameters": {"type": "object", "properties": {
            "keywords": {"type": "string",
                         "description": "新主题关键词，逗号分隔，如\"万象城,广州万象城\""},
            "topic_name": {"type": "string", "description": "已有话题名（模糊匹配）"},
            "topic_id": {"type": "integer", "description": "已有话题ID"},
            "account_id": {"type": "integer", "description": "指定账号ID，不填用默认"},
            "aggressive": {"type": "boolean",
                           "description": "强攻模式（高频，有风控风险，需用户明确要求）"},
            "max_notes": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "start_collection",
        "description": ("启动采集。"
                        "可用 confirmation_id 执行已确认提案，或直接给 topic/keywords 启动。"
                        "用户意图明确时直接启动，不必强制走确认。"),
        "parameters": {"type": "object", "properties": {
            "confirmation_id": {"type": "string", "description": "已确认的提案ID"},
            "topic_id": {"type": "integer"},
            "topic_name": {"type": "string"},
            "keywords": {"type": "string", "description": "新主题关键词（自动建话题沉淀）"},
            "engine": {"type": "string"},
            "account_id": {"type": "integer"},
            "aggressive": {"type": "boolean"},
            "max_notes": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "collection_report",
        "description": "采集任务完成后生成汇报：数量/负面数/Top3链接",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer", "description": "任务ID"}}}}},
    {"type": "function", "function": {
        "name": "collection_status",
        "description": "查询采集任务状态（不填 task_id 则查最近5个）",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer", "description": "任务ID"}}}}},
    {"type": "function", "function": {
        "name": "query_notes",
        "description": "多条件查询帖子",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"},
            "sentiment": {"type": "string", "enum": ["negative", "neutral", "positive"]},
            "keyword": {"type": "string"}, "days": {"type": "integer"},
            "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "sentiment_stats",
        "description": "情感统计（分布/负面占比/环比）",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"},
            "days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "hot_words",
        "description": "热词统计",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"},
            "days": {"type": "integer"}, "top_n": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "generate_brief",
        "description": "生成舆情简报（声量/情感/Top负面/一句话总结）",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"},
            "days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "export_csv",
        "description": "导出帖子为 CSV，返回下载链接",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"},
            "sentiment": {"type": "string", "enum": ["negative", "neutral", "positive"]},
            "days": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "negative_alerts",
        "description": "负面突增预警列表",
        "parameters": {"type": "object", "properties": {
            "topic_id": {"type": "integer"}, "topic_name": {"type": "string"}}}}},
]

# ---------------- 模式检测 ----------------

_mode_cache: Dict[str, Any] = {"ts": 0, "mode": "rule", "detail": "未检测"}


def _check_provider(name: str) -> Tuple[bool, str]:
    """轻量探测单个 provider 是否可用（只测 /v1/models 鉴权）。返回 (ok, detail)。

    注意：models 通不代表生成可用（如 Gemini 项目侧 403）。设置页"测试连接"
    请用 test_provider_deep 做完整生成测试。
    """
    cfg = get_ai_config()
    if name not in cfg["providers"]:
        return False, f"未知 provider {name}"
    p = cfg["providers"][name]
    if _is_placeholder(p["base_url"]) or _is_placeholder(p["api_key"]) or not p["model"]:
        return False, (f"{p['title']}：base_url/api_key 尚未配置，"
                       "请在设置页填写或切换 provider")
    try:
        from .netutil import http_client
        url = p["base_url"].rstrip("/") + "/models"
        with http_client(8.0) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {p['api_key']}"})
        if r.status_code < 400:
            return True, f"{p['title']} 连接正常（{p['model']}）"
        if r.status_code == 401:
            return False, f"{p['title']}：key 无效（401），请检查 key 是否正确"
        return False, f"{p['title']} 返回 {r.status_code}，可能是 key 无效"
    except Exception as e:  # noqa: BLE001
        return False, f"{p['title']} 不可达：{type(e).__name__}"


def test_provider_deep(name: str) -> Tuple[bool, str]:
    """深度测试：models 鉴权 + 最小生成调用，给出诚实结论。

    供设置页"测试连接"按钮使用（用户主动点击，非热路径）。
    """
    ok, detail = _check_provider(name)
    if not ok:
        return ok, detail
    cfg = get_ai_config()
    p = cfg["providers"][name]
    try:
        client, _ = _openai_client(name)
        resp = client.chat.completions.create(
            model=p["model"],
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1, temperature=0)
        _ = resp.choices[0].message.content
        return True, f"{p['title']} 连接正常（{p['model']}，生成测试通过）"
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "403" in msg or "denied" in msg.lower() or "permission" in msg.lower():
            if name == "gemini":
                return False, ("Gemini：key 有效，但 Google Cloud 项目侧拒绝了生成请求"
                               "（403）。请检查该项目的 API 配额/区域限制，或更换 key 后重试。")
            return False, f"{p['title']}：项目侧拒绝生成请求（403），请检查配额或更换 key"
        if "404" in msg or "not_found" in msg.lower() or "no longer available" in msg.lower():
            return False, (f"{p['title']}：模型 {p['model']} 不可用"
                            "（已下架/无权限），请在模型下拉中换一个")
        if "429" in msg or "rate_limit" in msg.lower():
            return False, f"{p['title']}：触发限流（429），稍后重试即可"
        return False, f"{p['title']}：生成测试失败（{type(e).__name__}）"


def _candidates() -> List[str]:
    """候选顺序：当前选中 provider 优先，其次按 AI_PROVIDER_PRIORITY 顺延"""
    from .config import AI_PROVIDER_PRIORITY
    active = get_ai_config()["active"]
    ordered = [active] + [p for p in AI_PROVIDER_PRIORITY if p != active]
    # 去重保序
    seen, out = set(), []
    for p in ordered:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def check_ai_ready(force: bool = False) -> Tuple[str, str]:
    """返回 (mode, detail)，mode ∈ ai/rule。60 秒缓存。

    自动顺延：当前 provider 不可用时按优先级尝试下一个，可用的即生效，
    并在 detail 中告知用户发生了顺延。
    """
    now = time.time()
    if not force and now - _mode_cache["ts"] < 60:
        return _mode_cache["mode"], _mode_cache["detail"]
    tried: List[str] = []
    mode, detail, used = "rule", "未配置", None
    for name in _candidates():
        ok, d = _check_provider(name)
        tried.append(f"{name}({'ok' if ok else 'fail'})")
        if ok:
            cfg = get_ai_config()
            active = cfg["active"]
            used = name
            if name != active:
                d = f"已自动顺延到可用 provider：{d}"
            mode, detail = "ai", d
            break
        detail = d  # 最后一个失败原因兜底
    if mode == "rule":
        detail = f"所有 provider 均不可用（{', '.join(tried)}），已降级为规则模式"
    p = get_ai_config()["providers"][used or get_ai_config()["active"]]
    _mode_cache.update(ts=now, mode=mode, detail=detail,
                       provider=used or get_ai_config()["active"],
                       provider_title=p["title"], model=p.get("model", ""))
    logger.info("AI 模式检测：%s（%s）", mode, detail)
    return mode, detail


def current_mode() -> Dict[str, Any]:
    mode, detail = check_ai_ready()
    return {"mode": mode, "detail": detail,
            "provider": _mode_cache.get("provider"),
            "provider_title": _mode_cache.get("provider_title"),
            "model": _mode_cache.get("model")}


# ---------------- AI 模式（OpenAI 协议 + function calling） ----------------

def _openai_client(provider_name: Optional[str] = None, http_client=None):
    from openai import OpenAI
    cfg = get_ai_config()
    name = provider_name or cfg["active"]
    p = cfg["providers"][name]
    if http_client is None:
        # 默认走代理兼容的 httpx 客户端（部分环境 NO_PROXY 含裸 IPv6 会导致
        # httpx 解析崩溃；netutil.http_client 只显式使用 http(s)_proxy）
        from .netutil import http_client as _make_client
        http_client = _make_client(AI_TIMEOUT_SEC)
    kw = {"base_url": p["base_url"], "api_key": p["api_key"],
          "timeout": AI_TIMEOUT_SEC, "http_client": http_client}
    return OpenAI(**kw), {"name": name, **p}


def _provider_cfg(name: str) -> Dict[str, Any]:
    cfg = get_ai_config()
    return {"name": name, **cfg["providers"][name]}


def _exec_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    fn = TOOLS.get(name)
    if not fn:
        return {"ok": False, "error": f"未知工具 {name}"}
    try:
        return fn(**(args or {}))
    except Exception as e:  # noqa: BLE001
        logger.exception("工具执行失败 %s", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def ai_chat(message: str, history: Optional[List[Dict[str, str]]] = None,
            http_client=None, provider_name: Optional[str] = None) -> Dict[str, Any]:
    """AI 模式对话。返回 {mode, reply, actions}。"""
    client, p = _openai_client(provider_name, http_client)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": _build_system_prompt()}]
    for h in history or []:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})
    actions: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None  # propose_collection 提案（前端渲染确认按钮）
    try:
        for _ in range(6):
            resp = client.chat.completions.create(
                model=p["model"], messages=messages,
                tools=TOOL_DEFS, tool_choice="auto")
            msg = resp.choices[0].message
            if not msg.tool_calls:
                reply = msg.content or "(模型未返回内容)"
                break
            messages.append(msg)
            for tc in msg.tool_calls:
                import json as _json
                args = _json.loads(tc.function.arguments or "{}")
                result = _exec_tool(tc.function.name, args)
                action = {"tool": tc.function.name, "args": args,
                          "result_summary": _summarize(tc.function.name, result)}
                # 前端需要的结构化字段（进度条/确认按钮用；不Dump全量result避免响应过大）
                for k in ("task_id", "topic_id", "confirmation_id"):
                    if result.get(k) is not None:
                        action[k] = result[k]
                actions.append(action)
                if (tc.function.name == "propose_collection"
                        and result.get("confirmation_id")):
                    pending = {"confirmation_id": result["confirmation_id"],
                               "summary": result.get("summary", ""),
                               "params": result.get("params", {})}
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": _json.dumps(result, ensure_ascii=False)})
        else:
            reply = "工具调用轮次过多，已停止。"
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 调用失败（%s），降级为规则模式", mask_key(p["api_key"]))
        return rule_chat(message, note=f"AI 调用失败（{type(e).__name__}），已降级为规则模式")
    # pending 已在工具调用循环内从 propose_collection 结果直接提取
    out = {"mode": "ai", "provider": p["name"], "provider_title": p["title"],
           "reply": reply, "actions": actions, "chips": _build_chips(),
           "shortcuts": _detect_shortcuts(message)}
    if pending:
        out["pending_confirmation"] = pending
    return out


def _summarize(tool: str, result: Dict[str, Any]) -> str:
    if tool == "start_collection":
        return (f"已启动采集任务 {result.get('task_id')}" if result.get("ok")
                else f"启动失败：{result.get('error')}")
    if tool == "export_csv":
        return (f"已导出 {result.get('rows')} 行：{result.get('download_url')}"
                if result.get("ok") else f"导出失败：{result.get('error')}")
    if tool == "generate_brief":
        return result.get("trend_summary", "")
    if tool == "negative_alerts":
        return result.get("summary", "")
    if tool == "sentiment_stats":
        d = result.get("sentiment_dist", {})
        return (f"负面 {d.get('negative', 0)} / 中性 {d.get('neutral', 0)} / "
                f"正面 {d.get('positive', 0)}，负面占比 {result.get('neg_ratio', 0):.0%}")
    if tool == "query_notes":
        return f"查到 {result.get('total', 0)} 条，返回前 {len(result.get('notes', []))} 条"
    if tool == "list_topics":
        return f"共 {len(result.get('topics', []))} 个话题"
    if tool == "hot_words":
        ws = result.get("hot_words", [])[:5]
        return "热词 Top5：" + "、".join(f"{w['word']}({w['count']})" for w in ws)
    if tool == "collection_status":
        ts = result.get("tasks") or ([result.get("task")] if result.get("task") else [])
        return "; ".join(f"任务{t['id']}:{t['status']}" for t in ts if t) or "无任务"
    if tool == "propose_collection":
        return ("已生成采集提案，待确认" if result.get("ok")
                else f"提案失败：{result.get('error')}")
    if tool == "collection_report":
        if not result.get("ok"):
            return result.get("error") or "任务尚未完成"
        return (f"共 {result.get('notes_total')} 条，负面 {result.get('negative_count')} 条"
                f"（{result.get('negative_ratio')}%)")
    return "ok"


# ---------------- 规则模式（兜底） ----------------

def _extract_topic_name(text: str) -> Optional[str]:
    t = re.sub(r"\s+", "", text)
    for topic in db.list_topics():
        name = re.sub(r"\s+", "", topic["name"])
        core = name.replace("负面舆情", "")
        if core and core in t:
            return topic["name"]
    return None


def _extract_days(text: str, default: int = 7) -> int:
    m = re.search(r"(\d+)\s*天", text)
    if m:
        return max(1, min(int(m.group(1)), 90))
    if "上周" in text or "近一周" in text or "一周" in text:
        return 7
    if "近一月" in text or "一个月" in text or "30天" in text:
        return 30
    if "今天" in text:
        return 1
    return default


def _extract_keywords(text: str) -> List[str]:
    """从采集指令里剥离出关键词（新主题用）。"""
    t = re.sub(r"\s+", "", text)
    t = re.sub(r"(采集|爬取|抓取|搜集|强攻|高频|模式|一下|帮我|请|"
               r"最近\d+天|近\d+天|上周|今天|舆情|帖子|笔记|的|：|:)", "", t)
    return [k for k in re.split(r"[,，、]", t) if k]


_CONFIRM_RE = r"(开始|确认|好的|可以|确定|是的|开|开始吧|确认开始)[！!。\s]*"
_CANCEL_RE = r"(取消|算了|不用了|放弃)[！!。\s]*"


def _fresh_pending() -> Optional[Dict[str, Any]]:
    """取最近一次未过期的采集提案。"""
    global _latest_pending_cid
    cid = _latest_pending_cid
    p = PENDING_COLLECTIONS.get(cid) if cid else None
    if not p or time.time() - p["created_at"] > PENDING_TTL_SEC:
        if cid:
            PENDING_COLLECTIONS.pop(cid, None)
        _latest_pending_cid = None
        return None
    return {"cid": cid, **p}


def rule_chat(message: str, note: str = "") -> Dict[str, Any]:
    """规则式指令解析。返回 {mode:'rule', reply, actions, pending_confirmation?}。"""
    global _latest_pending_cid
    text = message.strip()
    tname = _extract_topic_name(text)
    actions: List[Dict[str, Any]] = []

    def _act(tool, **kw):
        r = _exec_tool(tool, kw)
        action = {"tool": tool, "args": kw, "result_summary": _summarize(tool, r)}
        for k in ("task_id", "topic_id", "confirmation_id"):
            if r.get(k) is not None:
                action[k] = r[k]
        actions.append(action)
        return r

    reply = ""
    pending = None
    # 固定快捷指令（chips）→ 直接走确认流
    # 快捷 chips 动态映射：从话题名反查（通用化，不 hardcode 品牌）
    chip_map = {}
    for _t in db.list_topics()[:2]:
        chip_map[f"采{_t['name'][:8]}"] = {"topic_name": _t["name"]}
    # ---- 确认 / 取消口令（"开始"/"取消"纯文本，不依赖模型）----
    if re.fullmatch(_CONFIRM_RE, text):
        fp = _fresh_pending()
        if not fp:
            reply = "没有待确认的采集提案（可能已过期），请重新下达采集指令。"
        else:
            r = _act("start_collection", confirmation_id=fp["cid"])
            _latest_pending_cid = None
            if r.get("ok"):
                reply = (
                    f"✅ 采集任务已启动（任务 #{r['task_id']}，话题「{r['topic']}」"
                    f"{'，新话题已自动沉淀' if r.get('auto_created_topic') else ''}）\n"
                    f"账号：{r.get('account')}｜引擎：{r.get('engine')}")
            else:
                reply = f"❌ 启动失败：{r.get('error')}"
    elif re.fullmatch(_CANCEL_RE, text):
        fp = _fresh_pending()
        if fp:
            PENDING_COLLECTIONS.pop(fp["cid"], None)
            _latest_pending_cid = None
            reply = "已取消采集。"
        else:
            reply = "没有待确认的采集提案。"
    elif (chip_hit := next((v for k, v in chip_map.items() if k in text), None)):
        r = _act("propose_collection", **chip_hit)
        pending = {"confirmation_id": r["confirmation_id"],
                   "summary": r["summary"], "params": r["params"]}
        reply = (f"📋 {r['summary']}\n"
                 "回复「开始」确认启动，或「取消」放弃。")
    elif re.search(r"采集|爬取|抓取|搜集", text):
        # 通用采集指令 → 先提案确认（规则模式也一样，不直接启动）
        aggressive = bool(re.search(r"强攻|高频", text))
        kw_text = re.sub(r".*?(采集|爬取|抓取|搜集)", "", text).strip(" ：:，,、")
        kw_text = re.sub(r"(舆情|帖子|笔记|一下|帮我)$", "", kw_text).strip()
        r = None
        if tname and not kw_text:
            r = _act("propose_collection", topic_name=tname, aggressive=aggressive)
        elif kw_text:
            r = _act("propose_collection", keywords=kw_text, aggressive=aggressive)
        else:
            kws = _extract_keywords(text)
            if kws:
                r = _act("propose_collection", keywords="，".join(kws),
                         aggressive=aggressive)
        if r is None:
            reply = ("没听清要采什么🤔\n请告诉我关键词，例如「采集：瑞幸、库迪」。")
        elif r.get("ok"):
            pending = {"confirmation_id": r["confirmation_id"],
                       "summary": r["summary"], "params": r["params"]}
            reply = (f"📋 {r['summary']}\n"
                     "回复「开始」确认启动，或「取消」放弃。")
        else:
            reply = f"❌ {r.get('error', '无法解析采集意图')}"
    elif re.search(r"任务|状态|进度|怎么样", text):
        r = _act("collection_status")
        reply = "📋 " + _summarize("collection_status", r)
    elif re.search(r"导出|CSV|下载", text):
        sent = "negative" if re.search(r"负面", text) else None
        r = _act("export_csv", topic_name=tname, sentiment=sent,
                 days=_extract_days(text, 0) or None)
        reply = ("📥 " + _summarize("export_csv", r)
                 + (f"\n[点我下载]({r['download_url']})" if r.get("ok") else ""))
    elif re.search(r"预警|告警|突增|报警", text):
        r = _act("negative_alerts", topic_name=tname)
        lines = []
        for a in r["alerts"]:
            icon = "🔴" if a["alert"] else "🟢"
            lines.append(f"{icon} {a['topic_name']}：{a['message']}")
        reply = "🚨 负面预警\n" + "\n".join(lines)
    elif re.search(r"简报|总结|汇报|分析|报告", text):
        days = _extract_days(text)
        r = _act("generate_brief", topic_name=tname, days=days)
        reply = ("📊 舆情简报（近{}天{}）\n{}\n负面 {} / 中性 {} / 正面 {}\n".format(
            days, f"·{r.get('topic_name')}" if r.get("topic_name") else "",
            r.get("trend_summary", ""),
            r.get("sentiment_dist", {}).get("negative", 0),
            r.get("sentiment_dist", {}).get("neutral", 0),
            r.get("sentiment_dist", {}).get("positive", 0)))
        tops = r.get("top_negative", [])[:3]
        if tops:
            reply += "Top 负面：\n" + "\n".join(
                f"- {x['title'] or '(无标题)'}（赞{x['liked_count']} 评{x['comment_count']}）"
                for x in tops)
    elif re.search(r"热词|词云|关键词", text):
        r = _act("hot_words", topic_name=tname, days=_extract_days(text))
        ws = r.get("hot_words", [])[:10]
        reply = "🔥 热词 Top10：" + ("、".join(
            f"{w['word']}({w['count']})" for w in ws) if ws else "暂无数据")
    elif re.search(r"帖子|笔记", text):
        sent = ("negative" if re.search(r"负面", text)
                else "positive" if re.search(r"正面", text)
                else "neutral" if re.search(r"中性|中立", text) else None)
        days = _extract_days(text)
        r = _act("query_notes", topic_name=tname, sentiment=sent, days=days,
                 limit=10)
        items = r.get("notes", [])
        if items:
            lines = [f"🔍 查到 {r.get('total', 0)} 条"
                     f"（近{days}天{'·' + tname if tname else ''}）："]
            for n in items[:10]:
                title = (n.get("title") or "(无标题)")[:32]
                lines.append(f"- [{n.get('sentiment')}] {title}"
                             f"｜{n.get('author')}｜赞{n.get('liked_count')}")
            reply = "\n".join(lines)
        else:
            reply = f"🔍 近{days}天没有查到相关帖子。"
    elif re.search(r"话题|列表", text):
        r = _act("list_topics")
        reply = "📁 话题列表：\n" + "\n".join(
            f"- {t['name']}（id={t['id']}，引擎={t.get('engine')}）"
            for t in r.get("topics", []))
    else:
        reply = ("我能帮你：\n"
                 "• 采集：如「采集某品牌十五周年舆情」\n"
                 "• 分析/简报：如「分析一下最近7天负面舆情」\n"
                 "• 导出：如「导出上周负面帖子」\n"
                 "• 预警：如「现在有什么预警」\n"
                 "• 任务状态：如「采集任务怎么样了」")
    if note:
        reply = note + "\n" + reply
    p = get_active_provider()
    out = {"mode": "rule", "provider": p["name"], "provider_title": p["title"],
           "reply": reply, "actions": actions, "chips": _build_chips(),
           "shortcuts": _detect_shortcuts(message)}
    if pending:
        out["pending_confirmation"] = pending
    return out


def chat(message: str, history: Optional[List[Dict[str, str]]] = None,
         http_client=None, confirmation_id: Optional[str] = None,
         confirmed: Optional[bool] = None) -> Dict[str, Any]:
    """统一入口：按优先级逐个尝试可用 provider，全失败才降级规则模式。

    confirmation_id + confirmed：采集确认流的第二步（用户点确认/取消）。
    """
    if confirmation_id:
        if confirmed:
            r = _exec_tool("start_collection", {"confirmation_id": confirmation_id})
            if r.get("ok"):
                reply = (f"✅ 采集任务已启动（任务 #{r['task_id']}，话题「{r['topic']}」"
                         f"{'，新话题已自动沉淀' if r.get('auto_created_topic') else ''}）\n"
                         f"账号：{r.get('account')}｜引擎：{r.get('engine')}\n"
                         "进度可在本窗口用「任务状态」查询，完成后我会汇报结果摘要。")
                return {"mode": "rule", "reply": reply, "actions": [
                    {"tool": "start_collection",
                     "args": {"confirmation_id": confirmation_id},
                     "result_summary": f"任务 #{r['task_id']} 已启动",
                     "task_id": r["task_id"], "topic_id": r.get("topic_id")}],
                    "chips": _build_chips(), "task_id": r["task_id"],
                    "shortcuts": _detect_shortcuts(message)}
            return {"mode": "rule",
                    "reply": f"❌ 启动失败：{r.get('error')}",
                    "actions": [], "chips": _build_chips(),
                    "need_login": r.get("need_login", False),
                    "shortcuts": _detect_shortcuts(message)}
        else:
            PENDING_COLLECTIONS.pop(confirmation_id, None)
            return {"mode": "rule", "reply": "已取消采集。", "actions": [],
                    "chips": _build_chips(),
                    "shortcuts": _detect_shortcuts(message)}
    # 口令确认（AI 模式下"开始/取消"也直接走确认流，不依赖模型理解）
    _t = (message or "").strip()
    if _t and _fresh_pending():
        if re.fullmatch(_CONFIRM_RE, _t):
            return chat(message, history, http_client,
                        confirmation_id=_latest_pending_cid, confirmed=True)
        if re.fullmatch(_CANCEL_RE, _t):
            return chat(message, history, http_client,
                        confirmation_id=_latest_pending_cid, confirmed=False)
    mode, detail = check_ai_ready()
    if mode == "ai":
        tried = []
        for name in _candidates():
            ok, _ = _check_provider(name)
            if not ok:
                tried.append(name)
                continue
            try:
                res = ai_chat(message, history, http_client, provider_name=name)
                if name != get_ai_config()["active"]:
                    p = _provider_cfg(name)
                    res["notice"] = f"当前 provider 不可用，已自动顺延使用{p['title']}"
                return res
            except Exception as e:  # noqa: BLE001
                logger.warning("AI 调用失败（%s via %s），尝试下一个 provider",
                               type(e).__name__, name)
                tried.append(name)
                continue
        note = f"所有可用 provider 调用均失败（已试：{', '.join(tried)}），已降级为规则模式"
        return rule_chat(message, note=note)
    return rule_chat(message, note=detail if detail else "")
