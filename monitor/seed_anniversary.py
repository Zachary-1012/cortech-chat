# -*- coding: utf-8 -*-
"""内置种子数据：广州太古汇十五周年 85 条真实采集帖子入库。

幂等：post_id 唯一（INSERT OR IGNORE），重复运行不会重复插入。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict

from . import db

TOPIC_NAME = "广州太古汇十五周年"
TOPIC_KEYWORDS = "广州太古汇十五周年,太古汇十五周年"

DATA_FILE = Path(__file__).resolve().parent / "seed_data" / "anniversary_posts.json"


def _ensure_topic() -> int:
    db.init_db()
    with db._lock, db._connect() as conn:
        row = conn.execute("SELECT id FROM topics WHERE name=?", (TOPIC_NAME,)).fetchone()
        if row:
            return int(row["id"])
    return db.create_topic(TOPIC_NAME, TOPIC_KEYWORDS, max_notes=100,
                          enable_comments=1, enabled=0)


def seed_anniversary() -> Dict[str, int]:
    """执行入库，返回 {topic_id, total, inserted, skipped}。"""
    topic_id = _ensure_topic()
    posts = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    inserted = skipped = 0
    for p in posts:
        ok = db.upsert_note({
            "post_id": p["post_id"],
            "topic_id": topic_id,
            "title": p["title"],
            "desc": p["remark"],
            "author": p["author"],
            "publish_time": p["publish_time"],
            "publish_time_raw": p["publish_time_raw"],
            "liked_count": p["liked_count"],
            "collected_count": p["collected_count"],
            "comment_count": p["comment_count"],
            "note_url": p["note_url"],
            "source_keyword": "广州太古汇十五周年",
            "sentiment": p["sentiment"],
            "sentiment_score": 0.2 if p["sentiment"] == "negative" else 0.5,
            "collected_at": int(time.time()),
        })
        if ok:
            inserted += 1
        else:
            skipped += 1
    return {"topic_id": topic_id, "total": len(posts),
            "inserted": inserted, "skipped": skipped}


def main() -> None:
    r = seed_anniversary()
    print("[seed_anniversary] topic_id=%d total=%d inserted=%d skipped=%d"
          % (r["topic_id"], r["total"], r["inserted"], r["skipped"]))


if __name__ == "__main__":
    main()
