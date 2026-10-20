# -*- coding: utf-8 -*-
"""伪冒账号反查（V1.1）：品牌维权——发现、核验、聚类、投诉全链路。

架构原则（通用化，不写死任何品牌）：
- 品牌档案（fc_brands）：每个品牌独立的特征库 + 嫌疑库 + 投诉批次。
- 特征库（fc_features）：命名模式 / 简介关键词 / IP 属地 / 主体类型 / sweep 关键词，
  全部后台可增删改；代码里不出现任何品牌的 hardcode 判断。
- docs/伪冒特征库-seed.json 只是首次启动的默认预设（可改可删可新增品牌）。
- DB 表首次调用自动创建，无需改 db.py；接线清单见模块底部的 WIRING 注释。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .db import _connect, apply_uid_filter, current_user_id

logger = logging.getLogger("xhs.fakecheck")

_lock = threading.Lock()

# 种子预设品牌名（仅数据默认值，用户可在后台改名/删除；非业务逻辑 hardcode）
SEED_BRAND_NAME = "DFS"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    remark TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fc_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    brand_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    pattern_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(brand_id, kind, value)
);
CREATE TABLE IF NOT EXISTS fc_suspects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    brand_id INTEGER NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    xhs_id TEXT NOT NULL DEFAULT '',
    uid TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    subject_type TEXT NOT NULL DEFAULT '',
    ip_location TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    credit_code TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    reg_address TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'import',
    score INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT 'low',
    flags TEXT NOT NULL DEFAULT '[]',
    verified INTEGER NOT NULL DEFAULT 0,
    profile_url TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fc_suspects_brand ON fc_suspects(brand_id);
CREATE INDEX IF NOT EXISTS idx_fc_suspects_xhs ON fc_suspects(xhs_id);
CREATE TABLE IF NOT EXISTS fc_sweep_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    max_notes INTEGER NOT NULL DEFAULT 30,
    account_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    progress TEXT NOT NULL DEFAULT '',
    found_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS fc_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    batch_size INTEGER NOT NULL DEFAULT 5,
    interval_days INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fc_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    batch_id INTEGER NOT NULL,
    suspect_id INTEGER NOT NULL,
    batch_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fc_batch_items_batch ON fc_batch_items(batch_id);
CREATE TABLE IF NOT EXISTS fc_rights_holder (
    brand_id INTEGER PRIMARY KEY,
    user_id INTEGER,
    company TEXT NOT NULL DEFAULT '',
    trademark_no TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

# 特征种类
KIND_LABELS = {
    "nickname_kw": "昵称关键词",
    "bio_kw": "简介关键词",
    "ip": "IP 属地",
    "subject_type": "可疑主体类型",
    "sweep_kw": "Sweep 搜索词",
}

VERIFY_LABELS = {0: "未核验", 1: "确认为伪冒", 2: "已排除"}
LEVEL_LABELS = {"high": "高", "mid": "中", "low": "低"}


def _ensure() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        # 迁移：V1.1 新增成立日期列（老库无此列时补上）
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(fc_suspects)").fetchall()]
        if "established_date" not in cols:
            conn.execute("ALTER TABLE fc_suspects ADD COLUMN established_date TEXT NOT NULL DEFAULT ''")
        # 迁移：P0 升级新增 pattern_id 列（溯源到判定模式库）
        fcols = [r["name"] for r in conn.execute("PRAGMA table_info(fc_features)").fetchall()]
        if "pattern_id" not in fcols:
            conn.execute("ALTER TABLE fc_features ADD COLUMN pattern_id TEXT NOT NULL DEFAULT ''")
        # 迁移：多用户隔离 user_id 列
        for _t in ("fc_brands", "fc_features", "fc_suspects", "fc_sweep_tasks",
                   "fc_batches", "fc_batch_items", "fc_rights_holder"):
            _cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_t})").fetchall()]
            if "user_id" not in _cols:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN user_id INTEGER")


def _seed_path() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "伪冒特征库-seed.json"


def _ensure_seed() -> None:
    """首次启动：无品牌档案时，从 seed JSON 导入默认预设品牌。"""
    _ensure()
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM fc_brands").fetchone()["c"]
    if n:
        return
    seed: Dict[str, Any] = {}
    try:
        seed = json.loads(_seed_path().read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("种子特征库读取失败，将创建空品牌：%s", e)
    bid = create_brand(SEED_BRAND_NAME, keywords=SEED_BRAND_NAME,
                       remark="默认预设（种子特征库），可在后台修改/删除")
    feats: List[Tuple[str, str, int]] = []
    for kw, w in (seed.get("nickname_keywords") or {}).items():
        feats.append(("nickname_kw", str(kw), int(w or 0)))
    for kw in (seed.get("bio_keywords") or []):
        feats.append(("bio_kw", str(kw), 8))
    for ip in (seed.get("ip_locations") or {}).keys():
        feats.append(("ip", str(ip), 15))
    for st in (seed.get("subject_types") or {}).keys():
        feats.append(("subject_type", str(st), 10))
    for kw in (seed.get("sweep_keywords") or []):
        feats.append(("sweep_kw", str(kw), 0))
    for kind, value, weight in feats:
        try:
            save_feature(bid, kind, value, weight)
        except Exception:
            pass
    logger.info("已导入种子预设品牌 %s（%d 条特征）", SEED_BRAND_NAME, len(feats))
    # P0 升级：导入判定模式库 8 模式的触发关键词（幂等）
    try:
        pr = import_pattern_features(bid)
        logger.info("判定模式特征导入：%d 条，覆盖 %s", pr["imported"], pr["patterns"])
    except Exception as e:
        logger.warning("判定模式特征导入跳过：%s", e)


def ensure_pattern_features() -> Dict[str, int]:
    """存量迁移：已存在的品牌若缺失模式特征，则补导入（幂等，可重复调用）。

    在 app 启动时调用一次；_ensure_seed 只覆盖全新安装。
    """
    _ensure()
    total = 0
    brands: List[Dict[str, Any]] = []
    try:
        brands = list_brands()
    except Exception:
        return {"imported": 0}
    pats = load_patterns()
    pat_brand = (pats.get("brand") or "").strip().lower()
    for b in brands:
        # 仅对模式库对应的品牌（DFS）自动补导入；其他品牌由用户在后台手动触发
        if pat_brand and (b.get("name") or "").strip().lower() != pat_brand:
            continue
        bid = int(b["id"])
        with _connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM fc_features WHERE brand_id=? AND pattern_id<>''",
                (bid,)).fetchone()["c"]
        if n == 0:
            try:
                r = import_pattern_features(bid)
                total += r["imported"]
            except Exception as e:
                logger.warning("品牌 %d 模式特征补导入失败：%s", bid, e)
    return {"imported": total}


# ---------------- 品牌档案 ----------------

def _brand_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "keywords": r["keywords"],
            "remark": r["remark"], "created_at": r["created_at"]}


def list_brands() -> List[Dict[str, Any]]:
    _ensure_seed()
    where, params = [], []
    apply_uid_filter(where, params)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM fc_brands {clause} ORDER BY id", params).fetchall()
    return [_brand_to_dict(r) for r in rows]


def get_brand(bid: int) -> Optional[Dict[str, Any]]:
    _ensure()
    where, params = ["id=?"], [bid]
    apply_uid_filter(where, params)
    with _connect() as conn:
        r = conn.execute(f"SELECT * FROM fc_brands WHERE {' AND '.join(where)}", params).fetchone()
    return _brand_to_dict(r) if r else None


def _require_brand(bid: int) -> Dict[str, Any]:
    b = get_brand(bid)
    if not b:
        raise ValueError("品牌档案不存在")
    return b


def create_brand(name: str, keywords: str = "", remark: str = "") -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写品牌名称")
    _ensure()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fc_brands (user_id, name, keywords, remark, created_at) VALUES (?,?,?,?,?)",
            (current_user_id(), name, (keywords or "").strip(), (remark or "").strip(), int(time.time())))
        return cur.lastrowid


def update_brand(bid: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    _require_brand(bid)
    sets, params = [], []
    for k in ("name", "keywords", "remark"):
        v = fields.get(k)
        if isinstance(v, str):
            if k == "name" and not v.strip():
                raise ValueError("品牌名称不能为空")
            sets.append(f"{k}=?")
            params.append(v.strip())
    if not sets:
        raise ValueError("没有可更新的字段")
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE fc_brands SET {', '.join(sets)} WHERE id=?", (*params, bid))
    return get_brand(bid)  # type: ignore[return-value]


def delete_brand(bid: int) -> bool:
    _require_brand(bid)
    with _lock, _connect() as conn:
        for t in ("fc_features", "fc_suspects", "fc_sweep_tasks"):
            conn.execute(f"DELETE FROM {t} WHERE brand_id=?", (bid,))
        bids = [r["id"] for r in conn.execute(
            "SELECT id FROM fc_batches WHERE brand_id=?", (bid,)).fetchall()]
        for x in bids:
            conn.execute("DELETE FROM fc_batch_items WHERE batch_id=?", (x,))
        conn.execute("DELETE FROM fc_batches WHERE brand_id=?", (bid,))
        conn.execute("DELETE FROM fc_brands WHERE id=?", (bid,))
    return True


# ---------------- 特征库 ----------------

def _feat_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = {"id": r["id"], "brand_id": r["brand_id"], "kind": r["kind"],
         "kind_label": KIND_LABELS.get(r["kind"], r["kind"]),
         "value": r["value"], "weight": r["weight"],
         "enabled": bool(r["enabled"]), "created_at": r["created_at"]}
    # pattern_id 列可能在极老库中缺失（_ensure 已做迁移，此处防御）
    try:
        d["pattern_id"] = r["pattern_id"] or ""
    except (IndexError, KeyError):
        d["pattern_id"] = ""
    return d


def list_features(brand_id: int, kind: str = "") -> List[Dict[str, Any]]:
    _require_brand(brand_id)
    q = "SELECT * FROM fc_features WHERE brand_id=?"
    params: List[Any] = [brand_id]
    if kind:
        q += " AND kind=?"
        params.append(kind)
    q += " ORDER BY kind, weight DESC, id"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_feat_to_dict(r) for r in rows]


def _enabled_features(brand_id: int, kind: str) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fc_features WHERE brand_id=? AND kind=? AND enabled=1",
            (brand_id, kind)).fetchall()
    return [_feat_to_dict(r) for r in rows]


def save_feature(brand_id: int, kind: str, value: str, weight: int = 0,
                 pattern_id: str = "") -> Dict[str, Any]:
    _require_brand(brand_id)
    if kind not in KIND_LABELS:
        raise ValueError(f"未知的特征种类: {kind}")
    value = (value or "").strip()
    if not value:
        raise ValueError("特征值不能为空")
    _ensure()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO fc_features (user_id, brand_id, kind, value, weight, enabled, pattern_id, created_at)"
            " VALUES (?,?,?,?,?,1,?,?)"
            " ON CONFLICT(brand_id, kind, value) DO UPDATE SET weight=excluded.weight,"
            " enabled=1, pattern_id=excluded.pattern_id",
            (current_user_id(), brand_id, kind, value, int(weight or 0), (pattern_id or "").strip(),
             int(time.time())))
        r = conn.execute(
            "SELECT * FROM fc_features WHERE brand_id=? AND kind=? AND value=?",
            (brand_id, kind, value)).fetchone()
    return _feat_to_dict(r)


def _require_feature(fid: int) -> Dict[str, Any]:
    _ensure()
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fc_features WHERE id=?", (fid,)).fetchone()
    if not r:
        raise ValueError("特征不存在")
    _require_brand(r["brand_id"])
    return _feat_to_dict(r)


def update_feature(fid: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    sets, params = [], []
    if "weight" in fields:
        sets.append("weight=?")
        params.append(int(fields["weight"] or 0))
    if "enabled" in fields:
        sets.append("enabled=?")
        params.append(1 if fields["enabled"] else 0)
    if "value" in fields and isinstance(fields["value"], str) and fields["value"].strip():
        sets.append("value=?")
        params.append(fields["value"].strip())
    if not sets:
        raise ValueError("没有可更新的字段")
    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE fc_features SET {', '.join(sets)} WHERE id=?",
                           (*params, fid))
        if not cur.rowcount:
            raise ValueError("特征不存在")
        r = conn.execute("SELECT * FROM fc_features WHERE id=?", (fid,)).fetchone()
    return _feat_to_dict(r)


def delete_feature(fid: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM fc_features WHERE id=?", (fid,))
        return cur.rowcount > 0


# ---------------- 判定模式库（P0 升级） ----------------

_PATTERNS_CACHE: Optional[Dict[str, Any]] = None


def _patterns_path() -> Path:
    return Path(__file__).resolve().parent / "impersonation_patterns.json"


def load_patterns() -> Dict[str, Any]:
    """加载判定模式库 JSON（带缓存）。"""
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is None:
        try:
            _PATTERNS_CACHE = json.loads(_patterns_path().read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("判定模式库读取失败：%s", e)
            _PATTERNS_CACHE = {"patterns": [], "official_stores": {},
                               "licensed_dutyfree_operators": {}, "brand": ""}
    return _PATTERNS_CACHE


def get_pattern(pid: str) -> Optional[Dict[str, Any]]:
    """按 P01..P08 取单个模式定义。"""
    for p in load_patterns().get("patterns") or []:
        if p.get("id") == pid:
            return p
    return None


def pattern_legal_text(pid: str) -> str:
    """模式的法律依据文本，如「《商标法》第57条第(二)项」。"""
    p = get_pattern(pid)
    if not p:
        return ""
    parts = []
    for lb in (p.get("legal_basis") or []):
        parts.append(f"《{lb.get('law', '')}》{lb.get('article', '')}")
    return "；".join(parts)


def import_pattern_features(brand_id: int) -> Dict[str, int]:
    """把判定模式库 8 个模式的触发关键词导入该品牌的特征库。

    每个特征标注 pattern_id 溯源到模式。幂等：已存在的（brand_id, kind, value）
    会更新 weight/enabled/pattern_id，不重复插入。
    返回 {"imported": n, "patterns": [...]}。
    """
    _require_brand(brand_id)
    pats = load_patterns()
    imported = 0
    used_pids: List[str] = []
    for p in (pats.get("patterns") or []):
        pid = p.get("id") or ""
        kind = p.get("feature_kind")  # null 表示需代码逻辑（P03 已有实现，P06 需图算法）
        weight = int(p.get("score_weight") or 0)
        triggers = p.get("triggers") or {}
        keywords: List[str] = []
        if kind and pid not in used_pids:
            used_pids.append(pid)
        if not kind:
            continue
        # P02 特殊处理：免税关键词应为 bio_kw（简介含免税词），
        # subject_type 联动（个体工商户）在 score_candidate 里做，不在此导入
        import_kind = kind
        if pid == "P02":
            import_kind = "bio_kw"
        # P01 门店关键词 / P04 KOS / P05 引流规避 / P08 平台处罚 → bio_kw 或 nickname_kw
        # P02 免税词 → bio_kw（subject_type 联动在 score_candidate 里做）
        for kw in (triggers.get("keywords") or []):
            kw = str(kw).strip()
            if kw:
                keywords.append(kw)
        for pt in (triggers.get("patterns") or []):  # P05 的规避话术 patterns
            pt = str(pt).strip()
            if pt:
                keywords.append(pt)
        for kw in keywords:
            try:
                save_feature(brand_id, import_kind, kw, weight, pattern_id=pid)
                imported += 1
            except Exception as e:
                logger.warning("模式特征导入失败 %s/%s: %s", pid, kw, e)
    # P01 官方宣称词（official_claim_words）也作为 bio_kw 导入，权重减半
    p01 = get_pattern("P01")
    if p01:
        ocw = ((p01.get("triggers") or {}).get("official_claim_words") or [])
        for w in ocw:
            w = str(w).strip()
            if w:
                try:
                    save_feature(brand_id, "bio_kw", w, 12, pattern_id="P01")
                    imported += 1
                except Exception:
                    pass
        if "P01" not in used_pids:
            used_pids.append("P01")
    # P04 官方口吻词（official_tone_words）导入，权重减半，标注需人工复核在 scoring 侧处理
    p04 = get_pattern("P04")
    if p04:
        otw = ((p04.get("triggers") or {}).get("official_tone_words") or [])
        for w in otw:
            w = str(w).strip()
            if w:
                try:
                    save_feature(brand_id, "bio_kw", w, 10, pattern_id="P04")
                    imported += 1
                except Exception:
                    pass
        if "P04" not in used_pids:
            used_pids.append("P04")
    logger.info("品牌 %d 导入判定模式特征 %d 条，覆盖模式 %s", brand_id, imported, used_pids)
    return {"imported": imported, "patterns": used_pids}

# ---------------- 嫌疑库 ----------------

SUSPECT_COLS = ("brand_id", "nickname", "xhs_id", "uid", "subject", "subject_type",
                "ip_location", "bio", "notes", "credit_code", "operator",
                "reg_address", "established_date", "source", "score", "level",
                "flags", "verified", "profile_url", "created_at")


def _suspect_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = {c: r[c] for c in SUSPECT_COLS if c in r.keys()}
    d["id"] = r["id"]
    try:
        d["flags"] = json.loads(d.get("flags") or "[]")
    except Exception:
        d["flags"] = []
    d["level_label"] = LEVEL_LABELS.get(d.get("level"), d.get("level"))
    d["verified_label"] = VERIFY_LABELS.get(d.get("verified"), "未核验")
    return d


def _store_claim_check(text: str) -> Tuple[List[str], List[str]]:
    """门店宣称检查（P01）：返回 (命中的虚假门店, 命中的真实门店关键词)。

    虚假门店：官方清单明确无此门店（如威尼斯人），声称即虚假。
    真实门店：官方清单内的门店，提及需核验身份。
    """
    pats = load_patterns()
    stores = pats.get("official_stores") or {}
    fake_claims = [str(x).strip() for x in (stores.get("fake_store_claims") or []) if str(x).strip()]
    # 真实门店用 P01 触发关键词中的门店短名（更贴合简介写法）
    p01 = get_pattern("P01") or {}
    real_kws = [str(k).strip() for k in ((p01.get("triggers") or {}).get("keywords") or [])
                if str(k).strip()]
    # 排除掉同时是虚假宣称的（以 fake 为准）
    fake_set = set(c.lower() for c in fake_claims)
    real_kws = [k for k in real_kws if k.lower() not in fake_set]
    t = (text or "").lower()
    hit_fake = [c for c in fake_claims if c.lower() in t]
    hit_real = [k for k in real_kws if k.lower() in t]
    return hit_fake, hit_real


def score_candidate(brand_id: int, nickname: str = "", bio: str = "",
                    ip_location: str = "", subject: str = "",
                    subject_type: str = "") -> Tuple[int, str, List[str]]:
    """嫌疑评分（全走该品牌的特征库配置，无 hardcode；P0 升级加入判定模式）。

    返回 (score, level, flags)。level: high>=60 / mid>=35 / low。
    P04（KOS）命中时 flag 标注"需人工复核"，不单独决定高风险。
    """
    brand = _require_brand(brand_id)
    brand_kws = [k.strip().lower() for k in (brand.get("keywords") or "").split(",")
                 if k.strip()]
    nick = nickname or ""
    bio_t = bio or ""
    flags: List[str] = []
    score = 0
    matched_pids: List[str] = []

    def _add_pid(pid: str) -> None:
        if pid and pid not in matched_pids:
            matched_pids.append(pid)

    # ---- 特征库命中（带 pattern_id 溯源） ----
    nick_hits = []
    nick_pids: List[str] = []
    for f in _enabled_features(brand_id, "nickname_kw"):
        if f["value"].lower() in nick.lower():
            nick_hits.append(f["value"])
            score += int(f["weight"] or 0)
            if f.get("pattern_id"):
                nick_pids.append(f["pattern_id"])
    if nick_hits:
        score = min(score, 100)  # 昵称项先累加，后续统一封顶
        flags.append(f"昵称命中关键词：{'、'.join(nick_hits)}")
        for pid in nick_pids:
            _add_pid(pid)

    bio_hits = []
    bio_pids: List[str] = []
    for f in _enabled_features(brand_id, "bio_kw"):
        if f["value"].lower() in bio_t.lower():
            bio_hits.append(f["value"])
            if f.get("pattern_id"):
                bio_pids.append(f["pattern_id"])
    if bio_hits:
        # P0 升级：有 pattern_id 的特征按其权重计分（P01/P02=25，P04/P05=20）；
        # 无 pattern_id 的沿用原逻辑（每词 8 分，封顶 24）
        # P04 KOS 命中必须标注需人工复核
        pattern_hits = []
        other_hits = []
        has_p04 = False
        for f in _enabled_features(brand_id, "bio_kw"):
            if f["value"] in bio_hits:
                pid = (f.get("pattern_id") or "").strip()
                if pid:
                    pattern_hits.append(f)
                    if pid == "P04":
                        has_p04 = True
                else:
                    other_hits.append(f)
        for f in pattern_hits:
            score += int(f["weight"] or 0)
        if other_hits:
            score += min(len(other_hits) * 8, 24)
        flag_txt = f"简介命中伪冒词：{'、'.join(bio_hits)}"
        # P04 必须标注需人工复核（可能是真实员工，不能全自动定高风险）
        if has_p04:
            flag_txt += "【需人工复核：可能是真实员工/柜姐，勿直接定性】"
            _add_pid("P04")
        flags.append(flag_txt)
        for pid in bio_pids:
            _add_pid(pid)

    for f in _enabled_features(brand_id, "ip"):
        if (ip_location or "").strip().lower() == f["value"].strip().lower():
            score += 15
            flags.append(f"IP 属地命中：{f['value']}")
            if f.get("pattern_id"):
                _add_pid(f["pattern_id"])
            break

    for f in _enabled_features(brand_id, "subject_type"):
        if (subject_type or "").strip() == f["value"].strip():
            score += 10
            flags.append(f"认证主体类型命中：{f['value']}")
            if f.get("pattern_id"):
                _add_pid(f["pattern_id"])
            break

    if subject and brand_kws and not any(k in subject.lower() for k in brand_kws):
        score += 15
        flags.append(f"蓝V认证主体「{subject}」与品牌「{brand['name']}」无关")
        _add_pid("P03")

    # ---- P01 门店清单对照 ----
    hit_fake, hit_real = _store_claim_check(f"{nick} {bio_t}")
    if hit_fake:
        # 虚假门店：官方明确无此门店，声称即虚假，直接高风险
        score = max(score + 50, 65)
        flags.append(f"声称虚假门店「{'、'.join(hit_fake)}」（官方门店清单无此门店，涉嫌虚假宣传）")
        _add_pid("P01")
    elif hit_real and not (subject or "").strip():
        # 真实门店但无认证：需人工核验
        score += 15
        flags.append(f"提及官方门店「{'、'.join(hit_real)}」但无认证，需人工核验身份")
        _add_pid("P01")

    # ---- P02 经营范围不符（免税词 + 个体工商户/非持牌主体） ----
    p02 = get_pattern("P02") or {}
    df_kws = [str(k).strip() for k in ((p02.get("triggers") or {}).get("keywords") or []) if str(k).strip()]
    has_df_word = any(k.lower() in bio_t.lower() for k in df_kws)
    if has_df_word:
        st = (subject_type or "").strip()
        ops = (load_patterns().get("licensed_dutyfree_operators") or {}).get("operators") or []
        subj = (subject or "").strip()
        in_licensed = any(op and op in subj for op in ops) if subj else False
        if "个体工商户" in st:
            score += 25
            flags.append("自称免税但认证主体为个体工商户（不可能持有免税牌照），涉嫌虚假宣传")
            _add_pid("P02")
        elif subj and not in_licensed:
            score += 20
            flags.append(f"自称免税但认证主体「{subj}」不在持牌免税企业名单，涉嫌虚假宣传")
            _add_pid("P02")

    # ---- P08 平台已处罚 ----
    if "禁言中" in nick:
        score += 20
        flags.append("昵称含「禁言中」，曾被平台处罚")
        _add_pid("P08")

    score = min(max(score, 0), 100)
    level = "high" if score >= 60 else ("mid" if score >= 35 else "low")
    # 将命中的模式 ID 附在 flags 末尾（供 complaint_material 溯源法律依据，不影响展示）
    if matched_pids:
        flags.append(f"__patterns__:{','.join(matched_pids)}")
    return score, level, flags


def get_matched_patterns(flags: List[str]) -> List[str]:
    """从 flags 中提取命中的模式 ID（__patterns__:P01,P02 标记）。"""
    for f in (flags or []):
        if isinstance(f, str) and f.startswith("__patterns__:"):
            return [p.strip() for p in f[len("__patterns__:"):].split(",") if p.strip()]
    return []


def _suspect_exists(brand_id: int, xhs_id: str, uid: str, nickname: str) -> bool:
    with _connect() as conn:
        if xhs_id:
            r = conn.execute(
                "SELECT id FROM fc_suspects WHERE brand_id=? AND xhs_id=?",
                (brand_id, xhs_id)).fetchone()
            if r:
                return True
        if uid:
            r = conn.execute(
                "SELECT id FROM fc_suspects WHERE brand_id=? AND uid=?",
                (brand_id, uid)).fetchone()
            if r:
                return True
        if nickname:
            r = conn.execute(
                "SELECT id FROM fc_suspects WHERE brand_id=? AND nickname=? AND xhs_id=''",
                (brand_id, nickname)).fetchone()
            if r:
                return True
    return False


def create_suspect(brand_id: int, data: Dict[str, Any],
                   auto_score: bool = True) -> Dict[str, Any]:
    _require_brand(brand_id)
    nickname = str(data.get("nickname") or "").strip()
    xhs_id = str(data.get("xhs_id") or "").strip()
    uid = str(data.get("uid") or "").strip()
    if not (nickname or xhs_id or uid):
        raise ValueError("昵称 / 账号ID / UID 至少填一项")
    if _suspect_exists(brand_id, xhs_id, uid, nickname):
        raise ValueError("该账号已在嫌疑库中（去重）")
    subject = str(data.get("subject") or "").strip()
    subject_type = str(data.get("subject_type") or "").strip()
    ip_location = str(data.get("ip_location") or "").strip()
    bio = str(data.get("bio") or "").strip()
    if auto_score:
        score, level, flags = score_candidate(
            brand_id, nickname, bio, ip_location, subject, subject_type)
    else:
        score, level, flags = int(data.get("score") or 0), str(data.get("level") or "low"), []
    if not profile_url_valid(data.get("profile_url")) and not uid:
        pass
    profile_url = str(data.get("profile_url") or "").strip() or (
        f"https://www.xiaohongshu.com/user/profile/{uid}" if uid else "")
    _ensure()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fc_suspects (user_id, brand_id, nickname, xhs_id, uid, subject, subject_type,"
            " ip_location, bio, notes, credit_code, operator, reg_address, established_date,"
            " source, score, level, flags, verified, profile_url, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (current_user_id(), brand_id, nickname, xhs_id, uid, subject, subject_type, ip_location, bio,
             str(data.get("notes") or "").strip(), str(data.get("credit_code") or "").strip(),
             str(data.get("operator") or "").strip(), str(data.get("reg_address") or "").strip(),
             str(data.get("established_date") or "").strip(),
             str(data.get("source") or "manual"), score, level, json.dumps(flags, ensure_ascii=False),
             int(data.get("verified") or 0), profile_url, int(time.time())))
        sid = cur.lastrowid
    return get_suspect(sid)  # type: ignore[return-value]


def profile_url_valid(u: Any) -> bool:
    return isinstance(u, str) and u.strip().startswith("http")


def get_suspect(sid: int) -> Optional[Dict[str, Any]]:
    _ensure()
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fc_suspects WHERE id=?", (sid,)).fetchone()
    return _suspect_to_dict(r) if r else None


def list_suspects(brand_id: int, level: str = "", verified: str = "",
                  source: str = "", keyword: str = "",
                  limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    _require_brand(brand_id)
    q = "SELECT * FROM fc_suspects WHERE brand_id=?"
    c = "SELECT COUNT(*) n FROM fc_suspects WHERE brand_id=?"
    params: List[Any] = [brand_id]
    if level:
        q += " AND level=?"; c += " AND level=?"; params.append(level)
    if verified != "":
        q += " AND verified=?"; c += " AND verified=?"; params.append(int(verified))
    if source:
        q += " AND source=?"; c += " AND source=?"; params.append(source)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q += " AND (nickname LIKE ? OR subject LIKE ? OR xhs_id LIKE ?)"
        c += " AND (nickname LIKE ? OR subject LIKE ? OR xhs_id LIKE ?)"
        params += [kw, kw, kw]
    q += " ORDER BY score DESC, id DESC LIMIT ? OFFSET ?"
    with _connect() as conn:
        total = conn.execute(c, params).fetchone()["n"]
        rows = conn.execute(q, (*params, limit, offset)).fetchall()
    return {"total": total, "items": [_suspect_to_dict(r) for r in rows]}


def _require_suspect(sid: int) -> Dict[str, Any]:
    """取嫌疑并校验归属（非本人品牌的抛 ValueError）。"""
    d = get_suspect(sid)
    if not d:
        raise ValueError("嫌疑账号不存在或无权访问")
    return d


def update_suspect(sid: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    s = get_suspect(sid)
    if not s:
        raise ValueError("嫌疑账号不存在")
    allowed = ("nickname", "xhs_id", "uid", "subject", "subject_type", "ip_location",
               "bio", "notes", "credit_code", "operator", "reg_address",
               "established_date", "verified", "profile_url")
    sets, params = [], []
    for k in allowed:
        if k in fields:
            v = fields[k]
            sets.append(f"{k}=?")
            params.append(int(v) if k == "verified" else str(v or "").strip())
    rescore = bool(fields.get("rescore"))
    if rescore:
        merged = dict(s)
        merged.update({k: fields[k] for k in allowed if k in fields})
        score, level, flags = score_candidate(
            s["brand_id"], merged.get("nickname", ""), merged.get("bio", ""),
            merged.get("ip_location", ""), merged.get("subject", ""),
            merged.get("subject_type", ""))
        sets += ["score=?", "level=?", "flags=?"]
        params += [score, level, json.dumps(flags, ensure_ascii=False)]
    if not sets:
        raise ValueError("没有可更新的字段")
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE fc_suspects SET {', '.join(sets)} WHERE id=?",
                     (*params, sid))
    return get_suspect(sid)  # type: ignore[return-value]


def delete_suspect(sid: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM fc_suspects WHERE id=?", (sid,))
        return cur.rowcount > 0


def suspect_stats(brand_id: int) -> Dict[str, Any]:
    _require_brand(brand_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT level, verified, COUNT(*) n FROM fc_suspects WHERE brand_id=?"
            " GROUP BY level, verified", (brand_id,)).fetchall()
    by_level = {"high": 0, "mid": 0, "low": 0}
    by_verified = {"0": 0, "1": 0, "2": 0}
    for r in rows:
        by_level[r["level"]] = by_level.get(r["level"], 0) + r["n"]
        by_verified[str(r["verified"])] = by_verified.get(str(r["verified"]), 0) + r["n"]
    return {"by_level": by_level, "by_verified": by_verified,
            "total": sum(by_level.values())}

# ---------------- 名单导入 ----------------

IMPORT_COLUMNS = ["昵称", "账号ID", "UID", "认证主体", "主体类型", "IP属地", "简介", "备注",
                  "统一信用代码", "经营者", "注册地址", "成立日期", "主页链接"]
# 兼容英文列名
IMPORT_ALIASES = {
    "nickname": "昵称", "xhs_id": "账号ID", "uid": "UID", "subject": "认证主体",
    "subject_type": "主体类型", "ip_location": "IP属地", "bio": "简介",
    "notes": "备注", "credit_code": "统一信用代码", "operator": "经营者",
    "reg_address": "注册地址", "established_date": "成立日期",
    "profile_url": "主页链接",
}


def import_template() -> bytes:
    """生成 Excel 导入模板（含填写说明）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "伪冒名单"
    for c, h in enumerate(IMPORT_COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True)
    ws.append(["示例昵称", "123456", "abc123", "某某百货店", "个体工商户",
               "Macao, China", "简介示例", "", "", "", "", ""])
    ws.sheet_properties.tabColor = "E05D5D"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_excel(brand_id: int, file_path: str) -> Dict[str, Any]:
    """导入 Excel 名单。返回 {imported, skipped, errors}。

    已确认的名单视为 verified=1（用户已人工核验过这两批）。
    """
    from openpyxl import load_workbook
    _require_brand(brand_id)
    wb = load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Excel 为空")
    header = [(str(c or "").strip()) for c in rows[0]]
    # 列名归一化
    norm = [IMPORT_ALIASES.get(h.lower(), h) for h in header]
    col = {name: i for i, name in enumerate(norm)}
    keymap = {"昵称": "nickname", "账号ID": "xhs_id", "UID": "uid",
              "认证主体": "subject", "主体类型": "subject_type",
              "IP属地": "ip_location", "简介": "bio", "备注": "notes",
              "统一信用代码": "credit_code", "经营者": "operator",
              "注册地址": "reg_address", "成立日期": "established_date",
              "主页链接": "profile_url"}
    imported, skipped, errors = 0, 0, []
    for ln, r in enumerate(rows[1:], 2):
        try:
            data: Dict[str, Any] = {"source": "import", "verified": 1}
            for cn, k in keymap.items():
                if cn in col and col[cn] < len(r) and r[col[cn]] is not None:
                    data[k] = str(r[col[cn]]).strip()
            if not (data.get("nickname") or data.get("xhs_id") or data.get("uid")):
                skipped += 1
                continue
            try:
                create_suspect(brand_id, data, auto_score=True)
                imported += 1
            except ValueError as e:
                if "已在嫌疑库" in str(e):
                    skipped += 1
                else:
                    errors.append(f"第{ln}行：{e}")
        except Exception as e:
            errors.append(f"第{ln}行：{e}")
    return {"imported": imported, "skipped": skipped, "errors": errors[:20]}


# ---------------- Sweep 排查 ----------------

def check_sweep_login() -> Tuple[bool, str]:
    """Sweep 必须登录态：复用 Cortech 统一采集器的登录检查。"""
    try:
        from . import cortech_crawler
        ok, msg = cortech_crawler.check_login()
        return ok, msg
    except Exception as e:
        return False, f"登录检查失败：{e}"


def _sweep_update(tid: int, **kw: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE fc_sweep_tasks SET {sets} WHERE id=?",
                     (*kw.values(), tid))


# ---------------- 关键词矩阵 ----------------

# 通用变体后缀：品牌名 + 后缀生成排查关键词
_CORTECH_SWEEP_SUFFIXES = [
    "代购", "折扣", "优惠", "促销", "会员", "黑卡", "小助手",
    "客服", "福利", "柜姐", "导购", "正品", "免税店",
]


def expand_keyword_matrix(brand_id: int) -> List[str]:
    """关键词矩阵：品牌配置词 + 品牌名变体自动扩展，去重返回。

    用于系统化 Sweep，确保几十个变体词全覆盖，不只搜 2-3 个词。
    """
    brand = _require_brand(brand_id)
    base_kws = [f["value"] for f in _enabled_features(brand_id, "sweep_kw")]
    name = (brand.get("name") or "").strip()
    variants: List[str] = []
    if name:
        for suf in _CORTECH_SWEEP_SUFFIXES:
            variants.append(f"{name}{suf}")
        # 英文品牌名加小写变体
        if name.isascii():
            variants.append(name.lower())
            for suf in _CORTECH_SWEEP_SUFFIXES:
                variants.append(f"{name.lower()}{suf}")
    merged = list(dict.fromkeys(
        [k.strip() for k in (base_kws + variants) if k and k.strip()]))
    return merged


def start_sweep(brand_id: int, keywords: Optional[List[str]] = None,
                max_notes: int = 30, account_id: Optional[int] = None,
                name: str = "") -> int:
    """启动 Sweep（后台线程）。无登录态时直接抛错，前端据此提示扫码。

    keywords 为空时自动使用关键词矩阵（配置词 + 变体扩展）。
    """
    brand = _require_brand(brand_id)
    ok, msg = check_sweep_login()
    if not ok:
        raise ValueError(f"需要先扫码登录小红书账号：{msg}")
    if keywords is None:
        keywords = expand_keyword_matrix(brand_id)
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        raise ValueError("没有可用的搜索关键词，请先在特征库配置 sweep 搜索词")
    max_notes = max(5, min(int(max_notes or 30), 200))
    if account_id is None:
        d = db.get_default_account()
        account_id = d["id"] if d else None
    _ensure()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fc_sweep_tasks (brand_id, name, keywords, max_notes, account_id,"
            " status, progress, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (brand_id, name or f"{brand['name']} Sweep", json.dumps(keywords, ensure_ascii=False),
             max_notes, account_id, "running", "准备开始", int(time.time())))
        tid = cur.lastrowid
    t = threading.Thread(target=_run_sweep,
                         args=(tid, brand_id, keywords, max_notes, account_id),
                         daemon=True)
    t.start()
    return tid


def _run_sweep(tid: int, brand_id: int, keywords: List[str],
               max_notes: int, account_id: Optional[int]) -> None:
    from . import cortech_crawler
    from .cortech_crawler import CrawlContext
    import tempfile
    found = 0
    try:
        crawler = cortech_crawler.get_crawler()
        total_kw = len(keywords)
        for i, kw in enumerate(keywords):
            _sweep_update(tid, progress=f"搜索关键词 {i+1}/{total_kw}：{kw}")
            with tempfile.TemporaryDirectory(prefix="fcsweep_") as tmpd:
                ctx = CrawlContext(
                    topic_id=0, topic_name="伪冒排查", keywords=[kw],
                    max_notes=max_notes, enable_comments=False,
                    max_comments_per_note=0, run_dir=Path(tmpd), task_id=0,
                    timeout_sec=120, account_id=account_id)
                try:
                    result = crawler.crawl(ctx)
                except Exception as e:
                    logger.warning("Sweep 关键词 %s 采集失败：%s", kw, e)
                    continue
            seen_authors = set()
            for note in result.notes:
                a = note.author
                aid = (a.author_id or "").strip()
                key = aid or (a.nickname or "").strip()
                if not key or key in seen_authors:
                    continue
                seen_authors.add(key)
                # 宁误报不漏报：搜索命中即入库候选，人工复核
                if _suspect_exists(brand_id, "", aid, a.nickname or ""):
                    continue
                try:
                    create_suspect(brand_id, {
                        "nickname": a.nickname or "",
                        "xhs_id": "",
                        "uid": aid,
                        "ip_location": a.ip_location or note.ip_location or "",
                        "bio": "",
                        "notes": f"Sweep 命中搜索词「{kw}」",
                        "source": "sweep",
                        "verified": 0,
                        "profile_url": (f"https://www.xiaohongshu.com/user/profile/{aid}"
                                        if aid else ""),
                    }, auto_score=True)
                    found += 1
                except ValueError:
                    pass
            _sweep_update(tid, found_count=found,
                          progress=f"已完成 {i+1}/{total_kw}，新发现 {found} 个候选")
        _sweep_update(tid, status="done", finished_at=int(time.time()),
                      progress=f"完成，共新发现 {found} 个候选账号（待人工复核）")
    except Exception as e:
        logger.exception("Sweep 任务失败")
        _sweep_update(tid, status="failed", error=str(e)[:500],
                      finished_at=int(time.time()))


def list_sweep_tasks(brand_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    _require_brand(brand_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fc_sweep_tasks WHERE brand_id=? ORDER BY id DESC LIMIT ?",
            (brand_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except Exception:
            d["keywords"] = []
        out.append(d)
    return out


def get_sweep_task(tid: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fc_sweep_tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["keywords"] = json.loads(d.get("keywords") or "[]")
    except Exception:
        d["keywords"] = []
    return d

# ---------------- 主体聚类 ----------------

def cluster_subjects(brand_id: int) -> Dict[str, Any]:
    """按经营者 / 统一信用代码 / 注册地址 / 认证主体聚类（≥2 账号才成团）。

    主体核验走公开来源（国家标准校验码、Wikidata、信用中国、公示系统公开页）。
    公示系统需要验证码时会降级。请在嫌疑详情页手动录入经营者/信用代码/地址后参与聚类。
    """
    _require_brand(brand_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, nickname, xhs_id, subject, subject_type, credit_code,"
            " operator, reg_address, score, level, verified"
            " FROM fc_suspects WHERE brand_id=? AND verified != 2",
            (brand_id,)).fetchall()
    items = [dict(r) for r in rows]

    def _groups(key: str, label: str) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for it in items:
            v = (it.get(key) or "").strip()
            if not v:
                continue
            buckets.setdefault(v, []).append(it)
        out = []
        for v, members in buckets.items():
            if len(members) >= 2:
                out.append({"key": v, "label": label, "count": len(members),
                            "members": [{"id": m["id"], "nickname": m["nickname"],
                                         "xhs_id": m["xhs_id"], "level": m["level"],
                                         "score": m["score"]} for m in members]})
        out.sort(key=lambda g: -g["count"])
        return out

    return {
        "by_operator": _groups("operator", "同一经营者"),
        "by_credit_code": _groups("credit_code", "同一信用代码"),
        "by_address": _groups("reg_address", "同一注册地址"),
        "by_subject": _groups("subject", "同一认证主体"),
        "total": len(items),
        "qcc_note": "主体核验使用公开来源，不调用企查查。"
                    "公示系统打不开或需要验证码时会降级，"
                    "可在嫌疑详情页手动录入经营者/信用代码/地址后参与聚类。",
    }


def qcc_lookup(name: str) -> Dict[str, Any]:
    """主体核验。保留原路径名，实现改为公开来源，不再调用企查查。"""
    from .entity_verify import lookup
    return lookup(name)


# ---------------- 证据包导出 ----------------

def export_csv(brand_id: int, level: str = "", verified: str = "") -> bytes:
    """嫌疑名单 CSV（UTF-8 BOM，Excel 直接打开不乱码）。"""
    data = list_suspects(brand_id, level=level, verified=verified,
                         limit=100000, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["昵称", "账号ID", "UID", "主页链接", "认证主体", "主体类型",
                "IP属地", "简介", "统一信用代码", "经营者", "注册地址", "成立日期",
                "嫌疑评分", "嫌疑等级", "命中红旗", "核验状态", "来源", "备注"])
    for s in data["items"]:
        w.writerow([s.get("nickname"), s.get("xhs_id"), s.get("uid"),
                    s.get("profile_url"), s.get("subject"), s.get("subject_type"),
                    s.get("ip_location"), s.get("bio"), s.get("credit_code"),
                    s.get("operator"), s.get("reg_address"), s.get("established_date"),
                    s.get("score"),
                    s.get("level_label"), "；".join(s.get("flags") or []),
                    s.get("verified_label"), s.get("source"), s.get("notes")])
    return "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")


def complaint_material(sid: int) -> Dict[str, Any]:
    """单个账号的投诉材料单页：基本信息 + 伪冒证据链 + 投诉理由文案。"""
    s = get_suspect(sid)
    if not s:
        raise ValueError("嫌疑账号不存在")
    brand = _require_brand(s["brand_id"])
    evidence = []
    for f in (s.get("flags") or []):
        # 跳过内部模式标记（__patterns__:P01,P02），不展示给用户
        if isinstance(f, str) and f.startswith("__patterns__:"):
            continue
        evidence.append(f)
    if s.get("subject"):
        evidence.append(f"蓝V认证主体为「{s['subject']}」（{s.get('subject_type') or '未知类型'}）")
    if s.get("operator"):
        evidence.append(f"经营者：{s['operator']}")
    if s.get("credit_code"):
        evidence.append(f"统一社会信用代码：{s['credit_code']}")
    if s.get("ip_location"):
        evidence.append(f"IP 属地：{s['ip_location']}")
    # 命中模式 → 法律依据
    matched_pids = get_matched_patterns(s.get("flags") or [])
    if not matched_pids:
        # 存量数据无标记时，用当前数据重新跑一遍评分以溯源模式
        try:
            _, _, fresh_flags = score_candidate(
                s["brand_id"], s.get("nickname", ""), s.get("bio", ""),
                s.get("ip_location", ""), s.get("subject", ""),
                s.get("subject_type", ""))
            matched_pids = get_matched_patterns(fresh_flags)
        except Exception:
            pass
    legal_refs: List[str] = []
    for pid in matched_pids:
        p = get_pattern(pid)
        if not p:
            continue
        lb_txt = pattern_legal_text(pid)
        if lb_txt and lb_txt not in legal_refs:
            legal_refs.append(f"{pid}（{p.get('name', '')}）：{lb_txt}")
    reason = (
        f"投诉对象「{s.get('nickname') or s.get('xhs_id')}」（{s.get('profile_url') or '主页链接见CSV'}）"
        f"涉嫌假冒 {brand['name']} 品牌："
        f"{'；'.join(evidence) if evidence else '（证据待补充）'}。"
        f"该账号以品牌官方/导购/KOS 人设发布内容、蹭品牌流量，"
        f"认证主体与 {brand['name']} 无任何关联，涉嫌虚假营销甚至假货销售，"
        f"请求平台核查并处理。"
    )
    return {"suspect": s, "brand": brand["name"], "evidence": evidence,
            "complaint_reason": reason,
            "matched_patterns": matched_pids,
            "legal_refs": legal_refs,
            "note": "请在小红书 App 内按此材料提交投诉；大批量维权建议走小红书品牌维权官方通道（需品牌方资质），效率更高。"}


def complaint_material_markdown(sid: int) -> str:
    _require_suspect(sid)
    """生成 Markdown 格式的举报材料，可直接复制提交或导出文件。"""
    import datetime
    m = complaint_material(sid)
    s = m["suspect"]
    brand = m["brand"]
    matched_pids: List[str] = m.get("matched_patterns") or []
    legal_refs: List[str] = m.get("legal_refs") or []
    lines = [
        f"# 伪冒账号举报材料",
        f"",
        f"**品牌**：{brand}",
        f"**生成时间**：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"## 1. 账号基本信息",
        f"",
        f"| 项目 | 内容 |",
        f"|------|------|",
        f"| 昵称 | {s.get('nickname') or '-'} |",
        f"| 小红书号 | {s.get('xhs_id') or '-'} |",
        f"| UID | {s.get('uid') or '-'} |",
        f"| 粉丝数 | {s.get('fans') or '-'} |",
        f"| 发帖数 | {s.get('notes_count') or '-'} |",
        f"| IP 属地 | {s.get('ip_location') or '-'} |",
        f"| 认证状态 | {'已认证' if s.get('verified') else '未认证'} |",
        f"| 认证主体 | {s.get('subject') or '-'} |",
        f"| 主页链接 | {s.get('profile_url') or '-'} |",
        f"",
        f"## 2. 伪冒证据链",
        f"",
    ]
    ev = m.get("evidence") or []
    if ev:
        for i, e in enumerate(ev, 1):
            lines.append(f"{i}. {e}")
    else:
        lines.append("（证据待补充，请人工核验后添加）")
    # 命中判定模式
    if matched_pids:
        lines += [f"", f"**命中判定模式**："]
        for pid in matched_pids:
            p = get_pattern(pid)
            pname = p.get("name", pid) if p else pid
            lines.append(f"- {pid} {pname}")
    lines += [
        f"",
        f"## 3. 风险评级",
        f"",
        f"**{s.get('level') or '待定'}**（{s.get('score') or '-'} 分）",
        f"",
        f"## 4. 投诉理由",
        f"",
        m.get("complaint_reason") or "",
        f"",
        f"## 5. 法律依据汇总",
        f"",
    ]
    if legal_refs:
        for lr in legal_refs:
            lines.append(f"- {lr}")
    else:
        lines.append("（暂无匹配的法律依据，请人工补充）")
    lines += [
        f"",
        f"> ⚠️ 非律师意见，以上法律依据仅供参考，正式维权请品牌方法务复核。",
        f"",
        f"## 6. 备注",
        f"",
        m.get("note") or "",
        f"",
    ]
    return "\n".join(lines)


# ---------------- 分批次投诉管理（半自动） ----------------

BATCH_STATUS = {"pending": "待提交", "submitted": "已提交",
                "accepted": "已受理", "rejected": "被驳回"}


def create_batch(brand_id: int, name: str, suspect_ids: List[int],
                 batch_size: int = 5, interval_days: int = 3) -> Dict[str, Any]:
    _require_brand(brand_id)
    """创建分批次投诉任务。半自动：系统备好材料与批次节奏，
    用户在小红书 App 内提交，站内只记录状态。不做全自动批量举报。
    """
    _require_brand(brand_id)
    name = (name or "").strip() or f"投诉批次 {time.strftime('%Y%m%d-%H%M')}"
    batch_size = max(1, min(int(batch_size or 5), 50))
    interval_days = max(0, min(int(interval_days or 0), 30))
    ids = [int(x) for x in (suspect_ids or []) if x]
    if not ids:
        raise ValueError("请先选择要投诉的账号")
    _ensure()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fc_batches (brand_id, name, batch_size, interval_days, status, created_at)"
            " VALUES (?,?,?,?, 'open', ?)",
            (brand_id, name, batch_size, interval_days, int(time.time())))
        bid = cur.lastrowid
        now = int(time.time())
        for i, sid in enumerate(ids):
            conn.execute(
                "INSERT INTO fc_batch_items (batch_id, suspect_id, batch_no, status, updated_at)"
                " VALUES (?,?,?,?,?)",
                (bid, sid, i // batch_size + 1, "pending", now))
    return get_batch(bid)  # type: ignore[return-value]


def list_batches(brand_id: int) -> List[Dict[str, Any]]:
    _require_brand(brand_id)
    _require_brand(brand_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT b.*, COUNT(i.id) AS total,"
            " SUM(CASE WHEN i.status='submitted' OR i.status='accepted' THEN 1 ELSE 0 END) AS done"
            " FROM fc_batches b LEFT JOIN fc_batch_items i ON i.batch_id=b.id"
            " WHERE b.brand_id=? GROUP BY b.id ORDER BY b.id DESC",
            (brand_id,)).fetchall()
    return [dict(r) for r in rows]


def get_batch(bid: int) -> Optional[Dict[str, Any]]:
    _require_brand(bid)
    with _connect() as conn:
        b = conn.execute("SELECT * FROM fc_batches WHERE id=?", (bid,)).fetchone()
        if not b:
            return None
        batch = dict(b)
        rows = conn.execute(
            "SELECT i.*, s.nickname, s.xhs_id, s.subject, s.profile_url, s.level, s.score"
            " FROM fc_batch_items i LEFT JOIN fc_suspects s ON s.id=i.suspect_id"
            " WHERE i.batch_id=? ORDER BY i.batch_no, i.id", (bid,)).fetchall()
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for r in rows:
            it = dict(r)
            it["status_label"] = BATCH_STATUS.get(it["status"], it["status"])
            groups.setdefault(it["batch_no"], []).append(it)
        batch["groups"] = [{"batch_no": k, "items": v,
                            "scheduled": time.strftime(
                                "%Y-%m-%d",
                                time.localtime(batch["created_at"]
                                               + (k - 1) * batch["interval_days"] * 86400))}
                           for k, v in sorted(groups.items())]
        return batch


def _require_batch(bid: int) -> None:
    _ensure()
    with _connect() as conn:
        r = conn.execute("SELECT brand_id FROM fc_batches WHERE id=?", (bid,)).fetchone()
    if not r:
        raise ValueError("批次不存在")
    _require_brand(r["brand_id"])


def update_batch_item(iid: int, status: str = "", note: str = "") -> Dict[str, Any]:
    if status and status not in BATCH_STATUS:
        raise ValueError(f"未知状态: {status}")
    sets, params = [], []
    if status:
        sets.append("status=?")
        params.append(status)
    if note is not None:
        sets.append("note=?")
        params.append(str(note))
    sets.append("updated_at=?")
    params.append(int(time.time()))
    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE fc_batch_items SET {', '.join(sets)} WHERE id=?",
                           (*params, iid))
        if not cur.rowcount:
            raise ValueError("批次条目不存在")
        bid = conn.execute("SELECT batch_id FROM fc_batch_items WHERE id=?",
                           (iid,)).fetchone()["batch_id"]
        # 整单完成时自动关闭批次
        left = conn.execute(
            "SELECT COUNT(*) n FROM fc_batch_items WHERE batch_id=? AND status='pending'",
            (bid,)).fetchone()["n"]
        if left == 0:
            conn.execute("UPDATE fc_batches SET status='done' WHERE id=?", (bid,))
    return get_batch(bid)  # type: ignore[return-value]


def delete_batch(bid: int) -> bool:
    _require_brand(bid)
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM fc_batch_items WHERE batch_id=?", (bid,))
        cur = conn.execute("DELETE FROM fc_batches WHERE id=?", (bid,))
        return cur.rowcount > 0


# ---------------- 接线清单（给主构建者 / app.py） ----------------
WIRING = """
[伪冒反查 fakecheck] app.py 接线（V1.1）：全部挂到 /api/fakecheck 前缀下。
DB：无需改 db.py（表首次调用自动创建）。前端路由：/fakecheck → @/pages/xhs/fakecheck/index.vue，
  name: XhsFakecheck, meta: { title: "伪冒反查", elIcon: "Warning" }，侧边栏菜单加"伪冒反查"。

品牌档案
  GET    /api/fakecheck/brands                        -> fakecheck.list_brands()
  POST   /api/fakecheck/brands                        -> fakecheck.create_brand(body: name, keywords, remark)
  PUT    /api/fakecheck/brands/{bid}                  -> fakecheck.update_brand(bid, body)
  DELETE /api/fakecheck/brands/{bid}                  -> fakecheck.delete_brand(bid)
特征库
  GET    /api/fakecheck/brands/{bid}/features         -> fakecheck.list_features(bid, kind?)
  POST   /api/fakecheck/brands/{bid}/features         -> fakecheck.save_feature(bid, kind, value, weight?)
  PUT    /api/fakecheck/features/{fid}                -> fakecheck.update_feature(fid, body)
  DELETE /api/fakecheck/features/{fid}                -> fakecheck.delete_feature(fid)
嫌疑库
  GET    /api/fakecheck/brands/{bid}/suspects         -> fakecheck.list_suspects(bid, query: level, verified, source, keyword, limit, offset)
  POST   /api/fakecheck/brands/{bid}/suspects         -> fakecheck.create_suspect(bid, body)
  GET    /api/fakecheck/suspects/{sid}                -> fakecheck.get_suspect(sid)
  PUT    /api/fakecheck/suspects/{sid}                -> fakecheck.update_suspect(sid, body)  # verified/credit_code/operator/rescore
  DELETE /api/fakecheck/suspects/{sid}                -> fakecheck.delete_suspect(sid)
  GET    /api/fakecheck/brands/{bid}/stats            -> fakecheck.suspect_stats(bid)
  POST   /api/fakecheck/brands/{bid}/import           -> fakecheck.import_excel(bid, 上传的xlsx临时路径)  # multipart
  GET    /api/fakecheck/brands/{bid}/import-template  -> 返回 fakecheck.import_template() 的 xlsx 文件下载
  GET    /api/fakecheck/brands/{bid}/export.csv       -> 返回 fakecheck.export_csv(bid, level?, verified?) 的 CSV 下载
  GET    /api/fakecheck/suspects/{sid}/material       -> fakecheck.complaint_material(sid)
Sweep 排查
  GET    /api/fakecheck/sweep-login                  -> fakecheck.check_sweep_login() -> {ok, message}
  POST   /api/fakecheck/brands/{bid}/sweep            -> fakecheck.start_sweep(bid, body: keywords?, max_notes?, account_id?, name?)
  GET    /api/fakecheck/brands/{bid}/sweeps           -> fakecheck.list_sweep_tasks(bid)
  GET    /api/fakecheck/sweeps/{tid}                  -> fakecheck.get_sweep_task(tid)
主体聚类 / 企查查 / 团伙证据链
  GET    /api/fakecheck/brands/{bid}/clusters        -> fakecheck.cluster_subjects(bid)
  GET    /api/fakecheck/brands/{bid}/gangs           -> fakecheck.analyze_gangs(bid, query: sim_threshold?, batch_days?)
  GET    /api/fakecheck/brands/{bid}/gangs/report    -> fakecheck.gang_report(bid, query.gang_key)  # 《团伙证据链报告》
  GET    /api/fakecheck/qcc-lookup                  -> fakecheck.qcc_lookup(query.name)
投诉批次
  POST   /api/fakecheck/brands/{bid}/batches          -> fakecheck.create_batch(bid, body: name, suspect_ids, batch_size?, interval_days?)
  GET    /api/fakecheck/brands/{bid}/batches          -> fakecheck.list_batches(bid)
  GET    /api/fakecheck/batches/{bid}                 -> fakecheck.get_batch(bid)
  PUT    /api/fakecheck/batch-items/{iid}             -> fakecheck.update_batch_item(iid, body: status?, note?)
  DELETE /api/fakecheck/batches/{bid}                 -> fakecheck.delete_batch(bid)
维权指引 / 权利人 / 商标证明 / 材料包
  GET    /api/fakecheck/rights-guide                  -> fakecheck.get_rights_guide()
  GET    /api/fakecheck/brands/{bid}/rights-holder    -> fakecheck.get_rights_holder(bid)
  POST   /api/fakecheck/brands/{bid}/rights-holder    -> fakecheck.save_rights_holder(bid, body)
  POST   /api/fakecheck/brands/{bid}/trademark-cert   -> fakecheck.save_trademark_cert(bid, 上传文件临时路径, 文件名)  # multipart
  GET    /api/fakecheck/brands/{bid}/trademark-cert   -> fakecheck.get_trademark_cert(bid)
  DELETE /api/fakecheck/brands/{bid}/trademark-cert   -> fakecheck.delete_trademark_cert(bid)
  GET    /api/fakecheck/batches/{bid}/package         -> fakecheck.export_complaint_package(bid) -> (zip_bytes, filename)  # 文件下载
注意：所有 ValueError → 400，文本即用户可读的中文提示。
"""

# ---------------- 团伙证据链分析（有组织预谋实锤） ----------------
# 口径：证据一/二/三…编号，方便法务 / 品牌维权直接引用。
# 强证据（single = 强）：
#   E1 同一经营者控制多家主体；E2 同一注册地址挂靠多家主体；
#   E3 主体集中批量注册（N 天内）；E4 多账号话术模板化（简介相似度>阈值）。
# 综合判定：命中 ≥2 条强证据 →「疑似有组织团伙」，否则「观察中」。

import re as _re
import unicodedata as _unicodedata


def _normalize_address(addr: str) -> str:
    """地址归一化：全角转半角、去空白、统一标点。"""
    s = _unicodedata.normalize("NFKC", addr or "")
    s = _re.sub(r"\s+", "", s)
    return s.strip()


def _building_key(addr: str) -> str:
    """楼栋级 key：去掉末尾房号/库房编号（如 “…库房1-4171”→“…库房”）。"""
    s = _normalize_address(addr)
    # 去掉末尾“数字/横杠”房号段（不含中文则视为房号）
    s = _re.sub(r"[\d\-－—ー]+$", "", s).rstrip("-－—")
    return s


def _parse_date(s: Any) -> str:
    """成立日期 → YYYY-MM-DD，解析失败返回 ''。"""
    import datetime as _dt
    if isinstance(s, (_dt.datetime, _dt.date)):
        return s.strftime("%Y-%m-%d")
    t = str(s or "").strip()
    m = _re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", t)
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except Exception:
            return ""
    return ""


def _date_diff_days(a: str, b: str) -> Optional[int]:
    import datetime as _dt
    try:
        da = _dt.date.fromisoformat(a)
        db = _dt.date.fromisoformat(b)
        return abs((da - db).days)
    except Exception:
        return None


def _bigrams(text: str) -> Dict[str, int]:
    t = _re.sub(r"\s+", "", text or "")
    d: Dict[str, int] = {}
    for i in range(len(t) - 1):
        bg = t[i:i + 2]
        d[bg] = d.get(bg, 0) + 1
    return d


def _cosine(a: Dict[str, int], b: Dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _template_groups(accounts: List[Dict[str, Any]],
                     threshold: float = 0.8) -> List[List[Dict[str, Any]]]:
    """简介话术模板化聚类（并查集，相似度>阈值成团）。"""
    cands = [a for a in accounts if len((a.get("bio") or "").strip()) >= 6]
    bgs = [(a, _bigrams(a.get("bio") or "")) for a in cands]
    parent = {i: i for i in range(len(bgs))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(len(bgs)):
        for j in range(i + 1, len(bgs)):
            if _cosine(bgs[i][1], bgs[j][1]) > threshold:
                union(i, j)
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for i, (a, _) in enumerate(bgs):
        buckets.setdefault(find(i), []).append(a)
    return [v for v in buckets.values() if len(v) >= 2]


def _entity_map(brand_id: int) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """subject → {credit_code, operator, reg_address, established_date, accounts[]}。"""
    _require_brand(brand_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fc_suspects WHERE brand_id=? AND verified != 2",
            (brand_id,)).fetchall()
    accounts = [_suspect_to_dict(r) for r in rows]
    entities: Dict[str, Dict[str, Any]] = {}
    for a in accounts:
        subj = (a.get("subject") or "").strip() or "(未登记主体)"
        e = entities.setdefault(subj, {
            "subject": subj, "credit_code": "", "operator": "",
            "reg_address": "", "established_date": "", "accounts": []})
        for k in ("credit_code", "operator", "reg_address", "established_date"):
            if not e[k] and a.get(k):
                e[k] = a[k]
        e["accounts"].append(a)
    return entities, accounts


def analyze_gangs(brand_id: int, sim_threshold: float = 0.8,
                  batch_days: int = 7) -> Dict[str, Any]:
    """团伙证据链分析。返回 gangs / template_groups / summary。"""
    _require_brand(brand_id)
    entities, accounts = _entity_map(brand_id)
    ent_list = list(entities.values())
    tmpl_groups = _template_groups(accounts, sim_threshold)
    # 话术簇成员 id 集合（用于团伙内判定 E4）
    tmpl_member_ids = set()
    for g in tmpl_groups:
        for a in g:
            tmpl_member_ids.add(a["id"])

    gangs: List[Dict[str, Any]] = []
    seen_entity_keys = set()

    def _build_gang(kind: str, key: str, name: str,
                    members: List[Dict[str, Any]]) -> Dict[str, Any]:
        """members: 该团伙的主体列表。"""
        accs: List[Dict[str, Any]] = []
        for e in members:
            accs.extend(e["accounts"])
        evidences: List[Dict[str, Any]] = []

        # E1 同一经营者多主体
        ops = {e["operator"] for e in members if e["operator"]}
        if kind == "operator" and len(members) >= 2:
            ev = {"code": "E1", "strength": "strong",
                  "title": "同一经营者控制多家主体",
                  "detail": f"经营者「{name}」名下注册 {len(members)} 家主体"
                            f"（{'、'.join(e['subject'] for e in members)}），"
                            f"共关联 {len(accs)} 个账号"}
            evidences.append(ev)
        # E2 同一地址多主体
        bkeys: Dict[str, List[str]] = {}
        for e in members:
            bk = _building_key(e["reg_address"])
            if bk:
                bkeys.setdefault(bk, []).append(e["subject"])
        for bk, subs in bkeys.items():
            if len(subs) >= 2:
                evidences.append({
                    "code": "E2", "strength": "strong",
                    "title": "同一注册地址挂靠多家主体",
                    "detail": f"地址「{bk}」下注册 {len(subs)} 家主体"
                              f"（{'、'.join(subs)}），疑似集中挂靠"})
                break
        # E3 集中批量注册
        dated = [(e["subject"], _parse_date(e["established_date"])) for e in members]
        dated = [(s, d) for s, d in dated if d]
        if len(dated) >= 2:
            dates = sorted(d for _, d in dated)
            span = _date_diff_days(dates[0], dates[-1])
            if span is not None and span <= batch_days:
                evidences.append({
                    "code": "E3", "strength": "strong",
                    "title": "主体集中批量注册",
                    "detail": f"{len(dated)} 家主体在 {span} 天内集中注册"
                              f"（{dates[0]}～{dates[-1]}），"
                              "系典型的有组织批量起号前兆"})
        # E4 话术模板化（团伙内账号命中话术簇）
        gang_acc_ids = {a["id"] for a in accs}
        hit = sum(1 for i in gang_acc_ids if i in tmpl_member_ids)
        if hit >= 2:
            evidences.append({
                "code": "E4", "strength": "strong",
                "title": "多账号话术模板化",
                "detail": f"团伙内 {hit} 个账号简介文本相似度超过 {sim_threshold}，"
                          "判定为同一话术模板批量运营"})
        for i, ev in enumerate(evidences, 1):
            ev["no"] = f"证据{['一', '二', '三', '四', '五', '六'][i-1] if i <= 6 else i}"
        strong_n = sum(1 for e in evidences if e["strength"] == "strong")
        verdict = "suspected_organized" if strong_n >= 2 else "watch"
        # 时间线
        timeline: List[Dict[str, str]] = []
        for e in members:
            d = _parse_date(e["established_date"])
            if d:
                timeline.append({"date": d, "event": f"主体「{e['subject']}」注册成立"})
        for a in accs:
            ts = a.get("created_at") or 0
            if ts:
                timeline.append({
                    "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
                    "event": f"账号「{a.get('nickname') or a.get('xhs_id')}」纳入排查"})
        timeline.sort(key=lambda x: x["date"])
        # 关联图谱
        nodes, edges = [], []
        nid = lambda p, v: f"{p}:{v}"
        if kind == "operator":
            nodes.append({"id": nid("op", name), "name": name,
                          "category": "经营者", "symbolSize": 46})
        for e in members:
            eid = nid("ent", e["subject"])
            nodes.append({"id": eid, "name": e["subject"], "category": "认证主体",
                          "symbolSize": 34})
            if kind == "operator":
                edges.append({"source": nid("op", name), "target": eid,
                              "label": "经营"})
            bk = _building_key(e["reg_address"])
            if bk:
                aid = nid("addr", bk)
                if not any(n["id"] == aid for n in nodes):
                    nodes.append({"id": aid, "name": bk, "category": "注册地址",
                                  "symbolSize": 28})
                edges.append({"source": eid, "target": aid, "label": "注册地"})
            for a in e["accounts"]:
                acid = nid("acc", str(a["id"]))
                nodes.append({"id": acid,
                              "name": a.get("nickname") or a.get("xhs_id") or "?",
                              "category": "账号", "symbolSize": 22,
                              "level": a.get("level")})
                edges.append({"source": eid, "target": acid, "label": "认证"})
        return {
            "key": f"{kind}:{name}",
            "kind": kind,
            "kind_label": "同一经营者" if kind == "operator" else "同一注册地址",
            "name": name,
            "verdict": verdict,
            "verdict_label": "疑似有组织团伙" if verdict == "suspected_organized" else "观察中",
            "evidence_count": strong_n,
            "evidences": evidences,
            "timeline": timeline,
            "entities": [{
                "subject": e["subject"], "credit_code": e["credit_code"],
                "operator": e["operator"], "reg_address": e["reg_address"],
                "established_date": _parse_date(e["established_date"]),
                "account_count": len(e["accounts"]),
                "accounts": [{"id": a["id"], "nickname": a.get("nickname"),
                              "xhs_id": a.get("xhs_id"), "level": a.get("level"),
                              "score": a.get("score")} for a in e["accounts"]],
            } for e in members],
            "account_count": len(accs),
            "graph": {"nodes": nodes, "edges": edges,
                      "categories": ["经营者", "认证主体", "账号", "注册地址"]},
        }

    # 按经营者组团伙
    op_map: Dict[str, List[Dict[str, Any]]] = {}
    for e in ent_list:
        if e["operator"]:
            op_map.setdefault(e["operator"], []).append(e)
    for op, members in op_map.items():
        if len(members) >= 2 or sum(len(e["accounts"]) for e in members) >= 2:
            gangs.append(_build_gang("operator", op, op, members))
            for e in members:
                seen_entity_keys.add(e["subject"])
    # 按地址组团伙（未被经营者团伙覆盖的主体）
    addr_map: Dict[str, List[Dict[str, Any]]] = {}
    for e in ent_list:
        if e["subject"] in seen_entity_keys:
            continue
        bk = _building_key(e["reg_address"])
        if bk:
            addr_map.setdefault(bk, []).append(e)
    for bk, members in addr_map.items():
        if len(members) >= 2:
            gangs.append(_build_gang("address", bk, bk, members))
    gangs.sort(key=lambda g: (0 if g["verdict"] == "suspected_organized" else 1,
                              -g["evidence_count"], -g["account_count"]))
    org_n = sum(1 for g in gangs if g["verdict"] == "suspected_organized")
    return {
        "gangs": gangs,
        "gang_count": len(gangs),
        "organized_count": org_n,
        "template_groups": [{
            "count": len(g),
            "members": [{"id": a["id"], "nickname": a.get("nickname"),
                         "xhs_id": a.get("xhs_id"), "bio": (a.get("bio") or "")[:60]}
                        for a in g],
        } for g in tmpl_groups],
        "entity_count": len(ent_list),
        "account_count": len(accounts),
    }


def gang_report(brand_id: int, gang_key: str) -> Dict[str, Any]:
    """《团伙证据链报告》：可直接打印 / 导出 PDF，作为投诉材料附件。"""
    _require_brand(brand_id)
    brand = _require_brand(brand_id)
    data = analyze_gangs(brand_id)
    gang = next((g for g in data["gangs"] if g["key"] == gang_key), None)
    if not gang:
        raise ValueError("团伙不存在")
    return {
        "title": "团伙证据链报告",
        "brand": brand["name"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "gang_name": gang["name"],
        "verdict_label": gang["verdict_label"],
        "evidence_count": gang["evidence_count"],
        "evidences": gang["evidences"],
        "timeline": gang["timeline"],
        "entities": gang["entities"],
        "account_count": gang["account_count"],
        "graph": gang["graph"],
        "footer": ("本报告由系统根据账号特征、工商关联信息自动生成，"
                   "供品牌维权 / 法务取证参考；正式投诉请以平台核验为准。"),
    }

# ---------------- 维权指引（已核实口径） ----------------

RIGHTS_GUIDE = [
    {"step": "第一步",
     "title": "单账号举报（App 内）",
     "body": "小红书 App → 侵权账号主页 → 右上角“…” → 举报 → 选择“假冒他人/仿冒”类理由 → 提交。"},
    {"step": "第二步",
     "title": "权利人批量投诉（官方通道）",
     "body": "以品牌方身份（商标注册证 + 营业执照）通过小红书官方侵权投诉入口提交。"
              "入口：App「设置 → 帮助与客服 → 知识产权保护」或 PC 帮助中心。"
              "材料清单：权利证明（商标注册证/营业执照）、批量侵权账号清单（含主页链接）、"
              "侵权对比说明（本模块《团伙证据链报告》可直接作为附件）、联系人信息。"},
    {"step": "第三步",
     "title": "批量走官方/法律通道最有效",
     "body": "品牌方面对平台一次性提交整批清单。参考先例：追觅曾对小红书站内百余个侵权账号"
              "批量发起诉讼，并要求平台披露账号实名信息——批量走法律/官方通道是走得通的。"},
    {"step": "第四步",
     "title": "升级路径",
     "body": "平台处理无果 → 12315 / 市场监管举报 → 法院起诉"
              "（起诉可要求平台披露侵权账号实名信息）。"},
]


def get_rights_guide() -> List[Dict[str, str]]:
    """维权指引（已核实口径，前端“维权指引”页直接渲染）。"""
    return RIGHTS_GUIDE


# ---------------- 权利人信息 + 商标证明 ----------------

def get_rights_holder(brand_id: int) -> Dict[str, Any]:
    _require_brand(brand_id)
    _require_brand(brand_id)
    _ensure()
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fc_rights_holder WHERE brand_id=?",
                         (brand_id,)).fetchone()
    if not r:
        return {"brand_id": brand_id, "company": "", "trademark_no": "",
                "contact": "", "phone": "", "email": ""}
    return dict(r)


def save_rights_holder(brand_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    _require_brand(brand_id)
    _require_brand(brand_id)
    _ensure()
    vals = {k: str(data.get(k) or "").strip()
            for k in ("company", "trademark_no", "contact", "phone", "email")}
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO fc_rights_holder (brand_id, company, trademark_no, contact,"
            " phone, email, updated_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(brand_id) DO UPDATE SET company=excluded.company,"
            " trademark_no=excluded.trademark_no, contact=excluded.contact,"
            " phone=excluded.phone, email=excluded.email, updated_at=excluded.updated_at",
            (brand_id, vals["company"], vals["trademark_no"], vals["contact"],
             vals["phone"], vals["email"], int(time.time())))
    return get_rights_holder(brand_id)


def _cert_dir() -> Path:
    from .config import DATA_DIR
    d = DATA_DIR / "fakecheck" / "certs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_trademark_cert(brand_id: int, src_path: str, filename: str) -> Dict[str, Any]:
    """保存商标证明文件（PDF/图片，仅存本地）。"""
    _require_brand(brand_id)
    _require_brand(brand_id)
    safe = "".join(c for c in (filename or "") if c not in "/\\").strip() or "cert"
    ext = Path(safe).suffix.lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
        raise ValueError("仅支持 PDF / PNG / JPG 格式")
    dest = _cert_dir() / f"brand{brand_id}_{int(time.time())}{ext}"
    import shutil
    shutil.copyfile(src_path, dest)
    return {"brand_id": brand_id, "filename": safe,
            "stored": dest.name, "size": dest.stat().st_size}


def get_trademark_cert(brand_id: int) -> Optional[Dict[str, Any]]:
    _require_brand(brand_id)
    _require_brand(brand_id)
    cands = sorted(_cert_dir().glob(f"brand{brand_id}_*"))
    if not cands:
        return None
    p = cands[-1]
    return {"brand_id": brand_id, "stored": p.name, "size": p.stat().st_size,
            "uploaded_at": int(p.stat().st_mtime)}


def delete_trademark_cert(brand_id: int) -> bool:
    _require_brand(brand_id)
    _require_brand(brand_id)
    ok = False
    for p in _cert_dir().glob(f"brand{brand_id}_*"):
        try:
            p.unlink()
            ok = True
        except Exception:
            pass
    return ok


# ---------------- 维权材料包（一键打包下载） ----------------

def _gang_report_html(rep: Dict[str, Any]) -> str:
    ev_html = "".join(
        f"<h3>{e['no']}：{e['title']}</h3><p>{e['detail']}</p>"
        for e in rep.get("evidences", []))
    tl_html = "".join(
        f"<li>{t['date']}：{t['event']}</li>" for t in rep.get("timeline", []))
    ent_rows = "".join(
        f"<tr><td>{e['subject']}</td><td>{e.get('credit_code') or '-'}</td>"
        f"<td>{e.get('operator') or '-'}</td>"
        f"<td>{e.get('established_date') or '-'}</td>"
        f"<td>{e['account_count']}</td></tr>"
        for e in rep.get("entities", []))
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{rep['title']}-{rep['gang_name']}</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:24px auto;padding:0 16px;}}
h1{{font-size:22px;}}h3{{color:#b02a2a;}}table{{border-collapse:collapse;width:100%;}}
td,th{{border:1px solid #ccc;padding:6px 8px;font-size:13px;text-align:left;}}
.meta{{color:#666;font-size:13px;}}</style></head><body>
<h1>{rep['title']}</h1>
<p class="meta">品牌：{rep['brand']}｜团伙：{rep['gang_name']}｜结论：{rep['verdict_label']}｜生成时间：{rep['generated_at']}</p>
<h2>证据链</h2>{ev_html}
<h2>时间线</h2><ul>{tl_html}</ul>
<h2>主体清单</h2><table><tr><th>认证主体</th><th>信用代码</th><th>经营者</th><th>成立日期</th><th>关联账号</th></tr>{ent_rows}</table>
<p class="meta">{rep['footer']}</p></body></html>"""


def export_complaint_package(batch_id: int) -> Tuple[bytes, str]:
    """维权材料包 ZIP：维权指引 + 权利人信息 + 商标证明 + 账号清单 + 团伙证据链报告。"""
    import zipfile
    with _connect() as conn:
        b = conn.execute("SELECT * FROM fc_batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise ValueError("投诉批次不存在")
    batch = dict(b)
    brand_id = batch["brand_id"]
    brand = _require_brand(brand_id)
    holder = get_rights_holder(brand_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. 维权指引
        guide_txt = "\n\n".join(f"{g['step']}：{g['title']}\n{g['body']}"
                                for g in RIGHTS_GUIDE)
        z.writestr("00-维权指引.txt", guide_txt.encode("utf-8"))
        # 2. 权利人信息
        z.writestr("01-权利人信息.json", json.dumps(
            {"brand": brand["name"], **holder}, ensure_ascii=False, indent=2).encode("utf-8"))
        # 3. 商标证明
        cert = get_trademark_cert(brand_id)
        if cert:
            p = _cert_dir() / cert["stored"]
            if p.exists():
                z.write(p, f"02-商标证明{Path(cert['stored']).suffix}")
        else:
            z.writestr("02-商标证明-待上传.txt",
                       "请将商标注册证/营业执照扫描件通过模块内“商标证明”上传后重新打包。".encode("utf-8"))
        # 4. 账号清单
        full = get_batch(batch_id) or {}
        items = [it for g in full.get("groups", []) for it in g["items"]]
        sbuf = io.StringIO()
        w = csv.writer(sbuf)
        w.writerow(["批次", "昵称", "账号ID", "主页链接", "认证主体", "嫌疑等级", "状态", "备注"])
        for it in items:
            w.writerow([it["batch_no"], it.get("nickname"), it.get("xhs_id"),
                        it.get("profile_url"), it.get("subject"),
                        it.get("level"), BATCH_STATUS.get(it["status"]),
                        it.get("note")])
        z.writestr("03-账号清单.csv", "\ufeff".encode("utf-8") + sbuf.getvalue().encode("utf-8"))
        # 5. 团伙证据链报告（疑似有组织团伙）
        gangs = analyze_gangs(brand_id)["gangs"]
        org = [g for g in gangs if g["verdict"] == "suspected_organized"] or gangs[:1]
        if org:
            for g in org:
                rep = gang_report(brand_id, g["key"])
                z.writestr(f"04-团伙证据链报告-{g['name']}.html",
                           _gang_report_html(rep).encode("utf-8"))
        else:
            z.writestr("04-团伙证据链报告-暂无.txt",
                       "当前未发现团伙线索，暂无证据链报告。".encode("utf-8"))
    fname = f"维权材料包-{brand['name']}-{batch['name']}-{time.strftime('%Y%m%d')}.zip"
    return buf.getvalue(), fname
