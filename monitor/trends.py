# -*- coding: utf-8 -*-
"""趋势统计：声量 / 情感 / 热词 / 互动 / 负面突增预警。

全部本地计算（SQLite + jieba），零 token 成本。
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import db
from .db import _connect

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

STOPWORDS = set("""
的 了 在 是 我 有 和 就 不 人 都 一 个 上 也 很 到 说 要 去 你 会 着 没 看 好 自己 这 那 之 前 后
与 或 但 而 其 于 被 把 将 并 从 对 比 更 最 太 又 再 可 以 呢 吗 吧 啊 哦 嗯 哈 啦 呀 嘛 么 得 地
什么 怎么 为什么 如何 多少 哪个 哪些 这种 那个 这个 这里 那里 时候 现在 今天 明天 昨天
可以 应该 需要 觉得 感觉 发现 真的 非常 特别 尤其 比较 有点 一些 一下 一起 已经 正在 曾经
如果 因为 所以 但是 然而 不过 虽然 还是 只是 都是 很多 很少 许多 非常 太 真 有点
就是 东西 事情 方面 问题 情况 时候 地方 朋友 大家 我们 你们 他们 它们 这样 那样 怎样 如何
进行 通过 作为 包括 关于 对于 由于 随着 除了 按照 根据 经过
演示 测试 示例 比如 例如
""".split())

# 字段权重：标题 > 正文 > 标签（标签词易污染，降权）
_FIELD_WEIGHTS = {"title": 3.0, "desc": 1.0, "tags": 0.5}

def _load_jieba():
    """加载 jieba + Cortech 自定义词典（单例）"""
    import jieba
    from pathlib import Path
    if not getattr(_load_jieba, "_done", False):
        dict_path = Path(__file__).parent / "cortech_dict.txt"
        if dict_path.exists():
            jieba.load_userdict(str(dict_path))
        _load_jieba._done = True
    return jieba


def _day_bounds(days: int, end_offset: int = 0):
    """返回 [start_ts, end_ts]（按天，end_offset=0 含今天）"""
    today = datetime.now(TZ).date()
    end = today - timedelta(days=end_offset)
    start = end - timedelta(days=days - 1)
    s = int(datetime(start.year, start.month, start.day, tzinfo=TZ).timestamp())
    e = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=TZ).timestamp())
    return s, e


def _periods(days: int, end_offset: int = 0) -> List[str]:
    today = datetime.now(TZ).date()
    end = today - timedelta(days=end_offset)
    return [str(end - timedelta(days=i)) for i in range(days - 1, -1, -1)]


def volume_trend(topic_id: Optional[int] = None, days: int = 30) -> List[Dict[str, Any]]:
    s, e = _day_bounds(days)
    where, params = ["publish_time>=?", "publish_time<=?"], [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    db.apply_uid_filter(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT date(publish_time,'unixepoch','+8 hours') d, COUNT(*) c FROM notes"
            f" WHERE {' AND '.join(where)} GROUP BY d", params).fetchall()
    m = {r["d"]: r["c"] for r in rows}
    return [{"period": d, "count": m.get(d, 0)} for d in _periods(days)]


def sentiment_trend(topic_id: Optional[int] = None, days: int = 30) -> List[Dict[str, Any]]:
    s, e = _day_bounds(days)
    where, params = ["publish_time>=?", "publish_time<=?"], [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    db.apply_uid_filter(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT date(publish_time,'unixepoch','+8 hours') d, sentiment, COUNT(*) c FROM notes"
            f" WHERE {' AND '.join(where)} GROUP BY d, sentiment", params).fetchall()
    m: Dict[str, Dict[str, int]] = {}
    for r in rows:
        m.setdefault(r["d"], {"negative": 0, "neutral": 0, "positive": 0})
        m[r["d"]][r["sentiment"]] = r["c"]
    out = []
    for d in _periods(days):
        v = m.get(d, {"negative": 0, "neutral": 0, "positive": 0})
        total = v["negative"] + v["neutral"] + v["positive"]
        out.append({"period": d, **v,
                    "neg_ratio": round(v["negative"] / total, 3) if total else 0.0})
    return out


def engagement_trend(topic_id: Optional[int] = None, days: int = 30) -> List[Dict[str, Any]]:
    s, e = _day_bounds(days)
    where, params = ["publish_time>=?", "publish_time<=?"], [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    db.apply_uid_filter(where, params)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT date(publish_time,'unixepoch','+8 hours') d, COUNT(*) c,"
            f" AVG(liked_count) al, AVG(comment_count) ac FROM notes"
            f" WHERE {' AND '.join(where)} GROUP BY d", params).fetchall()
    m = {r["d"]: r for r in rows}
    return [{"period": d,
             "avg_likes": round(m[d]["al"] or 0, 1) if d in m else 0,
             "avg_comments": round(m[d]["ac"] or 0, 1) if d in m else 0}
            for d in _periods(days)]


def _clean_words(text: str) -> List[str]:
    """jieba 分词 + 停用词/纯英文/单字过滤（用 cortech_dict.txt 自定义词典）。"""
    jieba = _load_jieba()
    out = []
    for w in jieba.cut(text):
        w = w.strip()
        if len(w) < 2 or w in STOPWORDS:
            continue
        if re.fullmatch(r"[0-9a-zA-Z\s]+", w):
            continue
        if not re.search(r"[\u4e00-\u9fff]", w):
            continue
        out.append(w)
    return out


def _fetch_note_texts(s: int, e: int, topic_id: Optional[int]):
    """取窗口内帖子的分词用字段（标题/正文/标签/互动）。"""
    where, params = ["publish_time>=?", "publish_time<=?"], [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    db.apply_uid_filter(where, params)
    with _connect() as conn:
        return conn.execute(
            f"""SELECT title, desc, tags,
                       COALESCE(liked_count,0)+COALESCE(collected_count,0)+COALESCE(comment_count,0)+COALESCE(share_count,0) AS eng
                FROM notes WHERE {' AND '.join(where)} LIMIT 5000""",
            params).fetchall()


def _weighted_freq(rows):
    """分词 + 字段加权 → (加权词频, 词→相关帖总互动)。"""
    weighted: Counter = Counter()
    eng_sum: Dict[str, int] = {}
    for r in rows:
        eng = r["eng"] or 0
        for field, weight in _FIELD_WEIGHTS.items():
            text = r[field] or ""
            if field == "tags":
                text = text.replace(",", " ")
            for w in _clean_words(text):
                weighted[w] += weight
                eng_sum[w] = eng_sum.get(w, 0) + eng
    return weighted, eng_sum


def hot_words(topic_id: Optional[int] = None, days: int = 30,
              top_n: int = 30, rank_type: str = "total") -> List[Dict[str, Any]]:
    """热词分析：jieba 分词 + 字段加权 + 互动加成 + 热度分。

    对标千瓜：
    - rank_type="total" 总量榜；"incremental" 增量榜（对比上一等长周期增量）。
    - 时间维度支持 7/30/90/365 天。
    对标新榜：热度分经对数标准化 score' = ln(score+1)/ln(均值+1)×1000，
    解决头部词碾压长尾词的问题（raw_score 保留原始分）。
    分数 = 加权词频 × (1 + log10(1 + 相关帖互动))。
    """
    import math
    s, e = _day_bounds(days)
    weighted, eng_sum = _weighted_freq(_fetch_note_texts(s, e, topic_id))

    if rank_type == "incremental":
        # 上一等长周期
        prev_s, prev_e = s - days * 86400, s - 1
        prev_weighted, _ = _weighted_freq(_fetch_note_texts(prev_s, prev_e, topic_id))
        base = {w: weighted[w] - prev_weighted.get(w, 0)
                for w in weighted if weighted[w] - prev_weighted.get(w, 0) > 0}
    else:
        base = dict(weighted)

    result = []
    for w, wf in sorted(base.items(), key=lambda x: x[1], reverse=True)[:top_n * 2]:
        raw = wf * (1 + math.log10(1 + eng_sum.get(w, 0)))
        result.append({"word": w, "count": int(wf), "raw_score": round(raw, 1),
                       "engagement": eng_sum.get(w, 0)})
    # 新榜式对数标准化
    vals = [r["raw_score"] for r in result]
    mean = sum(vals) / len(vals) if vals else 0
    for r in result:
        if mean > 0:
            r["score"] = round(math.log(r["raw_score"] + 1) / math.log(mean + 1) * 1000, 1)
        else:
            r["score"] = r["raw_score"]
    result.sort(key=lambda x: x["score"], reverse=True)
    return result[:top_n]


def _brand_cooccur(rows) -> List[Dict[str, Any]]:
    """关联品牌：竞品档案（含自有品牌）名在相关帖中的共现统计。
    对标千瓜 GetHotKeywordBrandStat。"""
    names: List[str] = []
    try:
        from . import competitors
        names += [c["name"] for c in competitors.list_competitors() if c.get("name")]
    except Exception:
        pass
    try:
        from .site import get_brand
        b = get_brand()
        names += [b.get("name"), b.get("short")]
    except Exception:
        pass
    names = [n for n in dict.fromkeys(names) if n]
    counts: Counter = Counter()
    for r in rows:
        text = f"{r['title'] or ''} {r['desc'] or ''} {r['tags'] or ''}"
        for n in names:
            if n in text:
                counts[n] += 1
    return [{"brand": n, "count": c} for n, c in counts.most_common(10)]


def hot_word_detail(word: str, topic_id: Optional[int] = None,
                    days: int = 30) -> Dict[str, Any]:
    """热词详情：关联品牌 + 关联达人。
    对标千瓜 GetHotKeywordBrandStat / GetHotKeyWordBloggerPage。"""
    s, e = _day_bounds(days)
    where, params = ["publish_time>=?", "publish_time<=?"], [s, e]
    if topic_id:
        where.append("topic_id=?"); params.append(topic_id)
    db.apply_uid_filter(where, params)
    like = f"%{word}%"
    where.append("(title LIKE ? OR desc LIKE ? OR tags LIKE ?)")
    params += [like] * 3
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT title, desc, tags, author, author_id, fans_count,
                       COALESCE(liked_count,0)+COALESCE(collected_count,0)+COALESCE(comment_count,0)+COALESCE(share_count,0) AS eng
                FROM notes WHERE {' AND '.join(where)} LIMIT 2000""",
            params).fetchall()

    influencers: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        aid = r["author_id"] or r["author"] or "unknown"
        d = influencers.setdefault(aid, {
            "author_id": aid, "nickname": r["author"] or "",
            "fans_count": r["fans_count"] or 0, "note_count": 0, "engagement": 0})
        d["note_count"] += 1
        d["engagement"] += r["eng"] or 0
    top_influencers = sorted(influencers.values(),
                             key=lambda x: x["engagement"], reverse=True)[:10]
    return {"word": word,
            "note_count": len(rows),
            "brands": _brand_cooccur(rows),
            "influencers": top_influencers}


def spike_alert(topic_id: Optional[int] = None) -> Dict[str, Any]:
    """负面突增预警：昨日负面数 > 过去7天均值2倍（且昨日>=5条）则标红"""
    s_prev, e_prev = _day_bounds(7, end_offset=2)   # 前7天（不含昨日今天）
    s_y, e_y = _day_bounds(1, end_offset=1)          # 昨日
    where_base, base_params = [], []
    if topic_id:
        where_base.append("topic_id=?"); base_params.append(topic_id)

    def _neg_count(s, e):
        where = where_base + ["publish_time>=?", "publish_time<=?", "sentiment='negative'"]
        with _connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM notes WHERE {' AND '.join(where)}",
                (*base_params, s, e)).fetchone()[0]

    y_neg = _neg_count(s_y, e_y)
    prev_counts = []
    for i in range(2, 9):
        si, ei = _day_bounds(1, end_offset=i)
        prev_counts.append(_neg_count(si, ei))
    avg7 = sum(prev_counts) / 7
    alert = (y_neg >= 5 and avg7 > 0 and y_neg > 2 * avg7) or (avg7 == 0 and y_neg >= 10)
    ratio = round(y_neg / avg7, 2) if avg7 > 0 else (float(y_neg) if y_neg else 0.0)
    return {
        "alert": alert,
        "yesterday": str((datetime.now(TZ).date() - timedelta(days=1))),
        "yesterday_neg": y_neg,
        "avg7_neg": round(avg7, 1),
        "ratio": ratio,
        "message": (f"⚠️ 负面突增预警：昨日负面 {y_neg} 条，"
                    f"为过去7天均值（{avg7:.1f}）的 {ratio} 倍" if alert
                    else f"昨日负面 {y_neg} 条，过去7天均值 {avg7:.1f} 条，无突增"),
    }


def get_trends(topic_id: Optional[int] = None, days: int = 30,
             rank_type: str = "total") -> Dict[str, Any]:
    return {
        "topic_id": topic_id,
        "days": days,
        "rank_type": rank_type,
        "volume": volume_trend(topic_id, days),
        "sentiment": sentiment_trend(topic_id, days),
        "engagement": engagement_trend(topic_id, days),
        "hot_words": hot_words(topic_id, days, rank_type=rank_type),
        "spike": spike_alert(topic_id),
    }


def build_briefing(topic_id: Optional[int] = None, days: int = 7) -> Dict[str, Any]:
    """舆情简报：声量/环比/情感分布/Top负面/热词/一句话趋势总结"""
    s, e = _day_bounds(days)
    s_prev, e_prev = _day_bounds(days, end_offset=days)
    where, base = ["publish_time>=?", "publish_time<=?"], [s, e]
    where_p, base_p = ["publish_time>=?", "publish_time<=?"], [s_prev, e_prev]
    db.apply_uid_filter(where, base)
    db.apply_uid_filter(where_p, base_p)
    tname = None
    if topic_id:
        where.append("topic_id=?"); base.append(topic_id)
        where_p.append("topic_id=?"); base_p.append(topic_id)
        t = db.get_topic(topic_id)
        tname = t["name"] if t else None
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM notes WHERE {' AND '.join(where)}", base).fetchone()[0]
        prev = conn.execute(
            f"SELECT COUNT(*) FROM notes WHERE {' AND '.join(where_p)}", base_p).fetchone()[0]
        dist = {}
        for label in ("negative", "neutral", "positive"):
            dist[label] = conn.execute(
                f"SELECT COUNT(*) FROM notes WHERE {' AND '.join(where)} AND sentiment=?",
                (*base, label)).fetchone()[0]
        top_neg = conn.execute(
            f"SELECT post_id, title, author, liked_count, comment_count, note_url, source_keyword"
            f" FROM notes WHERE {' AND '.join(where)} AND sentiment='negative'"
            f" ORDER BY (liked_count + comment_count * 3) DESC LIMIT 5", base).fetchall()
    neg_ratio = round(dist["negative"] / total, 3) if total else 0.0
    delta = (f"+{round((total - prev) / prev * 100, 1)}%" if prev
             else ("新增" if total else "持平"))
    if prev and total == prev:
        delta = "持平"
    summary = (f"近{days}天声量 {total} 条（环比{delta}）；"
               f"负面 {dist['negative']} 条，占比 {neg_ratio:.0%}；"
               f"{'负面占比较高，建议重点跟进 Top 负面帖文。' if neg_ratio >= 0.3 else '整体情感平稳。'}")
    spike = spike_alert(topic_id)
    if spike["alert"]:
        summary += spike["message"]
    # 全网资讯动态：当天 Top3 资讯 + 链接（纳入简报）
    news_items = db.list_news(days=1, page=1, page_size=3)["items"]
    news_top3 = [
        {"title": n["title"], "source": n["source"], "url": n["url"],
         "publish_time": n["publish_time"], "sentiment": n["sentiment"]}
        for n in news_items
    ]
    return {
        "topic_id": topic_id, "topic_name": tname, "days": days,
        "volume_total": total, "volume_prev": prev, "volume_delta": delta,
        "sentiment_dist": dist, "neg_ratio": neg_ratio,
        "top_negative": [dict(r) for r in top_neg],
        "hot_words": hot_words(topic_id, days, top_n=10),
        "spike": spike,
        "news_top3": news_top3,
        "trend_summary": summary,
    }


# 品牌词表（灰豚逆向：/hotword/detail/relatedBrand 关联品牌）
# 从 cortech_dict.txt 提取的真实品牌名，用于热词关联品牌分析
_BRAND_WORDS = (
    "路易威登", "LV", "香奈儿", "Chanel", "爱马仕", "Hermes", "迪奥", "Dior",
    "卡地亚", "Cartier", "劳力士", "Rolex", "古驰", "Gucci", "普拉达", "Prada",
    "巴黎世家", "Balenciaga", "圣罗兰", "YSL", "芬迪", "Fendi", "葆蝶家",
    "寇驰", "Coach", "阿玛尼", "Armani", "范思哲", "Versace", "巴宝莉", "Burberry",
    "蒂芙尼", "Tiffany", "宝格丽", "梵克雅宝", "欧米茄", "万宝龙",
    "周大福", "周生生", "老凤祥", "老铺黄金",
    "耐克", "Nike", "阿迪达斯", "Adidas", "新百伦", "彪马", "Puma",
    "斐乐", "Fila", "安踏", "李宁", "始祖鸟", "北面",
    "优衣库", "Uniqlo", "Zara", "无印良品", "MUJI",
    "泡泡玛特", "PopMart", "乐高", "LEGO", "迪士尼", "Disney",
    "星巴克", "Starbucks", "瑞幸", "喜茶", "奈雪", "霸王茶姬", "蜜雪冰城",
    "海底捞", "太二", "喜茶",
    "雅诗兰黛", "海蓝之谜", "兰蔻", "资生堂", "SK-II", "fresh", "馥蕾诗",
    "戴森", "Dyson", "苹果", "Apple", "华为", "小米",
    "宜家", "IKEA",
)


def related_brands(topic_id: Optional[int] = None, word: str = "",
                   days: int = 30, top_n: int = 10) -> List[Dict[str, Any]]:
    """热词关联品牌（灰豚逆向：/hotword/detail/relatedBrand）。

    找包含该热词的帖子，统计其中提及的品牌词频。
    """
    if not word:
        return []
    s, e = _day_bounds(days)
    where = ["publish_time>=?", "publish_time<=?",
             "(title LIKE ? OR desc LIKE ?)"]
    params: list = [s, e, f"%{word}%", f"%{word}%"]
    if topic_id:
        where.append("topic_id=?")
        params.append(topic_id)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT title, desc FROM notes WHERE {' AND '.join(where)} LIMIT 2000",
            params).fetchall()
    from collections import Counter
    counter: Counter = Counter()
    for r in rows:
        text = f"{r['title'] or ''} {r['desc'] or ''}"
        for b in _BRAND_WORDS:
            if b in text:
                counter[b] += 1
    return [{"brand": b, "count": c}
            for b, c in counter.most_common(top_n)]
