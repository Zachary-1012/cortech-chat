# -*- coding: utf-8 -*-
"""AI 功能知识库：产品所有功能的清单，供 AI 理解自然语言、给出快捷入口。

每个功能：name / route / keywords / desc / usage。
AI 用它做意图→功能映射；前端用 route 做可点击跳转。
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 产品信息
PRODUCT_NAME = "Chat Cortech"
PRODUCT_TAGLINE = "通用社媒运营 SaaS：舆情监控、内容创作、品牌保护，一站式搞定"

# 功能清单（route 以前端路由为准）
FEATURES: List[Dict] = [
    {
        "id": "overview",
        "name": "总览仪表盘",
        "route": "/overview",
        "keywords": ["总览", "仪表盘", "首页", "概览", "dashboard"],
        "desc": "所有话题的声量、情感、预警一眼看完",
        "usage": "每天上班先看总览，了解整体舆情",
    },
    {
        "id": "topics",
        "name": "话题管理",
        "route": "/yuqing/topics",
        "keywords": ["话题", "关键词", "监控话题", "新建话题", "话题管理", "topic"],
        "desc": "配置要监控的话题和关键词，如「广州太古汇十五周年」",
        "usage": "想监控一个新话题时，先来这里建话题、配关键词",
    },
    {
        "id": "tasks",
        "name": "采集任务",
        "route": "/yuqing/tasks",
        "keywords": ["采集任务", "任务", "采集进度", "任务状态", "爬取任务"],
        "desc": "查看采集任务的进度和状态",
        "usage": "采集跑起来后，来这里看进度",
    },
    {
        "id": "notes",
        "name": "帖子列表",
        "route": "/yuqing/posts",
        "keywords": ["帖子", "笔记", "帖子列表", "内容", "帖子详情"],
        "desc": "采集回来的帖子列表，可筛选情感、排序",
        "usage": "看采集到了哪些帖子，筛负面帖子",
    },
    {
        "id": "timeline",
        "name": "时间线",
        "route": "/yuqing/posts",
        "route_hint": "在帖子列表页选话题后，点「时间线」Tab",
        "keywords": ["时间线", "趋势", "演变", "时间轴", "声量趋势", "timeline"],
        "desc": "按发布时间看舆情演变：趋势图→帖子列表→详情三层下钻",
        "usage": "想看一个话题的舆情是怎么发展起来的，负面什么时候爆发的",
    },
    {
        "id": "deep_crawl",
        "name": "一键全量采集",
        "route": "/yuqing/topics",
        "route_hint": "在话题管理页，对话题点「一键采集」",
        "keywords": ["一键采集", "全量采集", "深度采集", "20个tag", "tag扩展", "采集全部"],
        "desc": "一个话题扩展 20 个 tag，一次性采回帖子+评论+作者全维度",
        "usage": "对我直接说「帮我采集广州太古汇十五周年」，我来办",
    },
    {
        "id": "hotwords",
        "name": "热词分析",
        "route": "/yuqing/hotwords",
        "keywords": ["热词", "关键词云", "词云", "热词榜", "增量榜", "总量榜"],
        "desc": "热词榜（总量/增量）、关联品牌、关联达人",
        "usage": "看大家在聊什么词，哪个词在涨",
    },
    {
        "id": "influencers",
        "name": "达人库",
        "route": "/influencers",
        "keywords": ["达人", "博主", "KOL", "KOC", "达人库", "影响力"],
        "desc": "达人管理、影响力分、相似达人推荐",
        "usage": "找合作达人，看谁的影响力高",
    },
    {
        "id": "events",
        "name": "事件聚类",
        "route": "/events",
        "keywords": ["事件", "聚类", "事件分析", "cluster", "自动归类"],
        "desc": "多篇帖子自动聚成事件，告别一条条翻",
        "usage": "帖子太多看不过来时，看事件级汇总",
    },
    {
        "id": "propagation",
        "name": "传播路径图谱",
        "route": "/events",
        "route_hint": "在事件详情页，点「传播路径」",
        "keywords": ["传播路径", "传播图谱", "源头", "放大器", "桥梁", "传播趋势"],
        "desc": "三层结构：传播趋势时间线→源头/放大器/桥梁关键节点→人群画像",
        "usage": "想知道一次舆情是怎么传开的，谁是源头、谁在放大",
    },
    {
        "id": "competitors",
        "name": "竞品对标",
        "route": "/competitors",
        "keywords": ["竞品", "对标", "对比", "SOV", "声量份额"],
        "desc": "和竞品横向对比声量、情感、热词",
        "usage": "想知道我们和竞品比怎么样",
    },
    {
        "id": "alerts",
        "name": "预警中心",
        "route": "/alerts",
        "keywords": ["预警", "报警", "负面预警", "提醒", "alert"],
        "desc": "负面突增、声量异常预警，三层综合预警",
        "usage": "设置关键词，负面一冒头就提醒你",
    },
    {
        "id": "fakecheck",
        "name": "伪冒反查",
        "route": "/fakecheck",
        "keywords": ["伪冒", "假冒", "反查", "sweep", "山寨", "仿冒", "举报"],
        "desc": "扫出冒用品牌名的账号，关键词矩阵+定时巡检+一键生成举报材料",
        "usage": "查有没有人冒充你们品牌，对我说「帮我查伪冒」就行",
    },
    {
        "id": "creator",
        "name": "内容创作中心",
        "route": "/creator",
        "keywords": ["创作", "文案", "写文案", "脚本", "海报", "推文", "内容生成"],
        "desc": "写探店文案、短视频脚本、活动推文，敏感词检测",
        "usage": "直接跟我说「写个探店文案」，不用选模板",
    },
    {
        "id": "bids",
        "name": "方案比稿",
        "route": "/bids",
        "keywords": ["比稿", "方案", "供应商", "PDF", "评审"],
        "desc": "上传供应商方案 PDF，AI 拆解打分横向对比",
        "usage": "收到几家供应商方案，传上来我帮你比",
    },
    {
        "id": "news",
        "name": "全网资讯",
        "route": "/news",
        "keywords": ["新闻", "资讯", "全网", "媒体报道"],
        "desc": "新闻+RSS 全网资讯时间线",
        "usage": "除了小红书，想看全网媒体怎么说",
    },
    {
        "id": "chat",
        "name": "AI 聊天",
        "route": "/chat",
        "keywords": ["聊天", "问答", "AI", "助手"],
        "desc": "就是我，自然语言下指令",
        "usage": "你现在就在用",
    },
]


def match_features(text: str, top_n: int = 3) -> List[Dict]:
    """按关键词匹配功能，返回命中的功能列表（按命中词数排序）。"""
    t = (text or "").lower()
    scored = []
    for f in FEATURES:
        hits = [kw for kw in f["keywords"] if kw.lower() in t]
        if hits:
            scored.append((len(hits), f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:top_n]]


def get_feature(feature_id: str) -> Optional[Dict]:
    for f in FEATURES:
        if f["id"] == feature_id:
            return f
    return None


def knowledge_text() -> str:
    """给 LLM 系统提示词用的功能清单文本。"""
    lines = [f"你是 {PRODUCT_NAME}（{PRODUCT_TAGLINE}）的 AI 助手。",
             "产品全部功能："]
    for f in FEATURES:
        hint = f.get("route_hint") or ""
        lines.append(
            f"- {f['name']}（{f['route']}）：{f['desc']}。{hint}"
        )
    return "\n".join(lines)


def to_shortcuts(features: List[Dict]) -> List[Dict]:
    """功能 → 快捷入口结构。"""
    out = []
    for f in features:
        out.append({
            "label": f["name"],
            "route": f["route"],
            "hint": f.get("route_hint") or "",
            "desc": f["desc"],
        })
    return out
