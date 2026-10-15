# -*- coding: utf-8 -*-
"""内置种子数据：DFS 48 个已核查嫌疑账号入库。

幂等：按 (brand_id, uid/nickname) 去重，已存在则跳过，重复运行安全。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from . import fakecheck

DATA_FILE = Path(__file__).resolve().parent / "seed_data" / "dfs_suspects.json"

_LEVEL_MAP = {"高": "high", "中": "mid", "低": "low"}
_SCORE_MAP = {"高": 75, "中": 45, "低": 20}


def _dfs_brand_id() -> int:
    brands = fakecheck.list_brands()  # 内部自动 _ensure_seed，无品牌时导入 DFS 预设
    for b in brands:
        if b["name"] == fakecheck.SEED_BRAND_NAME:
            return int(b["id"])
    raise RuntimeError("DFS 品牌档案不存在")


def seed_dfs_suspects() -> Dict[str, int]:
    """执行入库，返回 {brand_id, total, inserted, skipped}。"""
    brand_id = _dfs_brand_id()
    suspects = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    inserted = skipped = 0
    for s in suspects:
        level = _LEVEL_MAP.get(s["level"], "low")
        notes = "粉丝数：%s；符合标准：%s" % (s["fans_count"], s["stds"])
        try:
            fakecheck.create_suspect(brand_id, {
                "nickname": s["nickname"],
                "uid": s["uid"],
                "bio": s["bio"],
                "ip_location": s["ip_location"],
                "verified": s["verified"],
                "profile_url": s["profile_url"],
                "notes": notes,
                "score": _SCORE_MAP.get(s["level"], 20),
                "level": level,
                "source": "seed_import",
            }, auto_score=False)
            inserted += 1
        except ValueError as e:
            if "已在嫌疑库中" in str(e):
                skipped += 1
            else:
                raise
    return {"brand_id": brand_id, "total": len(suspects),
            "inserted": inserted, "skipped": skipped}


def main() -> None:
    r = seed_dfs_suspects()
    print("[seed_dfs_suspects] brand_id=%d total=%d inserted=%d skipped=%d"
          % (r["brand_id"], r["total"], r["inserted"], r["skipped"]))


if __name__ == "__main__":
    main()
