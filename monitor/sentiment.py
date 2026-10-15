# -*- coding: utf-8 -*-
"""情感分析：SnowNLP 情感分 + 负面词库综合判定"""
from __future__ import annotations

from functools import lru_cache
from typing import Tuple

from .config import NEGATIVE_WORDS_FILE


@lru_cache(maxsize=1)
def _load_negative_words() -> Tuple[str, ...]:
    words = []
    try:
        for line in NEGATIVE_WORDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    except FileNotFoundError:
        pass
    # 强负面词：命中即直接判 negative
    return tuple(sorted(words, key=len, reverse=True))


@lru_cache(maxsize=1)
def _snownlp():
    from snownlp import SnowNLP
    return SnowNLP


def analyze(text: str) -> Tuple[str, float]:
    """返回 (label, score)：label ∈ negative/neutral/positive，score 为 0~1 情感分。"""
    text = (text or "").strip()
    if not text:
        return "neutral", 0.5

    for w in _load_negative_words():
        if w and w in text:
            return "negative", 0.15

    try:
        score = float(_snownlp()(text).sentiments)
    except Exception:
        return "neutral", 0.5

    if score < 0.4:
        return "negative", score
    if score > 0.6:
        return "positive", score
    return "neutral", score


# ---------------- 细分类（灰豚逆向：评论情感细分类对齐） ----------------
# 规则版：先做词典规则，后续可换模型。返回 (fine_label, 粗分类)

# 愤怒：强烈不满、谩骂
_ANGER_WORDS = (
    "气死", "愤怒", "恶心", "垃圾", "去死", "滚", "傻", "蠢", "脑残",
    "无语", "服了", "离谱", "逆天", "什么鬼", "有病", "神经病",
    "投诉", "维权", "曝光", "拉黑", "取关", "举报",
)
# 讽刺/阴阳怪气：反话、正话反说
_SARCASM_WORDS = (
    "呵呵", "哈哈哈", "笑死", "真棒", "真厉害", "好样的", "长见识了",
    "学到了", "谢谢你", "大聪明", "人才", "绝了", "开眼了",
    "阴阳", "怪气", "反话", "你说得对",
)
# 失望：期望落空
_DISAPPOINT_WORDS = (
    "失望", "可惜", "没想到", "以为", "还以为", "白去了", "白买了",
    "踩雷", "翻车", "不如", "退步", "变差", "以前好", "再也不",
    "不会再", "最后一次", "取关了",
)
# 抱怨：具体不满陈述
_COMPLAIN_WORDS = (
    "排队", "太久", "太贵", "难吃", "服务差", "态度差", "不推荐",
    "避雷", "劝退", "坑", "贵", "吵", "脏", "乱", "差",
    "等了", "没吃到", "售罄", "没货", "涨价",
)

# 细分类 -> 中文名
FINE_LABELS = {
    "anger": "愤怒",
    "sarcasm": "讽刺",
    "disappointment": "失望",
    "complaint": "抱怨",
    "negative_other": "负面其他",
    "positive": "正面",
    "neutral": "中性",
}


def analyze_fine(text: str) -> Tuple[str, str]:
    """细分类情感：返回 (fine_label, coarse_label)。

    fine_label ∈ anger/sarcasm/disappointment/complaint/negative_other/positive/neutral
    规则版：按词典命中顺序判定（愤怒 > 讽刺 > 失望 > 抱怨）。
    """
    text = (text or "").strip()
    if not text:
        return "neutral", "neutral"
    coarse, _ = analyze(text)
    if coarse != "negative":
        return coarse, coarse
    # 负面才做细分类
    for w in _ANGER_WORDS:
        if w in text:
            return "anger", "negative"
    for w in _SARCASM_WORDS:
        if w in text:
            return "sarcasm", "negative"
    for w in _COMPLAIN_WORDS:
        if w in text:
            return "complaint", "negative"
    for w in _DISAPPOINT_WORDS:
        if w in text:
            return "disappointment", "negative"
    return "negative_other", "negative"
