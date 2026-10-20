# -*- coding: utf-8 -*-
"""主体核验：用公开资料替代企查查付费接口。

来源（全部不需要 API Key）：
- GB 32100-2015 统一社会信用代码校验码，本地计算
- GB/T 2260 登记机关行政区划（省级码），本地公开码表
- Wikidata SPARQL（query.wikidata.org）
- 信用中国公开检索（public.creditchina.gov.cn，无密钥）
- 国家企业信用信息公示系统公开页（www.gsxt.gov.cn）

公示系统的检索页有验证码。打不开或被验证码拦住时记为降级，
不编造经营者、信用代码或地址。调用方仍可手工录入后参与聚类。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]

_DEPT = {
    "1": "机构编制", "2": "外交", "3": "司法行政", "4": "文化",
    "5": "民政", "6": "旅游", "7": "宗教", "8": "工会",
    "9": "工商", "A": "中央军委改革和编制办公室", "N": "农业", "Y": "其他",
}
_ORG_KIND = {
    "1": "企业", "2": "个体工商户", "3": "农民专业合作社",
    "9": "其他",
}
_PROVINCE = {
    "11": "北京", "12": "天津", "13": "河北", "14": "山西", "15": "内蒙古",
    "21": "辽宁", "22": "吉林", "23": "黑龙江",
    "31": "上海", "32": "江苏", "33": "浙江", "34": "安徽", "35": "福建",
    "36": "江西", "37": "山东",
    "41": "河南", "42": "湖北", "43": "湖南", "44": "广东", "45": "广西",
    "46": "海南",
    "50": "重庆", "51": "四川", "52": "贵州", "53": "云南", "54": "西藏",
    "61": "陕西", "62": "甘肃", "63": "青海", "64": "宁夏", "65": "新疆",
    "71": "台湾", "81": "香港", "82": "澳门",
}

_UA = "CortechChat/1.0 (public entity verify; no paid registry API)"
_TIMEOUT = 4.0


class LookupDegraded(Exception):
    """公开源没有返回可用记录。"""


def _val(ch: str) -> int:
    idx = _ALPHABET.find(ch)
    if idx < 0:
        raise ValueError(f"非法字符 {ch}")
    return idx


def complete_uscc(body17: str) -> str:
    """给 17 位主体码补上 GB 32100 校验位。"""
    body = (body17 or "").strip().upper()
    if len(body) != 17:
        raise ValueError("统一社会信用代码前 17 位长度不对")
    total = sum(_val(body[i]) * _WEIGHTS[i] for i in range(17))
    expect = (31 - (total % 31)) % 31
    return body + _ALPHABET[expect]


def uscc_valid(code: str) -> bool:
    text = re.sub(r"\s+", "", code or "").upper()
    if not re.fullmatch(r"[0-9A-Z]{18}", text):
        return False
    try:
        return complete_uscc(text[:17]) == text
    except ValueError:
        return False


def decode_uscc(code: str) -> Dict[str, Any]:
    text = re.sub(r"\s+", "", code or "").upper()
    valid = uscc_valid(text)
    if len(text) != 18:
        return {"code": text, "valid": False, "message": "统一社会信用代码应为 18 位"}
    region_code = text[2:8]
    province = _PROVINCE.get(text[2:4], "")
    return {
        "code": text,
        "valid": valid,
        "department": _DEPT.get(text[0], "未知登记部门"),
        "org_kind": _ORG_KIND.get(text[1], "未知机构类别"),
        "region_code": region_code,
        "region": province,
        "org_code": text[8:17],
        "standard": "GB 32100-2015",
        "region_standard": "GB/T 2260",
        "message": ("校验码通过" if valid else "校验码不通过"),
    }


def _source(sid: str, title: str, status: str, detail: str) -> Dict[str, str]:
    return {"id": sid, "title": title, "status": status, "detail": detail[:300]}


def fetch_wikidata(name: str) -> Dict[str, Any]:
    safe = (name or "").replace("\\", "").replace('"', "")[:80]
    query = f"""
    SELECT ?item ?itemLabel ?inception WHERE {{
      ?item rdfs:label ?itemLabel .
      FILTER(LANG(?itemLabel) IN ("zh", "zh-cn", "zh-hans", "zh-hk", "en"))
      FILTER(CONTAINS(LCASE(?itemLabel), LCASE("{safe}")))
      OPTIONAL {{ ?item wdt:P571 ?inception }}
    }} LIMIT 5
    """
    resp = httpx.get(
        "https://query.wikidata.org/sparql",
        params={"query": query},
        headers={"Accept": "application/sparql-results+json", "User-Agent": _UA},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise LookupDegraded(f"Wikidata HTTP {resp.status_code}")
    rows = (((resp.json() or {}).get("results") or {}).get("bindings") or [])
    matches = []
    for row in rows:
        label = ((row.get("itemLabel") or {}).get("value") or "").strip()
        if not label:
            continue
        inception = ((row.get("inception") or {}).get("value") or "")[:10]
        matches.append({
            "name": label,
            "credit_code": "",
            "operator": "",
            "reg_address": "",
            "established_date": inception,
            "status": "Wikidata 有同名或包含该名称的条目",
            "source": "Wikidata",
            "confidence": 0.45,
            "note": "开放知识库条目，不是市场监管登记，没有统一社会信用代码。",
        })
    if not matches:
        raise LookupDegraded("Wikidata 没有匹配条目")
    return {
        "source": _source("wikidata", "Wikidata", "ok", f"{len(matches)} 条"),
        "matches": matches,
    }


def fetch_creditchina(name: str) -> Dict[str, Any]:
    resp = httpx.get(
        "https://public.creditchina.gov.cn/private-api/catalogSearch",
        params={
            "keyword": name,
            "scenes": "defaultscenario",
            "tableName": "credit_xyzx_tyshxydm",
            "searchState": "2",
            "page": "1",
            "pageSize": "5",
        },
        headers={"User-Agent": _UA, "Accept": "application/json"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise LookupDegraded(f"信用中国公开检索 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise LookupDegraded("信用中国返回的不是 JSON") from exc
    rows = _dig_rows(payload)
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ent_name = _first(row, "name", "entName", "jgmc", "xyztmc", "title")
        code = _first(row, "tyshxydm", "creditCode", "uniscid", "code")
        if not ent_name and not code:
            continue
        matches.append({
            "name": ent_name,
            "credit_code": code,
            "operator": _first(row, "legalPerson", "fddbr", "operator"),
            "reg_address": _first(row, "address", "dom", "regAddress"),
            "established_date": _first(row, "esdate", "established", "clrq"),
            "status": _first(row, "status", "entStatus", "regstate") or "信用中国公开记录",
            "source": "信用中国",
            "confidence": 0.8 if code else 0.55,
            "note": "信用中国公开检索，无 API Key。",
        })
    if not matches:
        raise LookupDegraded("信用中国公开检索没有解析到主体")
    return {
        "source": _source("creditchina", "信用中国", "ok", f"{len(matches)} 条"),
        "matches": matches,
    }


def fetch_gsxt(name: str) -> Dict[str, Any]:
    """公示系统公开页。检索接口需要验证码，这里不绕过，只记录降级。"""
    try:
        resp = httpx.get(
            "https://www.gsxt.gov.cn/index.html",
            params={"keyword": name},
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        raise LookupDegraded(f"国家企业信用信息公示系统不可达：{exc.__class__.__name__}") from exc
    raise LookupDegraded(
        f"国家企业信用信息公示系统公开页 HTTP {status}，"
        "企业检索需要验证码，未解析主体记录"
    )


def _first(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _dig_rows(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "result", "rows", "list", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            nested = _dig_rows(val)
            if nested:
                return nested
    return []


DEFAULT_FETCHERS: List[Callable[[str], Dict[str, Any]]] = [
    fetch_wikidata,
    fetch_creditchina,
    fetch_gsxt,
]


def _cache_get(name: str) -> Optional[Dict[str, Any]]:
    from .db import _connect
    _ensure_cache()
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM entity_cache WHERE name=?", (name,)
        ).fetchone()
    if not row:
        return None
    if int(time.time()) - int(row["fetched_at"] or 0) > 24 * 3600:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def _cache_put(name: str, payload: Dict[str, Any]) -> None:
    from .db import _connect
    _ensure_cache()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO entity_cache (name, payload, fetched_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,"
            " fetched_at=excluded.fetched_at",
            (name, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


def _ensure_cache() -> None:
    from .db import _connect
    with _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entity_cache ("
            " name TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
        )
        conn.commit()


def lookup(name: str, *, online: bool = True,
           fetchers: Optional[List[Callable[[str], Dict[str, Any]]]] = None,
           use_cache: bool = True) -> Dict[str, Any]:
    """核验主体名称或统一社会信用代码。公开源失败时 degraded=True，不抛网络错误。"""
    raw = (name or "").strip()
    if not raw:
        raise ValueError("请填写要核验的主体名称或统一社会信用代码")
    compact = re.sub(r"\s+", "", raw).upper()
    cache_key = compact if re.fullmatch(r"[0-9A-Z]{18}", compact) else raw
    custom = fetchers is not None
    if online and use_cache and not custom:
        cached = _cache_get(cache_key)
        if cached:
            cached["cached"] = True
            return cached

    sources: List[Dict[str, str]] = []
    matches: List[Dict[str, Any]] = []
    uscc = None
    if re.fullmatch(r"[0-9A-Z]{18}", compact):
        uscc = decode_uscc(compact)
        sources.append(_source(
            "gb32100", "GB 32100-2015",
            "ok" if uscc["valid"] else "mismatch",
            uscc["message"] + (f"，登记地 {uscc['region']}" if uscc.get("region") else ""),
        ))
        if uscc["valid"]:
            matches.append({
                "name": "",
                "credit_code": compact,
                "operator": "",
                "reg_address": uscc.get("region") or "",
                "established_date": "",
                "status": "校验码通过，存续状态未联网确认",
                "source": "GB 32100-2015",
                "confidence": 0.7,
                "department": uscc.get("department"),
                "org_kind": uscc.get("org_kind"),
                "note": "校验码和行政区划来自国家标准，不能代替公示系统的存续查询。",
            })

    if online:
        for fetcher in (fetchers if custom else DEFAULT_FETCHERS):
            try:
                got = fetcher(raw)
            except LookupDegraded as exc:
                sources.append(_source(fetcher.__name__.replace("fetch_", ""),
                                       _fetcher_title(fetcher), "degraded", str(exc)))
            except Exception as exc:  # noqa: BLE001
                sources.append(_source(fetcher.__name__.replace("fetch_", ""),
                                       _fetcher_title(fetcher), "degraded",
                                       f"{exc.__class__.__name__}: {exc}"[:180]))
            else:
                sources.append(got["source"])
                matches.extend(got.get("matches") or [])

    degraded = any(item["status"] == "degraded" for item in sources) or not matches
    ok = bool(matches)
    if ok and uscc and uscc.get("valid"):
        message = ("统一社会信用代码校验码通过。"
                   + (f"登记地代码对应{uscc.get('region')}。" if uscc.get("region") else "")
                   + "公开网页没有补充到经营者时，请在嫌疑详情页手工补录。")
    elif ok:
        message = "公开来源返回了可核对的记录，请人工确认后再写入嫌疑主体。"
    else:
        message = (
            "没有从公开来源核到这条主体。已尝试国家标准校验码、Wikidata、"
            "信用中国公开检索和国家企业信用信息公示系统公开页。"
            "公示系统检索通常需要验证码，这次没有拿到记录。"
            "请在嫌疑详情页手工录入经营者、统一社会信用代码和注册地址，录入后仍可参与聚类。"
        )
    result = {
        "ok": ok,
        "degraded": degraded or not ok,
        "subject": raw,
        "message": message,
        "matches": matches[:8],
        "sources": sources,
        "uscc": uscc,
        "cached": False,
        "paid_api": False,
    }
    if online and use_cache and not custom:
        try:
            _cache_put(cache_key, result)
        except Exception:
            pass
    return result


def _fetcher_title(fetcher: Callable) -> str:
    return {
        "fetch_wikidata": "Wikidata",
        "fetch_creditchina": "信用中国",
        "fetch_gsxt": "国家企业信用信息公示系统",
    }.get(getattr(fetcher, "__name__", ""), getattr(fetcher, "__name__", "公开源"))


def source_labels() -> List[str]:
    return ["GB 32100-2015", "GB/T 2260", "Wikidata", "信用中国", "国家企业信用信息公示系统"]
