"""X 推文的完整记录。

**为什么不塞进 Item**：Item 是跨源统一 schema，字段是所有信源的最小公共集。
推文特有的东西——互动数、引用链、线程、作者粉丝数、展开后的外链——在别的源
里没有对应概念，硬塞只能进 `raw`，而契约明确写着 `raw` 结构不稳定、消费方
不得依赖。下游要拿它做展示就需要一个稳定的形状，所以单独一张表。

两者是**同一条推文的两个视图**，不是主从关系：Item 进信息流参与跨源检索，
TweetRecord 供需要推文原貌的消费方（AIRADAR 的推文卡片）使用。

外链在这里被展开成原始地址。X 把正文里的链接一律替换成 `t.co` 短链，但
`entities.urls[].expanded_url` 已经给了真实地址——**不需要额外发请求去解析**，
那既慢又会在对方的统计里留下一次点击。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: `source` 字段是一段 HTML 锚点（`<a href="…">Twitter Web App</a>`），
#: 要的只是里面的文字。
_SOURCE_TEXT = re.compile(r">([^<]+)<")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class TweetRecord:
    """一条推文的全部可用信息。字段取不到就是 None，不编造。"""

    tweet_id: str
    author_handle: str
    text: str
    fetched_at: datetime

    conversation_id: str | None = None
    author_name: str | None = None
    author_id: str | None = None
    author_avatar: str | None = None
    author_verified: bool = False
    author_followers: int | None = None

    lang: str | None = None
    created_at: datetime | None = None

    likes: int | None = None
    retweets: int | None = None
    replies: int | None = None
    quotes: int | None = None
    bookmarks: int | None = None
    views: int | None = None

    is_reply: bool = False
    reply_to_handle: str | None = None
    reply_to_tweet_id: str | None = None
    is_quote: bool = False
    quoted_tweet_id: str | None = None
    quoted_handle: str | None = None
    quoted_text: str | None = None

    #: 展开后的外链：[{url, expanded_url, display_url}]。正文里是 t.co 短链，
    #: 真实地址在这里——下游要抓原文时用 expanded_url，别去解析短链。
    urls: list[dict[str, str]] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    #: [{type, url, thumbnail}]，type 为 photo / video / animated_gif
    media: list[dict[str, str]] = field(default_factory=list)

    possibly_sensitive: bool = False
    source_client: str | None = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author_handle}/status/{self.tweet_id}"

    @property
    def external_urls(self) -> list[str]:
        """正文引用的站外地址，已展开。转推卡片、原文抓取都靠它。"""
        return [
            u["expanded_url"]
            for u in self.urls
            if u.get("expanded_url") and "//x.com/" not in u["expanded_url"]
            and "//twitter.com/" not in u["expanded_url"]
        ]


def _media_entries(legacy: dict[str, Any]) -> list[dict[str, str]]:
    """媒体优先取 extended_entities——多图推文在 entities 里只出现第一张。"""
    entries = (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []
    out: list[dict[str, str]] = []
    for entry in entries:
        kind = entry.get("type") or "photo"
        url = entry.get("media_url_https")
        if kind in ("video", "animated_gif"):
            # 挑码率最高的那个变体；没有 bitrate 的是 m3u8 播放列表，排在后面。
            variants = (entry.get("video_info") or {}).get("variants") or []
            best = max(variants, key=lambda v: _int(v.get("bitrate")) or -1, default=None)
            if best and best.get("url"):
                out.append({"type": kind, "url": best["url"], "thumbnail": url or ""})
                continue
        if url:
            out.append({"type": kind, "url": url, "thumbnail": url})
    return out


def _quoted(result: dict[str, Any]) -> tuple[str | None, str | None]:
    """被引用推文的作者与正文。

    引用推文常常只有一句「看这个」，信息全在被引用的那条里——不带上它，
    下游拿到的就是一条没有上下文的孤立发言。
    """
    quoted = (result.get("quoted_status_result") or {}).get("result") or {}
    if quoted.get("__typename") == "TweetWithVisibilityResults":
        quoted = quoted.get("tweet") or {}
    if not quoted:
        return None, None
    legacy = quoted.get("legacy") or {}
    user = ((quoted.get("core") or {}).get("user_results") or {}).get("result") or {}
    handle = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get(
        "screen_name"
    )
    note = ((quoted.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    return handle, (note.get("text") or legacy.get("full_text") or None)


def from_graphql(
    result: dict[str, Any], fetched_at: datetime, published: datetime | None
) -> TweetRecord | None:
    """把 GraphQL 的 TweetResult 转成完整记录。

    嵌套深且形状不稳（推文可能包在 `tweet` 里，用户资料在 legacy 或 core 下，
    两处都要看），所以每层都用 get 兜住——缺字段是常态，不是异常。
    """
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}

    legacy = result.get("legacy") or {}
    tweet_id = legacy.get("id_str") or result.get("rest_id")
    if not tweet_id:
        return None

    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = user.get("core") or {}
    user_legacy = user.get("legacy") or {}
    handle = core.get("screen_name") or user_legacy.get("screen_name")
    if not handle:
        return None

    # 长推文的全文在 note_tweet 里，legacy.full_text 是被截断的。
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = (note.get("text") or legacy.get("full_text") or "").strip()
    if not text:
        return None

    entities = legacy.get("entities") or {}
    quoted_handle, quoted_text = _quoted(result)
    source_html = result.get("source") or ""
    source_match = _SOURCE_TEXT.search(source_html)

    return TweetRecord(
        tweet_id=str(tweet_id),
        author_handle=handle,
        text=text,
        fetched_at=fetched_at,
        conversation_id=legacy.get("conversation_id_str"),
        author_name=core.get("name"),
        author_id=user.get("rest_id") or legacy.get("user_id_str"),
        author_avatar=(user.get("avatar") or {}).get("image_url"),
        author_verified=bool(user.get("is_blue_verified")),
        author_followers=_int(user_legacy.get("followers_count")),
        lang=legacy.get("lang"),
        created_at=published,
        likes=_int(legacy.get("favorite_count")),
        retweets=_int(legacy.get("retweet_count")),
        replies=_int(legacy.get("reply_count")),
        quotes=_int(legacy.get("quote_count")),
        bookmarks=_int(legacy.get("bookmark_count")),
        views=_int((result.get("views") or {}).get("count")),
        is_reply=bool(legacy.get("in_reply_to_status_id_str")),
        reply_to_handle=legacy.get("in_reply_to_screen_name"),
        reply_to_tweet_id=legacy.get("in_reply_to_status_id_str"),
        is_quote=bool(legacy.get("is_quote_status")),
        quoted_tweet_id=legacy.get("quoted_status_id_str"),
        quoted_handle=quoted_handle,
        quoted_text=quoted_text,
        urls=[
            {
                "url": u.get("url") or "",
                "expanded_url": u.get("expanded_url") or "",
                "display_url": u.get("display_url") or "",
            }
            for u in (entities.get("urls") or [])
        ],
        hashtags=[h.get("text") for h in (entities.get("hashtags") or []) if h.get("text")],
        mentions=[
            m.get("screen_name")
            for m in (entities.get("user_mentions") or [])
            if m.get("screen_name")
        ],
        media=_media_entries(legacy),
        possibly_sensitive=bool(legacy.get("possibly_sensitive")),
        source_client=source_match.group(1) if source_match else None,
    )
