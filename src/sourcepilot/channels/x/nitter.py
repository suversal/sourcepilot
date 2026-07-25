"""Nitter 后端——时间线，**零认证**，多实例故障转移。

Nitter 自己持一份副账号 cookie 去调 X 内部 API，对外吐 RSS/HTML，我们只解析结果。
这是参考项目 x-tweet-fetcher 的做法：把认证甩给别人。

**公共实例寿命很短**。参考文档记着「2026.03 起基本全挂」，实测 2026-07-26：

    nitter.net             时间线 19 条 ✅   搜索 0 条 ❌
    xcancel.com            需 RSS 客户端白名单
    nitter.tiekoetter.com  时间线 0 条
    lightbrd / nitter.space  403

所以这里做**多实例健康检查 + 顺次故障转移**，并且明确：**搜索指望不上 Nitter**
（搜索是最先被各实例关掉的功能，它最费上游配额）。搜索走 GraphQL。
自建实例配在源配置里，会排在公共实例前面。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import feedparser
import httpx
from bs4 import BeautifulSoup

from ...contracts import (
    Item,
    RateLimited,
    Source,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from .config import NITTER_INSTANCES

log = logging.getLogger("sourcepilot.channels.x")

#: Nitter 对普通浏览器 UA 有时会挡，对 RSS 客户端反而放行。
RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SourcePilot RSS Reader)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}


def _entry_to_item(entry, handle: str, now: datetime) -> Item | None:
    link = entry.get("link") or ""
    # Nitter 的链接指向它自己的域，换回 x.com——契约要求 url 是第三方原文。
    tweet_id = link.rstrip("#m").rsplit("/", 1)[-1] if link else ""
    if not tweet_id.isdigit():
        return None

    text = BeautifulSoup(entry.get("title") or "", "html.parser").get_text(" ", strip=True)
    if not text:
        return None

    published = None
    parsed = entry.get("published_parsed")
    if parsed:
        published = datetime(*parsed[:6], tzinfo=UTC)

    author = (entry.get("author") or handle).lstrip("@")

    return Item(
        id=f"x:{tweet_id}",
        source=Source(type=SourceType.X, name="X / Twitter", platform="x"),
        title=text[:80],
        summary=text if len(text) > 80 else None,
        url=f"https://x.com/{author}/status/{tweet_id}",
        author=author,
        published_at=published,
        discovered_at=now,
        time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
        # Nitter 的 RSS 不带互动数，没有热度信号就老实给 0（契约 §2）。
        score=0.0,
        categories=[],
        lang=None,
        media=[],
        raw={"backend": "nitter"},
    )


class NitterBackend:
    name = "nitter"
    supports = frozenset({"timeline"})

    def __init__(self, instances: list[str] | None = None, timeout: float = 12.0) -> None:
        self.instances = list(instances or NITTER_INSTANCES)
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.instances)

    def _fetch_rss(self, path: str) -> tuple[str, str]:
        """顺次试各实例，返回 (实例, RSS 文本)。

        逐个降级而不是只认一个：公共实例随时会挂，卡在第一个上等于整条路断了。
        """
        last_error: Exception | None = None
        for instance in self.instances:
            url = f"{instance.rstrip('/')}{path}"
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get(url, headers=RSS_HEADERS)
            except httpx.HTTPError as exc:
                last_error = exc
                log.info("Nitter 实例 %s 不可达：%s", instance, type(exc).__name__)
                continue

            if response.status_code == 429:
                last_error = RateLimited(f"{instance} 限流")
                continue
            if response.status_code != 200:
                last_error = UpstreamDown(f"{instance} 返回 {response.status_code}")
                continue
            if "not yet whitelisted" in response.text:
                # xcancel 这类要求 RSS 阅读器先备案，对我们等同不可用。
                last_error = UpstreamDown(f"{instance} 要求 RSS 客户端白名单")
                continue
            return instance, response.text

        raise UpstreamDown(f"所有 Nitter 实例都不可用（最后一个错误：{last_error}）")

    def fetch_timeline(self, handle: str, limit: int) -> list[Item]:
        handle = handle.lstrip("@")
        instance, text = self._fetch_rss(f"/{handle}/rss")
        feed = feedparser.parse(text)
        if not feed.entries:
            raise UpstreamDown(f"{instance} 返回了空时间线——该实例多半已失效")

        now = datetime.now(UTC)
        items = []
        for entry in feed.entries[:limit]:
            item = _entry_to_item(entry, handle, now)
            if item is not None:
                items.append(item)
        log.info("Nitter 由 %s 提供 %d 条 @%s 的推文", instance, len(items), handle)
        return items
