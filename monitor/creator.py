# -*- coding: utf-8 -*-
"""内容创作中心：AI 驱动的运营文案生成（全开源，纯 prompt 工程）。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import ai_chat, db, trends
from .config import MONITOR_DIR

logger = logging.getLogger("xhs-monitor.creator")

PROMPTS_DIR = MONITOR_DIR / "prompts"

CREATION_TYPES = [
    {"id": "xiaohongshu_note", "name": "小红书图文笔记",
     "desc": "标题+正文+话题标签，种草口吻",
     "fields": [
         {"key": "theme", "label": "主题", "type": "text", "required": True,
          "placeholder": "如：十五周年庆探店攻略"},
         {"key": "selling_points", "label": "核心卖点", "type": "textarea",
          "required": True, "placeholder": "如：满1000返200、限定快闪店"},
         {"key": "tone", "label": "语气", "type": "select",
          "options": ["种草安利", "真实测评", "干货攻略", "探店vlog风"]},
         {"key": "length", "label": "正文字数", "type": "select",
          "options": ["300", "500", "800"]},
     ]},
    {"id": "short_video_script", "name": "短视频脚本",
     "desc": "口播稿/分镜表，可直接拍摄",
     "fields": [
         {"key": "theme", "label": "主题", "type": "text", "required": True},
         {"key": "selling_points", "label": "核心卖点", "type": "textarea",
          "required": True},
         {"key": "style", "label": "视频风格", "type": "select",
          "options": ["口播", "剧情", "卡点混剪", "探店实拍"]},
         {"key": "duration", "label": "目标时长", "type": "select",
          "options": ["30秒", "60秒", "90秒"]},
     ]},
    {"id": "event_post", "name": "活动推文",
     "desc": "公众号/小红书活动推文",
     "fields": [
         {"key": "theme", "label": "活动名称", "type": "text", "required": True},
         {"key": "selling_points", "label": "活动亮点", "type": "textarea",
          "required": True},
         {"key": "event_info", "label": "时间地点", "type": "text",
          "placeholder": "如：10月18日-11月17日，XX商场M层（示例）"},
         {"key": "tone", "label": "语气", "type": "select",
          "options": ["活泼网感", "高级质感", "亲民实在"]},
     ]},
    {"id": "poster_copy", "name": "海报文案",
     "desc": "主标题+副标题+行动指令",
     "fields": [
         {"key": "theme", "label": "主题", "type": "text", "required": True},
         {"key": "selling_points", "label": "核心信息", "type": "textarea",
          "required": True},
         {"key": "tone", "label": "风格", "type": "select",
          "options": ["大气简约", "促销有力", "文艺走心"]},
     ]},
    {"id": "live_preview", "name": "直播预告文案",
     "desc": "小红书/社群/朋友圈三版",
     "fields": [
         {"key": "theme", "label": "直播主题", "type": "text", "required": True},
         {"key": "selling_points", "label": "福利亮点", "type": "textarea",
          "required": True},
         {"key": "event_info", "label": "开播时间", "type": "text",
          "placeholder": "如：10月18日 20:00"},
         {"key": "tone", "label": "语气", "type": "select",
          "options": ["热情吆喝", "质感推荐"]},
     ]},
    {"id": "comment_reply", "name": "评论区回复话术",
     "desc": "3条不同角度回复，可直接复制",
     "fields": [
         {"key": "theme", "label": "用户评论原文", "type": "textarea",
          "required": True},
         {"key": "stance", "label": "回复立场", "type": "select",
          "options": ["感谢", "致歉", "解释说明", "引导转化"]},
         {"key": "tone", "label": "语气", "type": "select",
          "options": ["亲切", "专业", "幽默"]},
     ]},
]

_TYPE_MAP = {t["id"]: t for t in CREATION_TYPES}


def get_type(type_id: str) -> Dict[str, Any]:
    if type_id not in _TYPE_MAP:
        raise ValueError(f"未知创作类型: {type_id}")
    return _TYPE_MAP[type_id]


def load_template(type_id: str) -> str:
    p = PROMPTS_DIR / f"{type_id}.txt"
    if not p.exists():
        raise ValueError(f"prompt 模板缺失: {p.name}")
    text = p.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines).strip()


def render_prompt(type_id: str, inputs: Dict[str, str],
                  inspiration: str = "") -> str:
    tpl = load_template(type_id)
    # 品牌占位符（通用化：模板里的 {brand_name} 自动替换为当前品牌）
    from .site import get_brand
    brand = get_brand()
    ctx: Dict[str, str] = {"inspiration": inspiration or "",
                           "brand_name": brand["name"],
                           "brand_short": brand["short"]}
    for f in get_type(type_id)["fields"]:
        ctx[f["key"]] = (inputs.get(f["key"]) or "").strip()
    try:
        return tpl.format(**ctx)
    except KeyError as e:
        raise ValueError(f"模板缺变量 {e}，请检查 monitor/prompts/{type_id}.txt")


def inspiration_text(days: int = 7,
                     topic_id: Optional[int] = None) -> str:
    b = trends.build_briefing(topic_id, days)
    hot = [w["word"] for w in b["hot_words"][:8]]
    lines: List[str] = []
    if hot:
        lines.append("【近期热词】" + "、".join(hot))
    uw_sql, uw_params = db.uid_sql()
    with db._connect() as conn:
        pos = conn.execute(
            "SELECT title FROM notes WHERE sentiment='positive'"
            + (" AND topic_id=?" if topic_id else "")
            + uw_sql
            + " ORDER BY liked_count DESC LIMIT 3",
            ((topic_id,) if topic_id else ()) + tuple(uw_params)).fetchall()
        neg = conn.execute(
            "SELECT title FROM notes WHERE sentiment='negative'"
            + (" AND topic_id=?" if topic_id else "")
            + uw_sql
            + " ORDER BY liked_count DESC LIMIT 3",
            ((topic_id,) if topic_id else ()) + tuple(uw_params)).fetchall()
    if pos:
        lines.append("【用户正面评价】" + "；".join(p["title"][:40] for p in pos))
    if neg:
        lines.append("【用户吐槽槽点】" + "；".join(n["title"][:40] for n in neg))
    if not lines:
        return ""
    return "【舆情灵感输入】\n" + "\n".join(lines)


# 关键词 → 类型推断（仅内部路由参考，不强制用户选择模板）
_TYPE_KEYWORDS = {
    "xiaohongshu_note": ["图文", "笔记", "探店", "种草", "测评", "攻略"],
    "short_video_script": ["短视频", "脚本", "口播", "分镜", "vlog", "视频"],
    "event_post": ["活动", "推文", "公众号"],
    "poster_copy": ["海报", "主标题", "副标题"],
    "live_preview": ["直播", "预告", "开播"],
    "comment_reply": ["回复", "评论区", "话术"],
}


def _infer_type(request: str) -> Optional[str]:
    """从用户自然语言推断最接近的创作类型（仅内部参考，不强制用户选）。"""
    req = request or ""
    for type_id, kws in _TYPE_KEYWORDS.items():
        if any(k in req for k in kws):
            return type_id
    return None


def generate_free(request: str, inspiration: str = "") -> Dict[str, Any]:
    """自由创作：用户说要什么就直接写，不强制选模板、不强制填表单。

    request：用户自然语言，如"写个探店文案"、"帮我写条十五周年庆的推文"。
    内部会尝试匹配最接近的模板做结构参考，但不要求用户填任何字段；
    匹配不上就直接按 request 自由发挥。
    """
    type_id = _infer_type(request)
    template_hint = ""
    if type_id:
        try:
            template_hint = load_template(type_id)
        except ValueError:
            template_hint = ""
    mode, _ = ai_chat.check_ai_ready()
    if mode != "ai":
        return {"content": "（当前无可用 AI provider，请稍后重试）",
                "mode": "rule", "provider": None, "model": None,
                "inferred_type": type_id,
                "notice": "当前无可用 AI provider"}
    provider = ai_chat._mode_cache.get("provider")
    client, p = ai_chat._openai_client(provider)
    try:
        from .site import get_brand
        brand = get_brand()
        insp = inspiration or inspiration_text()
        system = (f"你是{brand['name']}官方运营文案。用户说什么，你就直接写成文，"
                  "不要反问、不要让用户选模板、不要列提纲。直接输出成品文案。")
        user_msg = f"用户需求：{request}\n"
        if template_hint:
            user_msg += f"\n【结构参考（仅参考，不要照搬格式）】\n{template_hint}\n"
        if insp:
            user_msg += f"\n{insp}\n"
        resp = client.chat.completions.create(
            model=p["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.8)
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("自由创作调用失败：%s", type(e).__name__)
        return {"content": "", "mode": "rule", "provider": None, "model": None,
                "inferred_type": type_id,
                "notice": "AI 调用失败（%s）" % type(e).__name__}
    if not content:
        raise RuntimeError("模型返回空内容，请重试")
    return {"content": content, "mode": "ai", "provider": p["name"],
            "model": p["model"], "inferred_type": type_id}


def _rule_draft(type_id: str, inputs: Dict[str, str]) -> str:
    t = get_type(type_id)
    parts = ["【规则模式初稿·" + t["name"] + "】（当前无可用 AI provider，"
             "以下为按模板生成的结构化初稿，请人工润色）", ""]
    for f in t["fields"]:
        v = (inputs.get(f["key"]) or "").strip()
        parts.append("■ " + f["label"] + "：" + (v or "（未填）"))
    parts += ["", "■ 正文初稿：", "（待 AI 生成或人工撰写）"]
    return "\n".join(parts)


def generate(type_id: str, inputs: Dict[str, str],
             inspiration: str = "") -> Dict[str, Any]:
    t = get_type(type_id)
    for f in t["fields"]:
        if f.get("required") and not (inputs.get(f["key"]) or "").strip():
            raise ValueError(f"请填写「{f['label']}」")
    prompt = render_prompt(type_id, inputs, inspiration)
    mode, _ = ai_chat.check_ai_ready()
    if mode != "ai":
        return {"content": _rule_draft(type_id, inputs), "mode": "rule",
                "provider": None, "model": None,
                "notice": "当前无可用 AI provider，已生成规则模式初稿"}
    provider = ai_chat._mode_cache.get("provider")
    client, p = ai_chat._openai_client(provider)
    try:
        # 品牌名从配置读取（通用化：换品牌改配置，不改代码）
        from .site import get_brand
        brand = get_brand()
        resp = client.chat.completions.create(
            model=p["model"],
            messages=[
                {"role": "system",
                 "content": f"你是{brand['name']}官方运营文案，按用户要求直接输出成文。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8)
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("创作生成调用失败：%s", type(e).__name__)
        return {"content": _rule_draft(type_id, inputs), "mode": "rule",
                "provider": None, "model": None,
                "notice": "AI 调用失败（%s），已生成规则模式初稿" % type(e).__name__}
    if not content:
        raise RuntimeError("模型返回空内容，请重试")
    return {"content": content, "mode": "ai", "provider": p["name"],
            "model": p["model"]}
