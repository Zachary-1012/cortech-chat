# -*- coding: utf-8 -*-
"""灌入演示数据（标题带前缀），让仪表盘/图表/简报开箱即可查看。

清空方法：删掉 data/monitor.db 后重启网站即可。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import db
from monitor.adapters.base import AuthorData, CommentData, NoteData
from monitor.ingester import store_unified

NOW = int(time.time())
DAY = 86400

DEMOS = [
# (topic_idx, post_id, title, desc, author, fans, days_ago, likes, collects, comments, shares, sentiment_hint)
(0, "demo15_01", "太古汇十五周年排队两小时，避雷",
"现场人太多，组织混乱，排队两小时才进场，太失望了", "羊城探店", 1200, 1, 320, 40, 88, 12),
(0, "demo15_02", "十五周年抽中Ole购物卡，真香",
"运气爆棚，500块购物卡到手，工作人员态度也很好", "广州吃货", 5600, 0, 210, 25, 40, 8),
(0, "demo15_03", "十五周年限定展打卡",
"中庭的装置很好看，随手拍都很出片", "拍照达人", 8900, 2, 150, 60, 20, 5),
(0, "demo15_04", "吐槽十五周年奖品缩水",
"宣传说的大奖根本抢不到，文字游戏，劝退", "较真的人", 300, 1, 180, 20, 65, 9),
(0, "demo15_05", "十五周年音乐会体验",
"阵容不错，音响效果也可以，就是座位有点少", "周末去哪儿", 2100, 3, 90, 10, 15, 3),
(1, "demo_all_01", "太古汇Ole超市物价偏高",
"进口食品很全，但价格比山姆贵不少，偶尔逛逛还行", "精打细算", 1500, 2, 140, 30, 42, 6),
(1, "demo_all_02", "太古汇停车太难了",
"周末停车场爆满，绕了三圈才找到位置，停车费还贵", "开车一族", 800, 1, 260, 35, 70, 11),
(1, "demo_all_03", "太古汇新开的餐厅测评",
"环境一流，出品稳定，就是等位要一小时", "美食侦探", 12000, 0, 190, 45, 33, 7),
(1, "demo_all_04", "在太古汇被服务惊艳到",
"买东西遇到问题，客服耐心解决，点赞", "满意顾客", 200, 4, 75, 8, 12, 2),
(1, "demo_all_05", "避雷太古汇某奶茶店",
"排长队，味道一般，服务员爱答不理", "奶茶品鉴师", 3500, 1, 170, 22, 48, 10),
]

COMMENTS = [
("demo15_01", "同排队，气死了", "路人甲", 45),
("demo15_01", "组织确实混乱", "路人乙", 20),
("demo15_04", "我也觉得是文字游戏", "路人丙", 30),
("demo_all_02", "停车费真的贵", "车主丁", 25),
("demo_all_05", "这家我也踩雷过", "路人戊", 18),
]


def main() -> None:
    db.init_db()
    db.seed_topics()
    topics = db.list_topics()
    notes, comments = [], []
    for i, (ti, nid, title, desc, author, fans, ago, lk, cl, cm, sh) in enumerate(DEMOS):
        notes.append(NoteData(
            post_id=nid, title=title, desc=desc,
            author=AuthorData(author_id=f"demo_u{i}", nickname=author,
                              fans_count=fans, ip_location="广东"),
            publish_time=NOW - ago * DAY - i * 3600, ip_location="广东",
            tags=["广州太古汇", "演示"], liked_count=lk, collected_count=cl,
            comment_count=cm, share_count=sh, media_type="image",
            note_url=f"https://www.xiaohongshu.com/explore/{nid}",
            source_keyword="演示数据"))
    for j, (nid, content, author, likes) in enumerate(COMMENTS):
        comments.append(CommentData(
            comment_id=f"demo_c{j}", post_id=nid, content=content,
            author=AuthorData(nickname=author), create_time=NOW - 3600,
            like_count=likes, reply_count=0))
    # 按话题分别入库
    n0 = [n for n, d in zip(notes, DEMOS) if d[0] == 0]
    n1 = [n for n, d in zip(notes, DEMOS) if d[0] == 1]
    a, b = store_unified(topics[0]["id"], n0, [c for c in comments if c.post_id.startswith("demo15")])
    c_, d_ = store_unified(topics[1]["id"], n1, [c for c in comments if c.post_id.startswith("demo_all")])
    print(f"演示数据入库：话题1 +{a}帖，话题2 +{c_}帖，评论 +{b + d_}条")


if __name__ == "__main__":
    main()
