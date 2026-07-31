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

    #: 纯转发（RT）。**外层那条推文本身没有内容**——正文是 `RT @某某: …` 的
    #: 截断，互动数记的是转发这个动作，真正的原文与热度都在被转发的那条上。
    #: 不识别出来的话，转发会被当成转发者的原创，作者归属直接错。
    is_retweet: bool = False
    retweeted_tweet_id: str | None = None
    retweeted_handle: str | None = None
    retweeted_text: str | None = None

    #: 展开后的外链：[{url, expanded_url, display_url}]。正文里是 t.co 短链，
    #: 真实地址在这里——下游要抓原文时用 expanded_url，别去解析短链。
    urls: list[dict[str, str]] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    #: [{type, url, thumbnail}]，type 为 photo / video / animated_gif
    media: list[dict[str, str]] = field(default_factory=list)

    possibly_sensitive: bool = False
    source_client: str | None = None

    #: note tweet（>280 长推）的富文本标记，X 原始形状：
    #: [{"from_index", "to_index", "richtext_types": ["Bold"|"Italic", …]}]。
    #: 存事实不存演绎——Markdown 版在读取时由 display_text 现算（同 content_kind
    #: 的原则），规则要调时历史数据不会带着旧演绎。普通推文恒为空数组：
    #: X 的短推正文不支持富文本。
    richtext_tags: list[dict[str, Any]] = field(default_factory=list)

    #: 这条推文是不是一篇长文（X Articles）的入口。搜索与时间线只给得出这个
    #: 标记和摘要，**正文要单独一次请求**——见 GraphQLBackend.fetch_article。
    has_article: bool = False
    article_id: str | None = None
    article_title: str | None = None
    article_markdown: str | None = None
    #: 正文前 ~90 字的机械截断，逐字忠实原文。**不是全文**——全文在
    #: article_markdown，这里只够列表页做预览。
    article_summary: str | None = None
    #: Grok 生成的要点归纳。**二手信息**，可能延迟生成、也可能与正文语言不一致。
    #: 单独一格是为了让下游明确知道手里拿的是机器概括而不是原文。
    article_ai_summary: str | None = None
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


def tweet_type(row: dict[str, Any]) -> str:
    """这条推文和别人是什么关系——**X 平台自己的客观事实**，不是我们的规则。

    与 `content_kind` 是两个维度，别混：这个回答「它是什么」（原创/回复/引用/
    转发），`content_kind` 回答「该怎么展示」（长文/卡片/…）。一条推文同时有
    这两个属性。

    四种关系里 `is_reply` 与 `is_quote` **可以同时成立**（在回复某人时引用了
    另一条），所以这里给的是**主类型**，精确判断仍用那几个布尔字段。
    优先级：转发 > 引用 > 回复 > 原创——转发的外层没有自己的内容，
    这个事实盖过其余。
    """
    if row.get("is_retweet"):
        return "repost"
    if row.get("is_quote"):
        return "quote"
    if row.get("is_reply"):
        return "reply"
    return "original"


def classify(row: dict[str, Any]) -> str:
    """这条推文该按什么形态展示。

    **推文不是一种内容，是几种**：一篇 3 万阅读的长文和一句 59 字的吐槽塞进
    同一个列表位，两边都不对。下游按这个字段分流：`article` / `longform`
    走文章流程（正文已在库里），其余走卡片。

    判定是**确定性规则**，不涉及语义理解——那是下游的事（同 categories 的原则）。
    优先级从高到低，一条推文可能同时满足多条，取最高的那个：

      repost   纯转发，内容整个是别人的
      article  挂了长文，正文在 article_markdown
      longform 推文本身够长，自己就是内容
      link     带站外链接，真内容在链接里
      quote    引用别人，上下文在被引那条
      brief    一句话

    `repost` 排在最前：转发的外层正文只是 `RT @某某: …` 的截断，
    把它按其它任何一类展示都会把别人的内容记在转发者名下。
    """
    if row.get("is_retweet"):
        return "repost"
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
    media = row.get("media") or []
    if row.get("has_article") and (row.get("article_markdown") or "").strip():
        title = row.get("article_title") or (row.get("text") or "")[:60]
        # 长文的配图在抓正文时已经内嵌，这里不再拼推文自身的 media——
        # 那只是长文卡片的封面预览，重复出现一次没有意义。
        return title, row["article_markdown"]

    # 转发展示被转的原文。外层的 `RT @某某: …` 是截断版，按它渲染等于
    # 把一条完整推文显示成半句话——而且看起来像是转发者自己写的。
    if row.get("is_retweet") and (row.get("retweeted_text") or "").strip():
        text = row["retweeted_text"]
        first_line = text.strip().split("\n", 1)[0]
        # RT 壳的 media 就是原推的（X 原样复制过来），拼上不算张冠李戴。
        return (first_line[:60] or text[:60]), _weave_rich_text(text, [], media)

    text = row.get("text") or ""
    # 没有标题的推文取首行；首行过长就截断，别把整段塞进标题。
    # 标题取自**无标记**的原文——`**` 混进标题只会碍事。
    first_line = text.strip().split("\n", 1)[0]
    styled = _weave_rich_text(text, row.get("richtext_tags") or [], media)
    return (first_line[:60] or text[:60]), styled


#: richtext_types 值 → Markdown 标记。与 article 侧一样按小写比对，
#: X 的大小写习惯（`Bold`）不值得信赖。
_RICHTEXT_MARK = {"bold": "**", "italic": "*"}


def _media_markdown(entry: dict[str, Any]) -> str:
    """一条媒体的 Markdown 表示。视频给「可点击的缩略图」——Markdown 嵌不了
    播放器，缩略图套上视频链接是它能表达的极限。"""
    url = entry.get("url") or ""
    thumb = entry.get("thumbnail") or ""
    if entry.get("type") in ("video", "animated_gif") and thumb and thumb != url:
        return f"[![]({thumb})]({url})"
    return f"![]({url})"


def _weave_rich_text(text: str, tags: list[dict[str, Any]], media: list[dict[str, Any]]) -> str:
    """把富文本标记和图片织进正文，产出 Markdown。没有素材时原样返回。

    样式：一段区间可以同时是 Bold+Italic（types 是数组），嵌套地各套一层。
    区间边缘的空白缩进去——`**x **` 不是合法强调，同 article 侧的处理。

    图片：note tweet 的行内图（`inline_index`）织在原文位置，其余追加在
    正文末尾。织完后把正文里指向这些媒体的 t.co 残链清掉——图已经在正文里，
    留一个指向同一张图的短链只会碍事。老数据的 media 没存 `tco`，清不了，
    原样保留（和改动前的表现一致）。
    """
    if not tags and not media:
        return text
    from .article import weave_spans  # 放函数内避免模块级循环引用

    spans: list[tuple[int, int, str, str]] = []
    for tag in tags:
        start, end = tag.get("from_index"), tag.get("to_index")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        chunk = text[start:end]
        stripped = chunk.strip()
        if not stripped:
            continue
        start += len(chunk) - len(chunk.lstrip())
        for kind in tag.get("richtext_types") or []:
            mark = _RICHTEXT_MARK.get(str(kind).lower())
            if mark:
                spans.append((start, len(stripped), mark, mark))

    trailing: list[str] = []
    for entry in media:
        if not entry.get("url"):
            continue
        idx = entry.get("inline_index")
        if isinstance(idx, int) and 0 <= idx <= len(text):
            # 零长度区间 = 单点插入。前后各空一行，别和正文粘在一起。
            spans.append((idx, 0, f"\n\n{_media_markdown(entry)}\n\n", ""))
        else:
            trailing.append(_media_markdown(entry))

    woven = weave_spans(text, spans) if spans else text
    for tco in {e.get("tco") for e in media if e.get("tco")}:
        woven = woven.replace(tco, "")
    woven = woven.rstrip()
    if trailing:
        woven = woven + "\n\n" + "\n\n".join(trailing)
    return woven


def _media_entries(legacy: dict[str, Any]) -> list[dict[str, str]]:
    """媒体优先取 extended_entities——多图推文在 entities 里只出现第一张。

    `media_id` 留着给 note tweet 的行内图片定位（inline_media 按 id 引用）；
    `tco` 是正文里指向这张图的 t.co 短链——图片拼进 display_text 之后，
    这个残链就该从正文里清掉，不然读者看到的是「图 + 一个指向同一张图的死链」。
    """
    entries = (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []
    out: list[dict[str, str]] = []
    for entry in entries:
        kind = entry.get("type") or "photo"
        url = entry.get("media_url_https")
        extra = {
            "media_id": str(entry.get("id_str") or ""),
            "tco": entry.get("url") or "",
        }
        if kind in ("video", "animated_gif"):
            # 挑码率最高的那个变体；没有 bitrate 的是 m3u8 播放列表，排在后面。
            variants = (entry.get("video_info") or {}).get("variants") or []
            best = max(variants, key=lambda v: _int(v.get("bitrate")) or -1, default=None)
            if best and best.get("url"):
                out.append({"type": kind, "url": best["url"], "thumbnail": url or "", **extra})
                continue
        if url:
            out.append({"type": kind, "url": url, "thumbnail": url, **extra})
    return out


def _inner_tweet(result: dict[str, Any], key: str) -> dict[str, Any]:
    """取嵌套的推文（被引用的 / 被转发的）。两者结构一样，只是键不同。"""
    inner = (result.get(key) or {}).get("result") or {}
    if inner.get("__typename") == "TweetWithVisibilityResults":
        inner = inner.get("tweet") or {}
    return inner


def _tweet_author_and_text(inner: dict[str, Any]) -> tuple[str | None, str | None]:
    if not inner:
        return None, None
    legacy = inner.get("legacy") or {}
    user = ((inner.get("core") or {}).get("user_results") or {}).get("result") or {}
    handle = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get(
        "screen_name"
    )
    note = ((inner.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    return handle, (note.get("text") or legacy.get("full_text") or None)


def _quoted(result: dict[str, Any]) -> tuple[str | None, str | None]:
    """被引用推文的作者与正文。

    引用推文常常只有一句「看这个」，信息全在被引用的那条里——不带上它，
    下游拿到的就是一条没有上下文的孤立发言。
    """
    return _tweet_author_and_text(_inner_tweet(result, "quoted_status_result"))


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
    raw_text = note.get("text") or legacy.get("full_text") or ""
    text = raw_text.strip()
    if not text:
        return None

    # 富文本标记的下标指向**未 strip 的** note 正文；上面剥掉了首部空白，
    # 下标就得跟着平移，否则整套标记向右错一段。
    richtext_tags: list[dict[str, Any]] = []
    media_entries = _media_entries(legacy)
    if note.get("text"):
        lead = len(raw_text) - len(raw_text.lstrip())
        # note tweet 的图片可以插在正文中间（inline_media 按 media_id 指位置），
        # 位置记进 media 条目里，display_text 织入时按它落位。
        for im in ((note.get("media") or {}).get("inline_media") or []):
            mid, idx = str(im.get("media_id") or ""), _int(im.get("index"))
            if not mid or idx is None:
                continue
            for entry in media_entries:
                if entry.get("media_id") == mid:
                    entry["inline_index"] = max(idx - lead, 0)
        for tag in ((note.get("richtext") or {}).get("richtext_tags") or []):
            start, end = _int(tag.get("from_index")), _int(tag.get("to_index"))
            kinds = [str(k) for k in (tag.get("richtext_types") or [])]
            if start is None or end is None or not kinds:
                continue
            start, end = max(start - lead, 0), min(end - lead, len(text))
            if end > start:
                richtext_tags.append(
                    {"from_index": start, "to_index": end, "richtext_types": kinds}
                )

    entities = legacy.get("entities") or {}
    article = ((result.get("article") or {}).get("article_results") or {}).get("result") or {}
    quoted_handle, quoted_text = _quoted(result)
    retweeted = _inner_tweet(legacy, "retweeted_status_result")
    rt_handle, rt_text = _tweet_author_and_text(retweeted)
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
        is_retweet=bool(retweeted),
        retweeted_tweet_id=(retweeted.get("legacy") or {}).get("id_str") or None,
        retweeted_handle=rt_handle,
        retweeted_text=rt_text,
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
        media=media_entries,
        possibly_sensitive=bool(legacy.get("possibly_sensitive")),
        source_client=source_match.group(1) if source_match else None,
        richtext_tags=richtext_tags,
        has_article=bool(article),
        article_id=(article.get("rest_id") if article else None),
        article_title=(article.get("title") if article else None),
        # 正文这里一定是 None——常规接口给不出来。留给 fetch_article 填。
        article_summary=(article.get("preview_text") if article else None),
        article_ai_summary=(article.get("summary_text") if article else None),
        article_cover=(
            ((article.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
            if article else None
        ),
    )
