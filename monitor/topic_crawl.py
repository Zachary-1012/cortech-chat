# -*- coding: utf-8 -*-
"""话题一次性全量采集：Tag 扩展 → 20 个 tag 逐个搜索 → 详情+评论+作者 → 情感分析

用户要求：
- 正/中/负面全采集（不只是负面）
- 一个话题扩展 20 个 tag
- 从采集中自动发现新 tag
- 一次性把帖子、评论、所有维度采回来
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db


def _get_crawler():
    """懒加载 cortech_crawler（tag 扩展/发现不需要网络依赖）"""
    from . import cortech_crawler
    return cortech_crawler


# ============ Tag 扩展规则（通用，不 hardcode 品牌） ============

# 地域词前缀（按需选用，用户可在话题配置里改）
REGION_PREFIXES = ["广州", "深圳", "香港", "澳门", "上海", "北京"]

# 内容后缀（覆盖测评/吐槽/推荐等不同角度，保证正中负全采到）
SUFFIXES = ["测评", "吐槽", "推荐", "避雷", "攻略", "打卡", "探店",
            "排雷", "种草", "拔草", "合集", "分享",
            # 周年/活动类话题专用后缀
            "周年庆", "活动", "限定", "生日会", "优惠", "福利"]

# 中文数字 ↔ 阿拉伯数字（十五周年 ↔ 15周年）
NUMERAL_MAP = {
    "十五": "15", "十四": "14", "十三": "13", "十二": "12",
    "十一": "11", "十": "10", "九": "9", "八": "8",
    "七": "7", "六": "6", "五": "5", "四": "4",
    "三": "3", "二": "2", "一": "1",
}
# 反向映射（阿拉伯数字 → 中文数字）
NUMERAL_MAP_REV = {v: k for k, v in NUMERAL_MAP.items()}

# 同义词替换（按长度降序匹配，避免 十五生日 这种别扭结果）
SYNONYM_MAP = {
    "十五周年": ["生日", "15载"],
    "周年": ["生日", "载"],
    "周年庆": ["生日会"],
}


def _numeral_variants(text: str) -> List[str]:
    """生成数字变体：十五周年 → 15周年，反之亦然（只做最长匹配，避免 10五周年 这种垃圾）"""
    variants = []
    # 中文数字 → 阿拉伯数字：按长度降序，只取第一个命中的最长词
    for cn in sorted(NUMERAL_MAP.keys(), key=len, reverse=True):
        if cn in text:
            variants.append(text.replace(cn, NUMERAL_MAP[cn]))
            break  # 只做最长匹配
    # 阿拉伯数字 → 中文数字
    for ar in sorted(NUMERAL_MAP_REV.keys(), key=len, reverse=True):
        if re.search(rf"(?<![0-9]){ar}(?![0-9])", text):
            variants.append(re.sub(rf"(?<![0-9]){ar}(?![0-9])",
                                   NUMERAL_MAP_REV[ar], text))
            break
    return variants


def _synonym_variants(text: str) -> List[str]:
    """生成同义词变体（最长匹配优先）"""
    variants = []
    for word in sorted(SYNONYM_MAP.keys(), key=len, reverse=True):
        if word in text:
            for s in SYNONYM_MAP[word]:
                variants.append(text.replace(word, s))
            break  # 只做最长匹配
    return variants

# 常用繁简对照（话题词含简体时生成繁体变体）
TRAD_MAP = {
    "广": "廣", "门": "門", "购": "購", "发": "發", "达": "達",
    "优": "優", "惠": "惠", "龙": "龍", "凤": "鳳", "乐": "樂",
    "万": "萬", "国": "國", "场": "場", "广": "廣", "东": "東",
    "汇": "匯", "钟": "鍾", "发": "發", "现": "現", "实": "實",
}

# 中文停用词（tag 发现时过滤）
STOPWORDS = set(
    "的了是在有和与或但而于对就都也还这那你我他她它们个为以"
    "及等之其所能会可要从把被给让向到着过啊吗呢吧呀么嘛哈"
    "这个那个什么怎么为什么如何可以真的非常特别有点比较最"
    "一个一种一样一起一下一些一直一般不是没有不要不能不会"
    "时候现在今天明天昨天今年去年已经刚刚正在将要"
)


def _to_traditional(text: str) -> str:
    """简体转繁体（常用字映射，非全量）"""
    return "".join(TRAD_MAP.get(ch, ch) for ch in text)


def _split_keywords(keywords: str) -> List[str]:
    """话题 keywords 按逗号/顿号/空格切分"""
    parts = re.split(r"[,，、\s]+", keywords or "")
    return [p.strip() for p in parts if p.strip()]


def expand_topic_tags(topic_id: int, target: int = 20) -> List[str]:
    """话题基础关键词扩展到 target 个 tag。

    扩展策略（按优先级）：
    1. 基础关键词本身
    2. 基础词 + 后缀（测评/吐槽/推荐/避雷…，覆盖正中负）
    3. 地域词 + 基础词（话题名含地域时跳过）
    4. 繁体变体
    5. 基础词两两组合

    返回去重后的 tag 列表（最多 target 个），同时存入 topic_tags 表。
    """
    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")
    base = _split_keywords(topic.get("keywords") or "")
    if not base:
        base = [topic.get("name") or ""]

    tags: List[str] = []
    seen = set()

    def _add(t: str):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            tags.append(t)

    # 1. 基础词
    for b in base:
        _add(b)

    # 2. 数字变体（十五周年 ↔ 15周年）
    for b in list(base):
        if len(tags) >= target:
            break
        for v in _numeral_variants(b):
            _add(v)
            if len(tags) >= target:
                break

    # 3. 同义词变体（周年 → 生日/载）
    for b in list(base):
        if len(tags) >= target:
            break
        for v in _synonym_variants(b):
            _add(v)
            if len(tags) >= target:
                break

    # 4. 基础词 + 后缀（含数字变体 + 后缀的组合）
    # 后缀外循环、基础词内循环，保证每个后缀覆盖所有基础词
    _suffix_bases = list(base)
    for b in list(base):
        _suffix_bases.extend(_numeral_variants(b))
    # 优先保证正中负全覆盖的核心后缀先出来
    _priority_suffixes = ["测评", "吐槽", "推荐", "避雷", "攻略", "打卡",
                          "周年庆", "活动", "限定"]
    _ordered_suffixes = _priority_suffixes + [s for s in SUFFIXES
                                              if s not in _priority_suffixes]
    for sfx in _ordered_suffixes:
        for b in _suffix_bases:
            if len(tags) >= target:
                break
            _add(f"{b}{sfx}")
        if len(tags) >= target:
            break

    # 3. 地域词 + 基础词（如果基础词已含地域词则跳过）
    has_region = any(any(r in b for r in REGION_PREFIXES) for b in base)
    if not has_region:
        for b in base:
            for r in REGION_PREFIXES:
                if len(tags) >= target:
                    break
                _add(f"{r}{b}")
            if len(tags) >= target:
                break

    # 4. 繁体变体
    for b in list(base):
        if len(tags) >= target:
            break
        trad = _to_traditional(b)
        if trad != b:
            _add(trad)

    # 5. 基础词两两组合
    for i, b1 in enumerate(base):
        for b2 in base[i + 1:]:
            if len(tags) >= target:
                break
            _add(f"{b1}{b2}")
        if len(tags) >= target:
            break

    result = tags[:target]
    # 存入 topic_tags 表（kind='expanded'）
    db.save_topic_tags(topic_id, result, kind="expanded")
    return result


def _cut_words(text: str) -> List[str]:
    """分词：优先 jieba，不可用时降级为按字切分"""
    try:
        import jieba
        return [w.strip() for w in jieba.cut(text) if w.strip()]
    except ImportError:
        # 降级：按 2-4 字滑动窗口切分（简单但可用）
        chars = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
        words = []
        for n in (2, 3, 4):
            for i in range(len(chars) - n + 1):
                words.append(chars[i:i + n])
        return words


def discover_tags(topic_id: int, top_n: int = 10,
                  min_freq: int = 3) -> List[Dict[str, Any]]:
    """从已采集帖子中自动发现高频新词。

    流程：取该话题所有帖子标题+正文+标签 → 分词 → 过滤停用词/已有tag →
    按频率排序 → 返回 top_n 个候选。
    """
    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")

    notes = db.list_posts(topic_id, page_size=5000)["items"]
    if not notes:
        return []

    # 已有 tag（基础+扩展），发现时排除
    existing = set(_split_keywords(topic.get("keywords") or ""))
    existing.update(t["tag"] for t in db.list_topic_tags(topic_id))

    counter: Counter = Counter()
    for n in notes:
        title = n.get("title") or ""
        desc = n.get("desc") or ""
        # 过滤占位符（如"（标题未保留）"这类非真实内容）
        title = re.sub(r"[（(].*?未保留.*?[）)]", "", title)
        desc = re.sub(r"[（(].*?未保留.*?[）)]", "", desc)
        text = f"{title} {desc} {' '.join(n.get('tags') or [])}"
        for w in _cut_words(text):
            w = w.strip().lower()
            if len(w) < 2 or w in STOPWORDS:
                continue
            # 过滤纯占位词
            if w in ("标题", "保留", "未保留", "噪声"):
                continue
            # 排除已包含在现有 tag 中的词
            if any(w in e.lower() or e.lower() in w for e in existing):
                continue
            counter[w] += 1

    result = []
    for word, freq in counter.most_common(top_n * 2):
        if freq < min_freq:
            continue
        result.append({"tag": word, "freq": freq, "source": "auto_discovered"})
        if len(result) >= top_n:
            break

    # 存入 topic_tags 表（kind='discovered'，默认不启用，等用户确认）
    db.save_topic_tags(topic_id, [r["tag"] for r in result],
                       kind="discovered", enabled=False)
    return result


def get_crawl_tags(topic_id: int) -> List[str]:
    """获取该话题实际用于采集的 tag 列表（启用的 expanded + 用户确认的 discovered）"""
    tags = db.list_topic_tags(topic_id, enabled_only=True)
    if tags:
        return [t["tag"] for t in tags]
    # 没有扩展过，用基础关键词
    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")
    return _split_keywords(topic.get("keywords") or "")


def deep_crawl_topic(topic_id: int,
                     progress_cb: Optional[Callable[[int, str], None]] = None,
                     max_notes_per_tag: int = 50,
                     enable_comments: bool = True,
                     account_id: Optional[int] = None,
                     profile_dir: Optional[Path] = None) -> Dict[str, Any]:
    """一个话题一次性全量采集。

    流程：
    1. 取 20 个扩展 tag
    2. 逐个 tag 用 cortech_crawler 搜索，去重
    3. 每个帖子：详情 + 一级评论 + 二级评论 + 作者信息（crawler 已支持）
    4. 情感分析跑全量（正/中/负都保留入库）
    5. 进度实时回调

    注意：需要小红书登录态，无登录时抛 CortechCrawlerFailed。
    """
    from . import sentiment
    from .ingester import store_unified

    cortech_crawler = _get_crawler()
    from .cortech_crawler import CrawlContext, NoteData, CortechCrawlerFailed

    topic = db.get_topic(topic_id)
    if not topic:
        raise ValueError(f"话题不存在: {topic_id}")

    tags = get_crawl_tags(topic_id)
    if not tags:
        raise ValueError("话题没有可用的采集 tag，请先扩展 tag")

    def _log(msg: str):
        if progress_cb:
            progress_cb(-1, msg)

    crawler = cortech_crawler.get_crawler()
    ok, hint = crawler.check_login()
    if not ok:
        raise CortechCrawlerFailed(f"未登录：{hint}。请先在「账号管理」页扫码登录小红书。")

    all_notes: List[NoteData] = []
    all_comments = []
    seen_ids = set()
    seen_comment_ids = set()
    total_tags = len(tags)

    for i, tag in enumerate(tags):
        pct = int(5 + 85 * i / max(total_tags, 1))
        if progress_cb:
            progress_cb(pct, f"正在采集 tag「{tag}」（{i+1}/{total_tags}）")

        # 为每个 tag 建独立上下文复用 crawler 的单关键词采集
        ctx = CrawlContext(
            topic_id=topic_id,
            topic_name=topic.get("name") or "",
            keywords=[tag],
            max_notes=max_notes_per_tag,
            enable_comments=enable_comments,
            max_comments_per_note=50,
            run_dir=None,  # type: ignore
            task_id=0,
            timeout_sec=600,
            log=lambda s: _log(f"[tag:{tag}] {s}"),
            account_id=None if profile_dir else account_id,
            profile_dir=profile_dir,
        )
        try:
            result = crawler.crawl(ctx)
        except Exception as e:
            _log(f"tag「{tag}」采集失败，跳过：{e}")
            continue

        for note in result.notes:
            if note.post_id in seen_ids:
                continue
            seen_ids.add(note.post_id)
            note.source_keyword = tag
            all_notes.append(note)

        # 评论去重收集（一级+二级评论 crawler 已抓取）
        for c in result.comments:
            cid = getattr(c, "comment_id", "")
            if cid and cid not in seen_comment_ids:
                seen_comment_ids.add(cid)
                all_comments.append(c)

    if progress_cb:
        progress_cb(92, f"采集完成，共 {len(all_notes)} 条去重帖子，正在情感分析…")

    # 情感分析跑全量（正/中/负都保留）
    pos = neu = neg = 0
    for note in all_notes:
        text = f"{note.title} {note.desc}"
        label, _ = sentiment.analyze(text)
        note.sentiment = label  # type: ignore
        if label == "positive":
            pos += 1
        elif label == "negative":
            neg += 1
        else:
            neu += 1

    if progress_cb:
        progress_cb(96, "正在入库…")

    # 入库（复用统一入库，帖子+评论一起）
    stored_notes, stored_comments = store_unified(
        topic_id, all_notes, all_comments, account_id=account_id,
        source="browser_use_profile" if profile_dir else "account")

    if progress_cb:
        progress_cb(100, "全量采集完成")

    return {
        "topic_id": topic_id,
        "tags_used": tags,
        "tags_count": len(tags),
        "notes_total": len(all_notes),
        "notes_stored": stored_notes,
        "comments_total": len(all_comments),
        "comments_stored": stored_comments,
        "sentiment": {"positive": pos, "neutral": neu, "negative": neg},
    }
