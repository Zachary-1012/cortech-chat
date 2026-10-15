# -*- coding: utf-8 -*-
"""敏感词检测（灰豚逆向：/tools/sensitive_words）。

内容创作发布前检查：广告法违禁词 + 小红书平台敏感词。
规则版：词典匹配，返回命中词及位置。
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Tuple

# 广告法违禁词（极限词）
_AD_LAW_WORDS = (
    # 最高级
    "国家级", "世界级", "最高级", "最佳", "最大", "第一", "唯一", "首个",
    "首选", "顶级", "极致", "顶尖", "终极", "之王", "之最", "之冠",
    # 绝对化
    "绝对", "百分百", "100%", "零风险", "永不", "永远", "彻底",
    "全面", "完全", "彻底根除",
    # 疗效承诺（医疗/保健）
    "根治", "治愈", "痊愈", "特效", "神效", "包治百病", "药到病除",
    "无效退款", "无副作用",
    # 财富承诺
    "一夜暴富", "保证收益", "稳赚不赔", "零风险投资", "保本",
    # 身份冒用
    "国家机关", "国家工作人员", "领导人",
)

# 小红书平台敏感词
_XHS_SENSITIVE_WORDS = (
    # 导流
    "微信", "加微", "VX", "vx", "私聊下单", "站外交易", "淘宝搜",
    "拼多多", "闲鱼", "转转",
    # 医疗夸大
    "美白针", "溶脂针", "私密整形", "换头",
    # 封建迷信
    "算命", "改运", "转运", "招财", "开光",
    # 违禁品暗示
    "烟", "电子烟", "代购处方药",
    # 色情擦边
    "约", "援交", "裸聊",
    # 政治敏感
    "游行", "示威", "罢工",
)

# 分类 -> 中文名
CATEGORIES = {
    "ad_law": "广告法违禁",
    "xhs": "平台敏感",
}


@lru_cache(maxsize=1)
def _word_index() -> List[Tuple[str, str]]:
    """(词, 分类)，按词长降序，命中长词优先。"""
    pairs = [(w, "ad_law") for w in _AD_LAW_WORDS]
    pairs += [(w, "xhs") for w in _XHS_SENSITIVE_WORDS]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def check(text: str) -> Dict[str, object]:
    """检测文本中的敏感词。

    返回 {"hits": [{"word": 词, "category": 分类, "category_name": 中文名, "pos": 位置}],
           "has_hit": bool, "count": int}
    """
    text = text or ""
    hits: List[Dict[str, object]] = []
    for word, cat in _word_index():
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            hits.append({
                "word": word,
                "category": cat,
                "category_name": CATEGORIES[cat],
                "pos": idx,
            })
            start = idx + len(word)
    hits.sort(key=lambda h: h["pos"])
    return {"hits": hits, "has_hit": bool(hits), "count": len(hits)}


def highlight(text: str) -> str:
    """把敏感词标红（HTML），用于前端预览。"""
    text = text or ""
    result = check(text)
    if not result["has_hit"]:
        return text
    # 从后往前替换，避免位置偏移
    out = text
    for h in sorted(result["hits"], key=lambda x: x["pos"], reverse=True):
        w = h["word"]
        pos = h["pos"]
        out = (out[:pos]
               + f'<span class="cortech-sensitive">{w}</span>'
               + out[pos + len(w):])
    return out
