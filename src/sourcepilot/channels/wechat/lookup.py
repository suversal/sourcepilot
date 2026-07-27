"""按名字查公众号的 fakeid，顺带报告它最近还更不更新。

    python -m sourcepilot.channels.wechat.lookup 智谱        # 按昵称
    python -m sourcepilot.channels.wechat.lookup zhipu_ai   # 按微信号（更准）

**为什么要有这个工具**：公众平台上同名号、山寨号、停更旧号极常见。实测搜
「智谱AI」命中的是个 2022 年就停更的号，而智谱现在发内容的是「智谱清言」；
搜「Kimi」命中的是 2018 年一个讲电影票的无关号。所以它不只给 fakeid，
还把每个候选的最新文章日期一并打出来——**挑号要看活跃度，不能看名字像不像**。

拿到结果后写进 config/sources/wechat.yaml 的 accounts。
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

from .mp import SEARCH_BIZ, Credentials, WechatClient

#: 候选之间的间隔。公众平台对连续请求敏感，查一次多等几秒比被限流划算。
INTERVAL = 4.0


def lookup(keyword: str, limit: int = 5) -> int:
    credentials = Credentials.load()
    if credentials is None:
        print("没有凭据。先按 config/sources/wechat.yaml 文件头的说明配置。")
        return 1

    client = WechatClient(credentials)
    payload = client._get(
        SEARCH_BIZ, {"action": "search_biz", "begin": 0, "count": limit, "query": keyword}
    )
    candidates = payload.get("list") or []
    if not candidates:
        print(f"搜「{keyword}」没有结果")
        return 1

    now = datetime.now(UTC)
    print(f"搜「{keyword}」得到 {len(candidates)} 个候选：\n")
    for item in candidates:
        time.sleep(INTERVAL)
        try:
            articles = client.list_articles(item["fakeid"], 1)
        except Exception as exc:
            recency = f"拉不到列表（{type(exc).__name__}）"
        else:
            if articles:
                published = datetime.fromtimestamp(articles[0]["update_time"], UTC)
                days = (now - published).days
                recency = f"{published:%Y-%m-%d}（{days} 天前）"
            else:
                recency = "没有可见文章"
        exact = " ★ 微信号精确匹配" if item.get("alias") == keyword else ""
        print(f"  {item['nickname']}{exact}")
        print(f"    fakeid: {item['fakeid']}")
        print(f"    微信号: {item.get('alias') or '（无）'}")
        print(f"    最近更新: {recency}\n")
    print("挑更新最近的那个写进 accounts，别只看名字对不对。")
    print("用微信号搜最准——它全平台唯一且不可改，昵称既会改也会重名。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(lookup(" ".join(sys.argv[1:])))
