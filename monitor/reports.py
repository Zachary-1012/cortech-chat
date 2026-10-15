# -*- coding: utf-8 -*-
"""规范化报告：舆情简报 / 专项报告 / 导出文件。

规范定义见 docs/报告与展示规范.md。本模块是该规范的后端实现。
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import db, trends
from .sentiment import _load_negative_words

SENTIMENT_CN = {"negative": "负面", "neutral": "中性", "positive": "正面"}


def _now_str() -> str:
    return datetime.now(trends.TZ).strftime("%Y-%m-%d %H:%M:%S")


def _report_no(prefix: str) -> str:
    return f"{prefix}-{datetime.now(trends.TZ).strftime('%Y%m%d-%H%M%S')}"


def _range_str(days: int) -> str:
    s, e = trends._day_bounds(days)
    f = lambda ts: datetime.fromtimestamp(ts, trends.TZ).strftime("%Y-%m-%d")
    return f"{f(s)} ~ {f(e)}"


def _scope_text(topic_id: Optional[int], days: int) -> str:
    kw = ""
    if topic_id:
        t = db.get_topic(topic_id)
        kw = f"，关键词：{t['keywords']}" if t else ""
    return (f"数据采集自小红书站内公开帖子及评论{kw}；"
            f"数据时间范围 {_range_str(days)}；情感由本地模型（SnowNLP+负面词库）判定，"
            f"仅供运营参考。")


def _extract_pain_points(title: str, desc: str, limit: int = 5) -> List[str]:
    """槽点 = 命中的负面词（按词库最长优先匹配）"""
    text = f"{title}\n{desc}"
    hits = [w for w in _load_negative_words() if w and w in text]
    return hits[:limit]


def build_briefing_report(topic_id: Optional[int] = None,
                          days: int = 7) -> Dict[str, Any]:
    """舆情简报（固定七段结构）"""
    b = trends.build_briefing(topic_id, days)
    t = trends.get_trends(topic_id, days)
    top_neg = []
    for n in b["top_negative"]:
        top_neg.append({
            "post_id": n["post_id"],
            "title": n["title"] or "(无标题)",
            "author": n["author"],
            "liked_count": n["liked_count"],
            "comment_count": n["comment_count"],
            "note_url": n["note_url"],
            "pain_points": _extract_pain_points(n["title"], ""),
        })
    alerts = []
    for topic in ([db.get_topic(topic_id)] if topic_id else db.list_topics()):
        if not topic:
            continue
        s = trends.spike_alert(topic["id"])
        if s["alert"]:
            alerts.append({"topic_name": topic["name"], **s})
    conclusions = _conclusions(b, alerts)
    # 全网资讯动态一节（帮手 C：news.briefing）
    from .news import briefing as news_briefing
    web_news = news_briefing.briefing_section(days)
    return {
        "report_no": _report_no("YQJB"),
        "type": "舆情简报",
        "product": "Chat Cortech",
        "generated_at": _now_str(),
        "data_range": _range_str(days),
        "scope": _scope_text(topic_id, days),
        "topic_id": topic_id,
        "topic_name": b.get("topic_name"),
        "sections": {
            "overview": b["trend_summary"],
            "volume": {
                "trend": t["volume"],
                "text": (f"近{days}天共采集 {b['volume_total']} 条帖子，"
                         f"环比{b['volume_delta']}。"),
            },
            "sentiment": {
                "dist": b["sentiment_dist"],
                "neg_ratio": b["neg_ratio"],
                "trend": t["sentiment"],
                "text": (f"负面 {b['sentiment_dist']['negative']} 条 "
                         f"({b['neg_ratio']:.0%})，中性 "
                         f"{b['sentiment_dist']['neutral']} 条，正面 "
                         f"{b['sentiment_dist']['positive']} 条。"),
            },
            "top_negative": top_neg,
            "hot_words": b["hot_words"],
            "web_news": web_news,
            "alerts": alerts,
            "conclusion": conclusions,
        },
    }


def _conclusions(b: Dict[str, Any], alerts: List[Dict]) -> List[str]:
    out = []
    if b["neg_ratio"] >= 0.3:
        out.append(f"负面占比达 {b['neg_ratio']:.0%}，建议优先处理 Top 负面帖文，"
                   "必要时官方账号下场回应。")
    elif b["neg_ratio"] >= 0.15:
        out.append("负面占比处于中等水平，建议持续监控，重点跟进新增负面。")
    else:
        out.append("整体情感平稳，保持常规监控即可。")
    for a in alerts:
        out.append(f"预警：{a['topic_name']}——{a['message']}，建议当日复核。")
    pains: Dict[str, int] = {}
    for n in b["top_negative"]:
        for w in _extract_pain_points(n["title"], ""):
            pains[w] = pains.get(w, 0) + 1
    if pains:
        top3 = sorted(pains, key=pains.get, reverse=True)[:3]
        out.append("主要槽点集中在：" + "、".join(top3) + "，建议针对性优化或回应。")
    if not out:
        out.append("暂无特别事项。")
    return out


def build_special_report(topic_id: Optional[int] = None, days: int = 30,
                         title: str = "专项舆情报告") -> Dict[str, Any]:
    """专项报告（固定五段结构，诚实声明方法与局限）"""
    b = trends.build_briefing(topic_id, days)
    t = trends.get_trends(topic_id, days)
    topic = db.get_topic(topic_id) if topic_id else None
    notes = db.list_posts(topic_id, page_size=200)["items"]
    comments_all = []
    for n in notes[:50]:
        comments_all.extend(
            db.list_comments(n["post_id"], page_size=20)["items"])
    top_neg = []
    for n in b["top_negative"]:
        top_neg.append({
            "post_id": n["post_id"], "title": n["title"] or "(无标题)",
            "author": n["author"], "note_url": n["note_url"],
            "pain_points": _extract_pain_points(n["title"], ""),
        })
    return {
        "report_no": _report_no("ZXBG"),
        "type": "专项报告",
        "product": "Chat Cortech",
        "title": title,
        "generated_at": _now_str(),
        "data_range": _range_str(days),
        "scope": _scope_text(topic_id, days),
        "sections": {
            "task": {
                "topic": topic["name"] if topic else "全部话题",
                "keywords": topic["keywords"] if topic else "",
                "days": days,
                "text": (f"本报告针对「{topic['name'] if topic else '全部话题'}」"
                         f"开展专项搜集，时间范围 {_range_str(days)}。"),
            },
            "conclusion": b["trend_summary"],
            "data": {
                "notes_total": b["volume_total"],
                "notes": [{
                    "post_id": n["post_id"], "title": n["title"] or "(无标题)",
                    "author": n["author"], "publish_time": _fmt_ts(n["publish_time"]),
                    "liked_count": n["liked_count"], "comment_count": n["comment_count"],
                    "sentiment": SENTIMENT_CN.get(n["sentiment"], n["sentiment"]),
                    "note_url": n["note_url"],
                } for n in notes],
                "comments_sample": [{
                    "comment_id": c["comment_id"], "post_id": c["post_id"],
                    "content": (c["content"][:100] + "…") if len(c["content"]) > 100 else c["content"],
                    "author": c["author"],
                    "sentiment": SENTIMENT_CN.get(c["sentiment"], c["sentiment"]),
                } for c in comments_all[:100]],
                "top_negative": top_neg,
            },
            "method_limits": [
                "渠道：仅覆盖小红书站内公开帖子与评论；未覆盖抖音/微博/大众点评等其他平台。",
                "采集方式：关键词搜索采集，依赖平台搜索排序；未被搜索召回的内容不在范围内。",
                "情感判定：本地模型（SnowNLP+负面词库）自动判定，存在误判可能，重要结论建议人工复核。",
                "时效：数据为采集时刻快照，点赞/评论数会随时间变化。",
            ],
            "suggestions": _conclusions(b, []),
        },
    }


def _brand_short() -> str:
    """品牌简称（用于文件名等），换品牌改配置即可。"""
    try:
        from .site import get_brand
        return get_brand()["short"]
    except Exception:
        return ""


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, trends.TZ).strftime("%Y-%m-%d %H:%M")


POST_HEADERS = ["帖子ID", "标题", "作者", "粉丝数", "发布时间", "IP属地", "话题标签",
                "点赞", "收藏", "评论", "分享", "情感", "情感分数", "原帖链接", "采集时间"]
COMMENT_HEADERS = ["评论ID", "所属帖子", "内容", "作者", "时间", "点赞", "情感"]


def _fetch_notes_rows(topic_id=None, sentiment=None,
                       date_from=None, date_to=None) -> List[Dict[str, Any]]:
    """统一取帖子行数据（供各格式导出复用）。"""
    data = db.list_posts(topic_id, sentiment, None, page=1, page_size=100000,
                         date_from=date_from, date_to=date_to)
    rows = []
    for n in data["items"]:
        rows.append({
            "帖子ID": n["post_id"], "标题": n["title"], "作者": n["author"],
            "粉丝数": n.get("fans_count", 0),
            "发布时间": _fmt_ts(n.get("publish_time")),
            "IP属地": n.get("ip_location", ""), "话题标签": n.get("tags", ""),
            "点赞": n["liked_count"], "收藏": n["collected_count"],
            "评论": n["comment_count"], "分享": n["share_count"],
            "情感": SENTIMENT_CN.get(n["sentiment"], n["sentiment"]),
            "情感分数": round(n["sentiment_score"], 3),
            "原帖链接": n["note_url"],
            "采集时间": _fmt_ts(n.get("collected_at")),
        })
    return rows


def _fetch_comments_rows(post_id=None, topic_id=None) -> List[Dict[str, Any]]:
    """统一取评论行数据。"""
    items: List[Dict[str, Any]] = []
    if post_id:
        items = db.list_comments(post_id, page_size=100000)["items"]
    elif topic_id:
        for n in db.list_posts(topic_id, page_size=1000)["items"]:
            items.extend(db.list_comments(n["post_id"], page_size=100000)["items"])
    rows = []
    for c in items:
        rows.append({
            "评论ID": c["comment_id"], "所属帖子": c["post_id"],
            "内容": c["content"], "作者": c["author"],
            "时间": _fmt_ts(c.get("create_time")), "点赞": c["like_count"],
            "情感": SENTIMENT_CN.get(c["sentiment"], c["sentiment"]),
        })
    return rows


def _export_fname(kind: str, ext: str) -> str:
    ts = datetime.now(trends.TZ).strftime("%Y%m%d_%H%M%S")
    return f"{_brand_short()}舆情_{kind}_{ts}.{ext}"


def export_notes_csv(topic_id=None, sentiment=None,
                     date_from=None, date_to=None) -> tuple[str, str]:
    """返回 (文件名, CSV文本)。文件名规范：{品牌简称}舆情_帖子_YYYYMMDD_HHMMSS.csv"""
    rows = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(POST_HEADERS)
    for r in rows:
        w.writerow([r[h] for h in POST_HEADERS])
    return _export_fname("帖子", "csv"), "\ufeff" + buf.getvalue()


def export_comments_csv(post_id: Optional[str] = None,
                        topic_id=None) -> tuple[str, str]:
    """评论导出。文件名规范：{品牌简称}舆情_评论_YYYYMMDD_HHMMSS.csv"""
    rows = _fetch_comments_rows(post_id, topic_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COMMENT_HEADERS)
    for r in rows:
        w.writerow([r[h] for h in COMMENT_HEADERS])
    return _export_fname("评论", "csv"), "\ufeff" + buf.getvalue()


def export_notes_xlsx(topic_id=None, sentiment=None,
                      date_from=None, date_to=None) -> tuple[str, bytes]:
    """XLSX 导出：帖子 / 评论 / 统计 三个 sheet。返回 (文件名, 二进制)。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="D97757")  # 陶土橙
    wrap = Alignment(wrap_text=True, vertical="top")

    def _write_sheet(ws, headers, rows):
        ws.append(headers)
        for c in ws[1]:
            c.font = hdr_font
            c.fill = hdr_fill
        for r in rows:
            ws.append([r[h] for h in headers])
        for row in ws.iter_rows():
            for c in row:
                c.alignment = wrap
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(12, min(40, len(h) * 3))

    # Sheet1: 帖子
    ws1 = wb.active
    ws1.title = "帖子"
    notes = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    _write_sheet(ws1, POST_HEADERS, notes)

    # Sheet2: 评论
    ws2 = wb.create_sheet("评论")
    comments = _fetch_comments_rows(topic_id=topic_id)
    _write_sheet(ws2, COMMENT_HEADERS, comments)

    # Sheet3: 统计
    ws3 = wb.create_sheet("统计")
    pos = sum(1 for r in notes if r["情感"] == "正面")
    neg = sum(1 for r in notes if r["情感"] == "负面")
    neu = len(notes) - pos - neg
    ws3.append(["指标", "数值"])
    for c in ws3[1]:
        c.font = hdr_font
        c.fill = hdr_fill
    for k, v in [("帖子总数", len(notes)), ("正面", pos), ("负面", neg),
                 ("中性", neu), ("评论总数", len(comments)),
                 ("导出时间", _now_str())]:
        ws3.append([k, v])

    buf = io.BytesIO()
    wb.save(buf)
    return _export_fname("帖子", "xlsx"), buf.getvalue()


def export_notes_json(topic_id=None, sentiment=None,
                      date_from=None, date_to=None) -> tuple[str, str]:
    """JSON 结构化导出。"""
    rows = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    payload = {
        "brand": _brand_short(), "exported_at": _now_str(),
        "count": len(rows), "posts": rows,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return _export_fname("帖子", "json"), text


def export_comments_json(post_id=None, topic_id=None) -> tuple[str, str]:
    """评论 JSON 导出。"""
    rows = _fetch_comments_rows(post_id, topic_id)
    payload = {
        "brand": _brand_short(), "exported_at": _now_str(),
        "count": len(rows), "comments": rows,
    }
    return _export_fname("评论", "json"), json.dumps(payload, ensure_ascii=False, indent=2)


def export_notes_md(topic_id=None, sentiment=None,
                    date_from=None, date_to=None) -> tuple[str, str]:
    """Markdown 报告导出。"""
    rows = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    L = [f"# {_brand_short()}舆情报告", "", f"导出时间：{_now_str()}",
         f"帖子数：{len(rows)}", "",
         "| 标题 | 作者 | 发布时间 | 点赞 | 评论 | 情感 |",
         "|---|---|---|---|---|---|"]
    for r in rows[:200]:  # MD 报告取前 200
        title = (r["标题"] or "")[:30].replace("|", "/")
        L.append(f"| {title} | {r['作者']} | {r['发布时间']} | "
                 f"{r['点赞']} | {r['评论']} | {r['情感']} |")
    if len(rows) > 200:
        L.append(f"\n> 共 {len(rows)} 条，仅展示前 200 条，完整数据请导出 XLSX/CSV。")
    return _export_fname("帖子", "md"), "\n".join(L)


def export_notes_html(topic_id=None, sentiment=None,
                      date_from=None, date_to=None) -> tuple[str, str]:
    """HTML 报告导出（带样式，可直接打印成 PDF）。"""
    rows = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    trs = []
    for r in rows[:500]:
        color = {"正面": "#2e7d32", "负面": "#c62828"}.get(r["情感"], "#5e5d59")
        trs.append(
            f"<tr><td>{r['标题'] or ''}</td><td>{r['作者']}</td>"
            f"<td>{r['发布时间']}</td><td>{r['点赞']}</td>"
            f"<td>{r['评论']}</td>"
            f"<td style='color:{color}'>{r['情感']}</td></tr>")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_brand_short()}舆情报告</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#FAF9F6;color:#141413;margin:40px}}
h1{{color:#141413}}table{{border-collapse:collapse;width:100%;background:#fff}}
th{{background:#D97757;color:#fff;padding:10px;text-align:left}}
td{{border:1px solid #f0eee6;padding:8px;font-size:14px}}
@media print{{body{{margin:0}}}}
</style></head><body>
<h1>{_brand_short()}舆情报告</h1>
<p>导出时间：{_now_str()} ｜ 帖子数：{len(rows)}</p>
<table><tr><th>标题</th><th>作者</th><th>发布时间</th><th>点赞</th><th>评论</th><th>情感</th></tr>
{"".join(trs)}</table></body></html>"""
    return _export_fname("帖子", "html"), html


def export_notes_pdf(topic_id=None, sentiment=None,
                     date_from=None, date_to=None) -> tuple[str, bytes]:
    """PDF 导出（fpdf2，中文用系统字体）。"""
    from fpdf import FPDF
    import os

    rows = _fetch_notes_rows(topic_id, sentiment, date_from, date_to)
    pdf = FPDF()
    # 中文字体：优先用 Windows 黑体/宋体
    font_added = False
    for fp in ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if os.path.exists(fp):
            pdf.add_font("Cortech", "", fp)
            font_added = True
            break
    pdf.add_page()
    if font_added:
        pdf.set_font("Cortech", size=16)
    else:
        pdf.set_font("Helvetica", size=16)
    # 中文标题用 ASCII 安全写法
    pdf.cell(0, 10, "Cortech Yuqing Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Cortech" if font_added else "Helvetica", size=10)
    if font_added:
        pdf.cell(0, 8, f"导出时间: {_now_str()}  帖子数: {len(rows)}",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        for r in rows[:100]:
            title = (r["标题"] or "")[:40]
            pdf.multi_cell(0, 6, f"[{r['情感']}] {title} | {r['作者']} | 赞{r['点赞']}")
    else:
        pdf.cell(0, 8, f"Posts: {len(rows)} (Chinese font not found, use HTML export for full report)",
                 new_x="LMARGIN", new_y="NEXT")
    out = pdf.output()
    data = bytes(out) if isinstance(out, bytearray) else out
    return _export_fname("帖子", "pdf"), data


# 导出格式注册表：format → (后缀, content-type, 函数, 是否二进制)
CORTECH_EXPORT_FORMATS = {
    "csv": ("csv", "text/csv; charset=utf-8", export_notes_csv, False),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", export_notes_xlsx, True),
    "json": ("json", "application/json; charset=utf-8", export_notes_json, False),
    "md": ("md", "text/markdown; charset=utf-8", export_notes_md, False),
    "html": ("html", "text/html; charset=utf-8", export_notes_html, False),
    "pdf": ("pdf", "application/pdf", export_notes_pdf, True),
}
