# -*- coding: utf-8 -*-
"""方案比稿：PDF/DOCX/文本解析 → AI 六维结构化拆解 → 横向对比 → 四维评分雷达 → 评审报告。

全开源：pypdf / python-docx 解析，LLM 复用已接入 provider 链。
比稿文件仅存用户本地 data/bids/，不上传任何外部服务。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ai_chat, db
from .config import DATA_DIR

logger = logging.getLogger("xhs-monitor.bid")

BIDS_DIR = DATA_DIR / "bids"

# AI 结构化拆解的固定维度
EXTRACT_DIMS = [
    ("concept", "创意概念"),
    ("gameplay", "核心玩法"),
    ("schedule", "执行排期"),
    ("price", "报价"),
    ("kpi", "KPI 承诺"),
    ("cases", "团队过往案例"),
]

# 四维评分（权重可调，默认见 db.DEFAULT_BID_WEIGHTS）
SCORE_DIMS = [
    ("creativity", "创意性"),
    ("feasibility", "可行性"),
    ("value", "性价比"),
    ("schedule", "档期匹配度"),
]

EXTRACT_PROMPT = """你是专业的市场活动比稿评审顾问。请阅读以下比稿方案全文，
按固定维度做结构化拆解，只输出 JSON（不要 markdown 代码块，不要解释）：

{{
  "concept": "创意概念（50字内概括）",
  "gameplay": "核心玩法（分点列出，每点20字内）",
  "schedule": "执行排期（关键节点和时间）",
  "price": "报价（金额与包含项）",
  "kpi": "KPI 承诺（具体数字指标）",
  "cases": "团队过往案例（列出名称与简介）",
  "summary": "一句话总评"
}}

方案全文：
---
{text}
---
只输出 JSON。"""


def parse_pdf(path: Path) -> str:
    """PDF → 纯文本。"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def parse_docx(path: Path) -> str:
    """DOCX → 纯文本（含表格）。"""
    import docx
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def parse_doc(path: Path) -> str:
    """DOC（老格式）→ 提示转格式。python-docx 不支持 .doc。"""
    raise ValueError(
        "不支持 .doc 老格式，请用 Word 另存为 .docx 后再上传")


def parse_xlsx(path: Path) -> str:
    """XLSX → 纯文本（所有 sheet 的表格转文本）。"""
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"【{ws.title}】")
        for row in ws.iter_rows(values_only=True):
            vals = [str(v) for v in row if v is not None]
            if vals:
                parts.append(" | ".join(vals))
    wb.close()
    return "\n".join(parts)


def parse_xls(path: Path) -> str:
    """XLS（老格式）→ 提示转格式。openpyxl 不支持 .xls。"""
    raise ValueError(
        "不支持 .xls 老格式，请用 Excel 另存为 .xlsx 后再上传")


def parse_csv(path: Path) -> str:
    """CSV → 纯文本。"""
    import csv
    parts = []
    with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        for row in csv.reader(f):
            vals = [v.strip() for v in row if v.strip()]
            if vals:
                parts.append(" | ".join(vals))
    return "\n".join(parts)


def parse_txt_md(path: Path) -> str:
    """TXT/MD → 纯文本。"""
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_html(path: Path) -> str:
    """HTML → 纯文本（去标签）。"""
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip = True
            if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip and data.strip():
                self.parts.append(data.strip())

    s = _Stripper()
    s.feed(path.read_text(encoding="utf-8", errors="ignore"))
    text = "".join(s.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_json(path: Path) -> str:
    """JSON → 纯文本（提取所有字符串字段）。"""
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    parts: list = []

    def _walk(o):
        if isinstance(o, str) and o.strip():
            parts.append(o.strip())
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(data)
    return "\n".join(parts)


# 统一解析器注册表：后缀 → 解析函数
CORTECH_PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".doc": parse_doc,
    ".xlsx": parse_xlsx,
    ".xls": parse_xls,
    ".csv": parse_csv,
    ".txt": parse_txt_md,
    ".md": parse_txt_md,
    ".html": parse_html,
    ".htm": parse_html,
    ".json": parse_json,
}

# 前端 accept 用
CORTECH_UPLOAD_ACCEPT = ",".join(sorted(CORTECH_PARSERS.keys()))


def extract_text_from_file(path: Path) -> str:
    """统一入口：按后缀分发到对应解析器。"""
    suffix = path.suffix.lower()
    fn = CORTECH_PARSERS.get(suffix)
    if not fn:
        raise ValueError(
            f"不支持的文件类型: {suffix}（支持 {', '.join(sorted(CORTECH_PARSERS))}）")
    return fn(path)


def _sentences(text: str) -> List[str]:
    """按句切分（中文句号/换行优先）。"""
    parts = re.split(r"[。！？\n；;]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def rule_extract(raw_text: str) -> Dict[str, str]:
    """规则兜底的六维拆解：无 AI 可用时也能给出可用结果；
    有 AI 时用于补齐 AI 未提取到的维度。纯本地正则，零外部依赖。"""
    text = (raw_text or "").strip()[:12000]
    sents = _sentences(text)
    out: Dict[str, str] = {}

    # 创意概念：首个有效段落（去标题化，限 100 字）
    out["concept"] = (sents[0][:100] if sents else "（未识别到）")

    # 核心玩法：含玩法关键词的句子
    play_kw = ("玩法", "互动", "抽奖", "直播", "打卡", "话题", "挑战赛",
               "裂变", "种草", "探店")
    plays = [s for s in sents if any(k in s for k in play_kw)][:5]
    out["gameplay"] = "；".join(plays) if plays else "（未识别到）"

    # 执行排期：含日期/阶段词的句子
    date_re = re.compile(r"(\d{4}年)?\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}")
    phase_kw = ("阶段", "预热", "爆发", "收官", "排期", "节点", "上线", "执行")
    sched = [s for s in sents
             if date_re.search(s) or any(k in s for k in phase_kw)][:6]
    out["schedule"] = "；".join(sched) if sched else "（未识别到）"

    # 报价：金额表达式
    money_re = re.compile(
        r"(?:¥|￥|RMB|人民币)?\s*\d[\d,.]*\s*(?:万)?元|"
        r"\d[\d,.]*\s*万元?")
    monies = money_re.findall(text)[:5]
    out["price"] = "、".join(dict.fromkeys(m.strip() for m in monies)
                             ) if monies else "（未识别到）"

    # KPI 承诺：指标词 + 数字的句子
    kpi_kw = ("KPI", "曝光", "阅读", "播放", "转化", "涨粉", "增粉", "GMV",
              "ROI", "互动率", "点赞", "目标")
    kpis = [s for s in sents if any(k in s for k in kpi_kw)
            and re.search(r"\d", s)][:5]
    out["kpi"] = "；".join(kpis) if kpis else "（未识别到）"

    # 团队过往案例：含案例关键词的句子
    case_kw = ("案例", "曾服务", "合作", "品牌方", "操盘", "执行过")
    cases = [s for s in sents if any(k in s for k in case_kw)][:5]
    out["cases"] = "；".join(cases) if cases else "（未识别到）"

    return out


def ai_extract(raw_text: str) -> Dict[str, Any]:
    """AI 六维拆解（规则兜底补齐）。
    无可用 LLM 时直接返回规则拆解（_mode=rule）；有 AI 时，
    AI 未提取到的维度用规则结果补齐。"""
    text = (raw_text or "").strip()[:12000]
    if not text:
        raise ValueError("方案文本为空")
    rules = rule_extract(text)
    mode, _ = ai_chat.check_ai_ready()
    if mode != "ai":
        return {**rules, "summary": "（规则拆解：当前无可用 AI provider，"
                "已用本地规则提取关键信息）", "_mode": "rule"}
    provider = ai_chat._mode_cache.get("provider")
    client, p = ai_chat._openai_client(provider)
    resp = client.chat.completions.create(
        model=p["model"],
        messages=[{"role": "user",
                   "content": EXTRACT_PROMPT.format(text=text)}],
        temperature=0.2)
    out = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("模型未返回有效 JSON，请重试")
    data = json.loads(m.group(0))
    result = {}
    for k, _ in EXTRACT_DIMS:
        v = str(data.get(k, "")).strip()
        # AI 没提取到 → 规则补齐
        if not v or v == "（未提取到）":
            v = rules.get(k, "（未提取到）")
        result[k] = v
    result["summary"] = str(data.get("summary", ""))
    result["_mode"] = "ai"
    result["_provider"] = p["name"]
    return result


def save_extracted(bid_id: int, extracted: Dict[str, Any]) -> bool:
    """人工编辑六维拆解后保存（供 PUT /api/bids/{id}/extracted 接线）。"""
    valid = {k for k, _ in EXTRACT_DIMS}
    clean = {k: str(v)[:2000] for k, v in (extracted or {}).items()
             if k in valid}
    if not clean:
        raise ValueError("没有有效的拆解维度")
    full = db.get_bid(bid_id) or {}
    old = json.loads(full.get("extracted_json") or "{}")
    old.update(clean)
    old["_mode"] = "manual"
    return db.update_bid_extracted(bid_id, old)


def reextract(bid_id: int) -> Dict[str, Any]:
    """重新跑 AI/规则拆解（供 POST /api/bids/{bid_id}/reextract 接线）。"""
    full = db.get_bid(bid_id)
    if not full:
        raise ValueError("比稿方案不存在")
    extracted = ai_extract(full.get("raw_text") or "")
    db.update_bid_extracted(bid_id, extracted)
    return extracted


def weighted_total(scores: Dict[str, float],
                   weights: Dict[str, float]) -> float:
    total_w = sum(weights.get(k, 0) for k, _ in SCORE_DIMS) or 1
    return round(sum(scores.get(k, 0) * weights.get(k, 0)
                     for k, _ in SCORE_DIMS) / total_w, 2)


def compare() -> Dict[str, Any]:
    """横向对比：对比表 + 四维评分 + 雷达图数据 + 排名。"""
    import json as _json
    bids = db.list_bids()
    rows = []
    for b in bids:
        full = db.get_bid(b["id"])
        extracted = _json.loads(full["extracted_json"] or "{}")
        scores = _json.loads(full["scores_json"] or "{}")
        weights = _json.loads(full["weights_json"] or "{}")
        rows.append({
            "id": b["id"], "name": b["name"],
            "extracted": {k: extracted.get(k, "") for k, _ in EXTRACT_DIMS},
            "scores": {k: scores.get(k, 0) for k, _ in SCORE_DIMS},
            "weights": weights,
            "total": weighted_total(scores, weights),
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    radar = {
        "dims": [{"key": k, "name": n} for k, n in SCORE_DIMS],
        "series": [{"name": r["name"],
                    "values": [r["scores"][k] for k, _ in SCORE_DIMS]}
                   for r in rows],
    }
    return {"bids": rows, "radar": radar,
            "ranking": [r["name"] for r in rows]}


def build_report() -> Dict[str, Any]:
    """比稿评审报告（沿用统一报告规范：编号/时间/结论先行/对比表明细/推荐意见）。"""
    import datetime as _dt
    c = compare()
    rows = c["bids"]
    no = "BGPG" + _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    if rows:
        winner = rows[0]
        conclusion = (f"综合评分最高为「{winner['name']}」（{winner['total']} 分），"
                      f"建议优先考虑。共 {len(rows)} 家参评。")
        recommend = (f"推荐「{winner['name']}」："
                     + "、".join(f"{n}{winner['scores'][k]}分"
                                 for k, n in SCORE_DIMS))
    else:
        conclusion, recommend = "暂无参评方案。", "请先录入比稿方案。"
    return {
        "report_no": no,
        "type": "比稿评审报告",
        "product": "Chat Cortech",
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "conclusion": conclusion,
        "table": [
            {"name": r["name"],
             **{k: r["extracted"][k] for k, _ in EXTRACT_DIMS},
             **{f"score_{k}": r["scores"][k] for k, _ in SCORE_DIMS},
             "total": r["total"]}
            for r in rows
        ],
        "radar": c["radar"],
        "recommend": recommend,
        "method": ("评分维度：创意性/可行性/性价比/档期匹配度（权重可调，"
                   "默认 0.3/0.3/0.25/0.15）；各维度 0-10 分人工打分。"),
    }
