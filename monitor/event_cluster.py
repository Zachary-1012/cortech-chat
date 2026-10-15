# -*- coding: utf-8 -*-
"""Cortech 事件聚类引擎：多篇帖子自动聚成事件。

方案评估结论（2026-10-15）：
- BERTopic：需 torch/transformers/sentence-transformers/umap/hdbscan + 数百 MB
  模型权重，总量 >1GB，用户本地便携包不可行 → 弃用。
- TF-IDF + KMeans(sklearn)：需新增 numpy/scipy/sklearn（~50MB wheels），
  且 K 需预先指定，事件发现场景不合适 → 弃用。
- TF-IDF + jieba + 余弦相似度 + 并查集连通分量：仅 stdlib + jieba
  （jieba 已是产品依赖），零新增依赖，无需预设 K，阈值可调 → 采用。

本模块是纯聚类核心，不依赖 db/config/ai_chat，可独立测试；
monitor/events.py 的落库流程可调用本模块（保持兼容）。
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

TZ = timezone(timedelta(hours=8))

# 阈值按数据量自适应：n 越小 idf 区分度越压缩，可达到的余弦相似度整体偏低。
# 50 篇模拟数据验证：n>=30 时 0.45 核心阈值达到 100% 纯度；小 n 需放宽。
def _adaptive_thresholds(n: int,
                         core: Optional[float],
                         assign: Optional[float]
                         ) -> Tuple[float, float]:
    if core is None or assign is None:
        if n >= 30:
            auto_core, auto_assign = 0.45, 0.30
        elif n >= 10:
            auto_core, auto_assign = 0.40, 0.27
        else:
            auto_core, auto_assign = 0.35, 0.25
        core = auto_core if core is None else core
        assign = auto_assign if assign is None else assign
    return core, assign


# 默认传 None 即启用按量自适应；显式传值则覆盖
DEFAULT_CORE_THRESHOLD: Optional[float] = None
DEFAULT_ASSIGN_THRESHOLD: Optional[float] = None
# 小于此规模的核心簇直接丢弃（噪声）
DEFAULT_MIN_CLUSTER = 3

_STOPWORDS = frozenset(
    "的了是在和有我你他她它这那都就也而与或但为对把被给让向从到比以及"
    "啊呀呢吧吗哈哇哦嗯呃这个那个什么怎么为什么如何可以能够应该非常"
    "特别真的太好很最更比较有点一些一种一样一起于是然后但是所以如果"
    "因为虽然尽管即使但是可是不过然而因此于是便就才都全每各某其此该"
)


def _tokenize(text: str) -> List[str]:
    """分词：优先 jieba；jieba 不可用时降级为字符 2-gram，保证零依赖可跑。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        import jieba

        toks = []
        for w in jieba.cut(text):
            w = w.strip()
            if len(w) < 2 or w.isdigit() or w in _STOPWORDS:
                continue
            toks.append(w)
        if toks:
            return toks
    except Exception:  # noqa: BLE001
        pass
    # 降级：字符 2-gram
    chars = [c for c in text if not c.isspace() and c not in _STOPWORDS]
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)
            if len(chars[i] + chars[i + 1]) == 2]


def _tfidf_vectors(docs: List[List[str]]
                   ) -> Tuple[List[Dict[str, float]], List[float], Dict[str, float]]:
    n = len(docs)
    df: Counter = Counter()
    for toks in docs:
        df.update(set(toks))
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vecs, norms = [], []
    for toks in docs:
        tf = Counter(toks)
        total = len(toks) or 1
        v = {t: (c / total) * idf[t] for t, c in tf.items()}
        vecs.append(v)
        norms.append(math.sqrt(sum(x * x for x in v.values())) or 1.0)
    return vecs, norms, idf


def _union_find_cluster(vecs: List[Dict[str, float]], norms: List[float],
                        threshold: float) -> List[List[int]]:
    """并查集聚类：余弦相似度 >= 阈值的帖子对连边，取连通分量。

    倒排索引预过滤：无公共词的帖子对余弦必为 0，直接跳过。
    """
    n = len(vecs)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    posting: Dict[str, List[int]] = defaultdict(list)
    for i, v in enumerate(vecs):
        for t in v:
            posting[t].append(i)
    seen = set()
    for lst in posting.values():
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                a, b = (lst[x], lst[y]) if lst[x] < lst[y] else (lst[y], lst[x])
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                va, vb = vecs[a], vecs[b]
                # 只在交集词上求点积
                if len(va) > len(vb):
                    va, vb = vb, va
                dot = sum(val * vb[t] for t, val in va.items() if t in vb)
                if dot >= threshold * norms[a] * norms[b]:
                    union(a, b)
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _top_keywords(member_docs: List[List[str]], idf: Dict[str, float],
                  top_n: int = 6) -> List[str]:
    agg: Counter = Counter()
    for toks in member_docs:
        tf = Counter(toks)
        total = len(toks) or 1
        for t, c in tf.items():
            agg[t] += (c / total) * idf.get(t, 1.0)
    return [w for w, _ in agg.most_common(top_n)]


def _representative(group: List[int], vecs: List[Dict[str, float]],
                    norms: List[float]) -> int:
    """簇内 medoid：与簇内其他成员平均余弦相似度最高的帖子。"""
    best_i, best_s = group[0], -1.0
    for i in group:
        s = 0.0
        vi = vecs[i]
        for j in group:
            if i == j:
                continue
            vj = vecs[j]
            small, big = (vi, vj) if len(vi) <= len(vj) else (vj, vi)
            dot = sum(v * big[t] for t, v in small.items() if t in big)
            s += dot / (norms[i] * norms[j])
        s /= max(len(group) - 1, 1)
        if s > best_s:
            best_s, best_i = s, i
    return best_i


def _cosine(va: Dict[str, float], na: float,
            vb: Dict[str, float], nb: float) -> float:
    if na <= 0 or nb <= 0:
        return 0.0
    small, big = (va, vb) if len(va) <= len(vb) else (vb, va)
    dot = sum(v * big[t] for t, v in small.items() if t in big)
    return dot / (na * nb)


def _centroid(idxs: List[int], vecs: List[Dict[str, float]]
              ) -> Tuple[Dict[str, float], float]:
    agg: Counter = Counter()
    for i in idxs:
        for t, v in vecs[i].items():
            agg[t] += v
    n = len(idxs) or 1
    c = {t: v / n for t, v in agg.items()}
    norm = math.sqrt(sum(x * x for x in c.values())) or 1.0
    return c, norm


def cluster_notes(notes: List[Dict[str, Any]],
                  core_threshold: Optional[float] = DEFAULT_CORE_THRESHOLD,
                  assign_threshold: Optional[float] = DEFAULT_ASSIGN_THRESHOLD,
                  min_cluster: int = DEFAULT_MIN_CLUSTER,
                  max_events: int = 30) -> Dict[str, Any]:
    """对帖子列表做事件聚类（两阶段）。

    阶段一：用严格阈值做并查集连通分量，形成高纯度核心簇。
    阶段二：未入簇帖子计算与各核心簇质心的余弦相似度，
            >= 归属阈值则归入最相似簇，否则记为噪声。

    core_threshold/assign_threshold 传 None 时按数据量自适应
    （n>=30: 0.45/0.30；10<=n<30: 0.40/0.27；n<10: 0.35/0.25）。

    notes 每项至少含：post_id, title, desc；可选 publish_time(秒时间戳),
    sentiment(negative/neutral/positive), liked_count, comment_count。

    返回 {"ok", "events": [...], "notes": N, "clustered": M, "noise": K}。
    每个 event 含：title, keywords, note_ids, note_count, representative
    （代表帖 post_id + 标题）, sentiment 分布, start_time/end_time,
    volume（按天声量曲线）。
    """
    if not notes:
        return {"ok": True, "events": [], "notes": 0, "clustered": 0, "noise": 0}

    docs = [_tokenize(f"{n.get('title') or ''} {n.get('desc') or ''}")
            for n in notes]
    pairs = [(n, d) for n, d in zip(notes, docs) if d]
    skipped = len(notes) - len(pairs)
    if not pairs:
        return {"ok": True, "events": [], "notes": len(notes),
                "clustered": 0, "noise": len(notes)}

    vecs, norms, idf = _tfidf_vectors([d for _, d in pairs])

    # 阶段一：严格阈值核心簇（阈值按数据量自适应）
    n_docs = len(pairs)
    core_th, assign_th = _adaptive_thresholds(n_docs, core_threshold,
                                              assign_threshold)
    cores = [g for g in _union_find_cluster(vecs, norms, core_th)
             if len(g) >= min_cluster]
    cores.sort(key=len, reverse=True)
    cores = cores[:max_events]

    assigned: Dict[int, int] = {}  # doc_idx -> core_idx
    for ci, g in enumerate(cores):
        for i in g:
            assigned[i] = ci

    # 阶段二：归属剩余帖子
    if cores:
        centroids = [_centroid(g, vecs) for g in cores]
        for i in range(len(pairs)):
            if i in assigned:
                continue
            best_ci, best_s = -1, assign_th
            for ci, (cvec, cnorm) in enumerate(centroids):
                s = _cosine(vecs[i], norms[i], cvec, cnorm)
                if s >= best_s:
                    best_s, best_ci = s, ci
            if best_ci >= 0:
                assigned[i] = best_ci
                cores[best_ci].append(i)
        # 重新计算质心已隐含在后续关键词/代表帖中，无需更新

    groups = [g for g in cores if len(g) >= min_cluster]
    groups.sort(key=len, reverse=True)

    used = set()
    events = []
    for g in groups:
        for i in g:
            used.add(i)
        member_docs = [pairs[i][1] for i in g]
        kw = _top_keywords(member_docs, idf)
        rep_idx = _representative(g, vecs, norms)
        rep = pairs[rep_idx][0]
        members = [pairs[i][0] for i in g]
        senti = Counter(m.get("sentiment") or "neutral" for m in members)
        times = [m.get("publish_time") or 0 for m in members]
        times = [t for t in times if t] or [0]
        vol: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for m in members:
            pt = m.get("publish_time")
            if not pt:
                continue
            d = datetime.fromtimestamp(pt, TZ).strftime("%Y-%m-%d")
            vol[d][0] += 1
            if (m.get("sentiment") or "") == "negative":
                vol[d][1] += 1
        events.append({
            "title": "、".join(kw[:3]) or "未命名事件",
            "keywords": kw,
            "note_ids": [m.get("post_id") for m in members],
            "note_count": len(members),
            "representative": {
                "post_id": rep.get("post_id"),
                "title": rep.get("title") or "",
            },
            "sentiment": {k: senti.get(k, 0)
                          for k in ("negative", "neutral", "positive")},
            "start_time": min(times),
            "end_time": max(times),
            "volume": [{"date": d, "count": v[0], "negative": v[1]}
                       for d, v in sorted(vol.items())],
        })

    clustered = len(used)
    return {"ok": True, "events": events, "notes": len(notes),
            "clustered": clustered, "noise": len(pairs) - clustered + skipped}
