"""FxTwitter 后端——单推与用户资料，**零认证**。

参考项目 x-tweet-fetcher 的哲学：把签名甩给别人。FxTwitter 是给 Discord 做
嵌入卡片的公开服务，它自己处理所有认证，我们只消费 JSON。

能力边界很清楚：**只能按 id 取单推、按 handle 取资料，没有搜索也没有时间线**。
所以它在链里的位置是「拿单条内容」，时间线交给 Nitter，搜索交给 GraphQL。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ...contracts import (
    Item,
    Media,
    MediaType,
    NotFound,
    RateLimited,
    Source,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from ...settings import DEFAULT_UA
from .config import FXTWITTER_BASE

log = logging.getLogger("sourcepilot.channels.x")


def _score(tweet: dict[str, Any]) -> float:
    """互动量归一化。契约 §2 说 X 的 score 由互动量算，不是排名。

    没有全局基准可归一化，所以用一个饱和函数：一万次互动约到 0.9，
    再多也不会突破 1.0。它表达的是「热度量级」，同源内可比。
    """
    stats = (
        int(tweet.get("likes") or 0)
        + int(tweet.get("retweets") or 0) * 2
        + int(tweet.get("replies") or 0)
    )
    return round(min(stats / (stats + 1000.0), 1.0), 4) if stats else 0.0


def _media(tweet: dict[str, Any]) -> list[Media]:
    out: list[Media] = []
    for kind, key in (("photos", MediaType.IMAGE), ("videos", MediaType.VIDEO)):
        for entry in (tweet.get("media") or {}).get(kind) or []:
            url = entry.get("url")
            if url:
                out.append(Media(type=key, url=url))
    return out


def tweet_to_item(tweet: dict[str, Any], now: datetime | None = None) -> Item | None:
    tweet_id = tweet.get("id")
    author = (tweet.get("author") or {}).get("screen_name")
    if not tweet_id or not author:
        return None

    text = (tweet.get("text") or "").strip()
    if not text:
        return None

    stamp = tweet.get("created_timestamp")
    published = datetime.fromtimestamp(int(stamp), tz=UTC) if stamp else None

    return Item(
        id=f"x:{tweet_id}",
        source=Source(type=SourceType.X, name="X / Twitter", platform="x"),
        # 推文没有标题，契约约定取正文前 80 字符当标题。
        title=text[:80],
        summary=text if len(text) > 80 else None,
        url=tweet.get("url") or f"https://x.com/{author}/status/{tweet_id}",
        author=author,
        published_at=published,
        discovered_at=now or datetime.now(UTC),
        time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
        score=_score(tweet),
        categories=[],
        lang=tweet.get("lang"),
        media=_media(tweet),
        raw={
            "backend": "fxtwitter",
            "likes": tweet.get("likes"),
            "retweets": tweet.get("retweets"),
            "replies": tweet.get("replies"),
            "views": tweet.get("views"),
        },
    )


class FxTwitterBackend:
    name = "fxtwitter"
    #: 只能取单条，撑不起搜索和时间线——链里由别的后端补。
    supports = frozenset({"tweet", "profile"})

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def available(self) -> bool:
        return True  # 零认证

    def _get(self, path: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(
                    f"{FXTWITTER_BASE}{path}",
                    headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"},
                )
        except httpx.TimeoutException as exc:
            raise UpstreamDown("FxTwitter 超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamDown(f"FxTwitter 连接失败：{type(exc).__name__}") from exc

        if response.status_code == 404:
            raise NotFound("FxTwitter 上找不到这条内容")
        if response.status_code == 429:
            raise RateLimited("FxTwitter 限流")
        # 它对不存在的路径会回 HTML 而不是 JSON，所以按内容类型判断。
        if "application/json" not in response.headers.get("content-type", ""):
            raise NotFound("FxTwitter 返回的不是 JSON——多半是这个地址它认不出来")
        return response.json()

    def fetch_tweet(self, handle: str, tweet_id: str) -> Item | None:
        payload = self._get(f"/{handle}/status/{tweet_id}")
        tweet = payload.get("tweet")
        return tweet_to_item(tweet) if tweet else None

    def fetch_profile(self, handle: str) -> dict[str, Any] | None:
        payload = self._get(f"/{handle}")
        return payload.get("user")
