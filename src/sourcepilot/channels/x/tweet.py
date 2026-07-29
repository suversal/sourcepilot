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

    #: 这条推文是不是一篇长文（X Articles）的入口。搜索与时间线只给得出这个
    #: 标记和摘要，**正文要单独一次请求**——见 GraphQLBackend.fetch_article。
    has_article: bool = False
    article_id: str | None = None
    article_title: str | None = None
    article_markdown: str | None = None
    article_summary: str | None = None
    article_cover: str | None = None

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


#: X 长推文的门槛。超过这个长度说明作者是在写内容，不是随口一句。
#: 取 280 是因为那正是 X 免费账号的单条上限——能超过它的都是有意为之。
LONGFORM_CHARS = 280


def classify(row: dict[str, Any]) -> str:
    """这条推文该按什么形态展示。

    **推文不是一种内容，是几种**：一篇 3 万阅读的长文和一句 59 字的吐槽塞进
    同一个列表位，两边都不对。下游按这个字段分流：`article` / `longform`
    走文章流程（正文已在库里），其余走卡片。

    判定是**确定性规则**，不涉及语义理解——那是下游的事（同 categories 的原则）。
    优先级从高到低，一条推文可能同时满足多条，取最高的那个：

      article  挂了长文，正文在 article_markdown
      longform 推文本身够长，自己就是内容
      link     带站外链接，真内容在链接里
      quote    引用别人，上下文在被引那条
      brief    一句话
    """
    if row.get("has_article"):
        return "article"
    if len(row.get("text") or "") > LONGFORM_CHARS:
        return "longform"
    if row.get("external_urls"):
        return "link"
    if row.get("is_quote"):
        return "quote"
    return "brief"


def display_fields(row: dict[str, Any]) -> tuple[str, str]:
    """展示用的标题与正文，省掉下游在展示层写分支。

    长文的 `text` 只有一句入口语（「我整理成一篇长文」），真内容在
    `article_markdown` 里——按 text 渲染会把一条 3 万阅读的内容显示成一句废话。

    带外链的那类**不在这里解析外链正文**：那要额外出网，是 read_article 的活，
    不该藏在一个取字段的函数里。下游拿 external_urls 自己去抓。
    """
    if row.get("has_article") and (row.get("article_markdown") or "").strip():
        title = row.get("article_title") or (row.get("text") or "")[:60]
        return title, row["article_markdown"]

    text = row.get("text") or ""
    # 没有标题的推文取首行；首行过长就截断，别把整段塞进标题。
    first_line = text.strip().split("\n", 1)[0]
    return (first_line[:60] or text[:60]), text


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
    article = ((result.get("article") or {}).get("article_results") or {}).get("result") or {}
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
        has_article=bool(article),
        article_id=(article.get("rest_id") if article else None),
        article_title=(article.get("title") if article else None),
        # 正文这里一定是 None——常规接口给不出来。留给 fetch_article 填。
        article_summary=(article.get("preview_text") if article else None),
        article_cover=(
            ((article.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
            if article else None
        ),
    )
