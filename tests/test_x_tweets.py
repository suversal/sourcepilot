"""推文全貌表。

Item 是跨源统一 schema，推文特有的东西（互动数、引用链、展开外链）在别的源
里没有对应概念。硬塞只能进 `raw`，而契约声明 raw 结构不稳定、消费方不得依赖
——下游要拿它做展示就需要一个稳定形状，所以单独一张表。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sourcepilot.channels.x.tweet import from_graphql

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def result(**over):
    base = {
        "__typename": "Tweet",
        "rest_id": "1",
        "core": {"user_results": {"result": {
            "rest_id": "u1",
            "core": {"name": "张三", "screen_name": "zhangsan"},
            "avatar": {"image_url": "https://pbs.twimg.com/a.jpg"},
            "is_blue_verified": True,
            "legacy": {"followers_count": 1234},
        }}},
        "legacy": {
            "id_str": "1", "full_text": "正文", "lang": "zh",
            "favorite_count": 10, "retweet_count": 2, "reply_count": 1,
            "quote_count": 3, "bookmark_count": 4,
            "conversation_id_str": "c1",
        },
        "views": {"count": "999"},
        "source": '<a href="https://mobile.twitter.com" rel="nofollow">Twitter Web App</a>',
    }
    for k, v in over.items():
        if k == "legacy":
            base["legacy"] = {**base["legacy"], **v}
        else:
            base[k] = v
    return base


class TestLinksAreExpanded:
    """X 把正文里的链接一律换成 t.co 短链，但响应里已经给了真实地址。

    不需要额外发请求去解析——那既慢，又会在对方统计里留下一次点击。
    """

    def test_expanded_url_is_kept(self):
        r = from_graphql(result(legacy={"entities": {"urls": [
            {"url": "https://t.co/abc", "expanded_url": "https://example.com/article",
             "display_url": "example.com/article"}
        ]}}), NOW, NOW)
        assert r.urls[0]["expanded_url"] == "https://example.com/article"
        assert r.external_urls == ["https://example.com/article"]

    def test_self_links_are_not_external(self):
        """指回 x.com 的是引用推文/图片的自身链接，不是站外原文。"""
        r = from_graphql(result(legacy={"entities": {"urls": [
            {"url": "https://t.co/a", "expanded_url": "https://x.com/foo/status/9"},
            {"url": "https://t.co/b", "expanded_url": "https://twitter.com/bar/status/8"},
            {"url": "https://t.co/c", "expanded_url": "https://news.site/post"},
        ]}}), NOW, NOW)
        assert r.external_urls == ["https://news.site/post"]

    def test_no_links_gives_empty_list(self):
        assert from_graphql(result(), NOW, NOW).external_urls == []


class TestQuotedTweetCarriesContext:
    def test_quoted_text_and_author(self):
        """引用推文常常只有一句「看这个」，信息全在被引用那条里。"""
        r = from_graphql(result(quoted_status_result={"result": {
            "legacy": {"full_text": "被引用的原文"},
            "core": {"user_results": {"result": {"core": {"screen_name": "someone"}}}},
        }}), NOW, NOW)
        assert r.quoted_handle == "someone"
        assert r.quoted_text == "被引用的原文"

    def test_no_quote_is_none_not_empty_string(self):
        r = from_graphql(result(), NOW, NOW)
        assert r.quoted_text is None and r.quoted_handle is None


class TestFullTextNotTruncated:
    def test_note_tweet_wins_over_truncated_full_text(self):
        """长推文的 legacy.full_text 是截断的，全文在 note_tweet 里。"""
        r = from_graphql(result(
            legacy={"full_text": "开头……"},
            note_tweet={"note_tweet_results": {"result": {"text": "完整的长正文" * 20}}},
        ), NOW, NOW)
        assert r.text.startswith("完整的长正文完整的长正文")


class TestEngagementAndAuthor:
    def test_all_counters_are_captured(self):
        r = from_graphql(result(), NOW, NOW)
        assert (r.likes, r.retweets, r.replies, r.quotes, r.bookmarks, r.views) == (
            10, 2, 1, 3, 4, 999
        )

    def test_author_profile(self):
        r = from_graphql(result(), NOW, NOW)
        assert (r.author_name, r.author_followers, r.author_verified) == ("张三", 1234, True)

    def test_source_client_is_text_not_html(self):
        """source 是一段 HTML 锚点，要的只是里面的文字。"""
        assert from_graphql(result(), NOW, NOW).source_client == "Twitter Web App"


class TestMedia:
    def test_video_picks_highest_bitrate(self):
        r = from_graphql(result(legacy={"extended_entities": {"media": [{
            "type": "video", "media_url_https": "https://p/thumb.jpg",
            "video_info": {"variants": [
                {"bitrate": 256000, "url": "https://v/low.mp4"},
                {"bitrate": 2176000, "url": "https://v/high.mp4"},
                {"url": "https://v/playlist.m3u8"},
            ]},
        }]}}), NOW, NOW)
        assert r.media[0]["url"] == "https://v/high.mp4"

    def test_multi_photo_uses_extended_entities(self):
        """多图推文在 entities 里只出现第一张，全部在 extended_entities。"""
        r = from_graphql(result(legacy={
            "entities": {"media": [{"type": "photo", "media_url_https": "https://p/1.jpg"}]},
            "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://p/1.jpg"},
                {"type": "photo", "media_url_https": "https://p/2.jpg"},
            ]},
        }), NOW, NOW)
        assert len(r.media) == 2


class TestPersistence:
    def _record(self, store, **over):
        from sourcepilot.channels.x.tweet import TweetRecord

        base = {"tweet_id": "1", "author_handle": "zhangsan", "text": "正文",
                "fetched_at": NOW, "created_at": NOW}
        store.upsert_tweets([TweetRecord(**{**base, **over})])

    def test_round_trip(self, store):
        self._record(store, likes=5, urls=[
            {"url": "https://t.co/x", "expanded_url": "https://a.com/p", "display_url": "a.com/p"}
        ])
        (row,) = store.query_tweets(limit=10)
        assert row["likes"] == 5
        assert row["external_urls"] == ["https://a.com/p"]
        assert row["url"] == "https://x.com/zhangsan/status/1"

    def test_counters_are_overwritten_on_recollect(self, store):
        """互动数会随时间涨，重复采集要覆盖而不是跳过。"""
        self._record(store, likes=5)
        self._record(store, likes=99)
        assert store.query_tweets(limit=10)[0]["likes"] == 99
        assert store.count_tweets() == 1

    def test_filter_by_handle_and_links(self, store):
        self._record(store, tweet_id="1", author_handle="a")
        self._record(store, tweet_id="2", author_handle="b",
                     urls=[{"url": "t", "expanded_url": "https://x.io/1", "display_url": "d"}])
        assert len(store.query_tweets(handle="a", limit=10)) == 1
        assert len(store.query_tweets(has_links=True, limit=10)) == 1

    def test_conversation_groups_a_thread(self, store):
        self._record(store, tweet_id="1", conversation_id="c9")
        self._record(store, tweet_id="2", conversation_id="c9")
        self._record(store, tweet_id="3", conversation_id="other")
        assert len(store.query_tweets(conversation_id="c9", limit=10)) == 2


class TestSinkIsOptional:
    def test_unbound_sink_drops_silently(self):
        """没绑定 store 时丢的只是推文表这份附加视图，Item 照常入库。"""
        from sourcepilot.channels.x import _TweetSink
        from sourcepilot.channels.x.tweet import TweetRecord

        assert _TweetSink().write([TweetRecord("1", "a", "t", NOW)]) == 0

    def test_write_failure_does_not_raise(self, store, monkeypatch):
        """推文表写失败不该让整轮采集失败——Item 才是主线。"""
        from sourcepilot.channels.x import _TweetSink
        from sourcepilot.channels.x.tweet import TweetRecord

        sink = _TweetSink()
        sink.bind(store)
        monkeypatch.setattr(
            store, "upsert_tweets", lambda r: (_ for _ in ()).throw(RuntimeError("库炸了"))
        )
        assert sink.write([TweetRecord("1", "a", "t", NOW)]) == 0


class TestEndpoint:
    @pytest.fixture
    def client(self, store):
        from fastapi.testclient import TestClient

        from sourcepilot.api import create_app
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([
            TweetRecord("1", "alice", "谈论 giffgaff 封号", NOW, created_at=NOW, likes=7,
                        urls=[{"url": "https://t.co/a", "expanded_url": "https://news.site/x",
                               "display_url": "news.site/x"}]),
            TweetRecord("2", "bob", "无关内容", NOW, created_at=NOW),
        ])
        return TestClient(create_app(store=store, sources={}, scheduler=False))

    def test_returns_the_full_shape(self, client):
        r = client.get("/api/v1/x/tweets?q=giffgaff")
        assert r.status_code == 200
        (tweet,) = r.json()["data"]["tweets"]
        assert tweet["likes"] == 7
        assert tweet["external_urls"] == ["https://news.site/x"]

    def test_filters_by_author(self, client):
        assert len(client.get("/api/v1/x/tweets?handle=bob").json()["data"]["tweets"]) == 1

    def test_bad_since_is_a_clear_error(self, client):
        r = client.get("/api/v1/x/tweets?since=昨天")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "BAD_REQUEST"

    def test_reports_cache_mode(self, client):
        """这个端点只读缓存，不触发抓取——meta 要如实说。"""
        assert client.get("/api/v1/x/tweets").json()["meta"]["mode"] == "cache"


class TestArticleMarkdown:
    """X 长文的正文是 Draft.js 的 {blocks, entityMap}。

    转 Markdown 而不是直接用 `plain_text`：后者把标题和正文拍平成同样的行、
    链接只剩锚文本，下游拿到的是一堆看不出结构的段落。
    """

    def _state(self, blocks, entity_map=None):
        return {"blocks": blocks, "entityMap": entity_map or {}}

    def test_headers_become_markdown_headings(self):
        from sourcepilot.channels.x.article import to_markdown

        md = to_markdown(self._state([
            {"type": "header-two", "text": "一、发生了什么"},
            {"type": "unstyled", "text": "正文段落"},
        ]))
        assert md == "## 一、发生了什么\n\n正文段落"

    def test_links_are_restored_from_entity_map(self):
        from sourcepilot.channels.x.article import to_markdown

        md = to_markdown(self._state(
            [{"type": "unstyled", "text": "详见官网说明",
              "entityRanges": [{"offset": 2, "length": 2, "key": "0"}]}],
            {"0": {"type": "LINK", "data": {"url": "https://a.com"}}},
        ))
        assert md == "详见[官网](https://a.com)说明"

    def test_multiple_links_do_not_shift_each_other(self):
        """从前往后替换会让后一个 offset 全部错位——必须倒着来。"""
        from sourcepilot.channels.x.article import to_markdown

        md = to_markdown(self._state(
            [{"type": "unstyled", "text": "AAA BBB",
              "entityRanges": [{"offset": 0, "length": 3, "key": "0"},
                               {"offset": 4, "length": 3, "key": "1"}]}],
            {"0": {"type": "LINK", "data": {"url": "https://1"}},
             "1": {"type": "LINK", "data": {"url": "https://2"}}},
        ))
        assert md == "[AAA](https://1) [BBB](https://2)"

    def test_entity_map_accepts_both_shapes(self):
        """X 有时给 dict、有时给 list——只认一种会在另一种上静默丢掉所有链接。"""
        from sourcepilot.channels.x.article import to_markdown

        blocks = [{"type": "unstyled", "text": "看这里",
                   "entityRanges": [{"offset": 0, "length": 3, "key": "7"}]}]
        as_list = to_markdown(self._state(
            blocks, [{"key": "7", "value": {"type": "LINK", "data": {"url": "https://x.io"}}}]))
        as_dict = to_markdown(self._state(
            blocks, {"7": {"type": "LINK", "data": {"url": "https://x.io"}}}))
        assert as_list == as_dict == "[看这里](https://x.io)"

    def test_images_resolve_through_media_map(self):
        from sourcepilot.channels.x.article import to_markdown

        md = to_markdown(
            self._state([{"type": "atomic", "text": " ",
                          "entityRanges": [{"offset": 0, "length": 1, "key": "0"}]}],
                        {"0": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "m1"}]}}}),
            {"m1": "https://pbs.twimg.com/a.jpg"},
        )
        assert md == "![](https://pbs.twimg.com/a.jpg)"

    def test_unresolvable_image_is_skipped_not_broken(self):
        """`![](None)` 比没有图更糟。"""
        from sourcepilot.channels.x.article import to_markdown

        md = to_markdown(
            self._state([
                {"type": "atomic", "text": " ",
                 "entityRanges": [{"offset": 0, "length": 1, "key": "0"}]},
                {"type": "unstyled", "text": "后续正文"},
            ], {"0": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "unknown"}]}}}),
            {},
        )
        assert md == "后续正文"

    def test_falls_back_to_plain_text(self):
        """结构解析不出来时宁可少格式，也不能没内容。"""
        from sourcepilot.channels.x.article import parse

        out = parse({"rest_id": "9", "title": "标题",
                     "content_state": {"blocks": []}, "plain_text": "兜底正文"})
        assert out["article_markdown"] == "兜底正文"

    def test_empty_article_is_none(self):
        from sourcepilot.channels.x.article import parse

        assert parse({}) is None
        assert parse({"rest_id": "9", "content_state": {"blocks": []}, "plain_text": ""}) is None


class TestInlineStyles:
    """行内加粗/斜体在 inlineStyleRanges 里。

    实测 X 给的 style 值是 `Bold` 这种首字母大写，不是 Draft.js 文档里的
    全大写 `BOLD`——两种都得认。
    """

    def _md(self, blocks, entity_map=None):
        from sourcepilot.channels.x.article import to_markdown

        return to_markdown({"blocks": blocks, "entityMap": entity_map or {}})

    def test_bold_is_woven_in(self):
        md = self._md([{"type": "unstyled", "text": "重点内容在此",
                        "inlineStyleRanges": [{"offset": 0, "length": 2, "style": "Bold"}]}])
        assert md == "**重点**内容在此"

    def test_style_case_is_forgiven(self):
        for style in ("Bold", "BOLD", "bold"):
            md = self._md([{"type": "unstyled", "text": "AB",
                            "inlineStyleRanges": [{"offset": 0, "length": 2, "style": style}]}])
            assert md == "**AB**", style

    def test_italic(self):
        md = self._md([{"type": "unstyled", "text": "强调词",
                        "inlineStyleRanges": [{"offset": 0, "length": 3, "style": "Italic"}]}])
        assert md == "*强调词*"

    def test_bold_link_nests_instead_of_crossing(self):
        """加粗的链接：两个区间重合，标记必须嵌套而不是交叉。"""
        md = self._md(
            [{"type": "unstyled", "text": "AAA",
              "entityRanges": [{"offset": 0, "length": 3, "key": "0"}],
              "inlineStyleRanges": [{"offset": 0, "length": 3, "style": "Bold"}]}],
            {"0": {"type": "LINK", "data": {"url": "https://1"}}},
        )
        assert md == "**[AAA](https://1)**"

    def test_edge_whitespace_is_tucked_inside(self):
        """`**加粗 **` 不是合法的 Markdown 强调——边缘空白要缩进区间里。"""
        md = self._md([{"type": "unstyled", "text": "AB cd",
                        "inlineStyleRanges": [{"offset": 0, "length": 3, "style": "Bold"}]}])
        assert md == "**AB** cd"

    def test_code_block_keeps_literal_asterisks(self):
        """代码块里的星号是字面内容，不套强调。"""
        md = self._md([{"type": "code-block", "text": "a = b",
                        "inlineStyleRanges": [{"offset": 0, "length": 5, "style": "Bold"}]}])
        assert md == "    a = b"


class TestNoteRichtext:
    """note tweet（>280 长推）的加粗/斜体在 note_tweet.richtext.richtext_tags 里。

    存的是 X 原始形状的事实，Markdown 版由 display_text 读取时现算——
    同 content_kind 的原则，规则要调时历史数据不会带着旧演绎。
    """

    def _note(self, text, tags):
        return result(note_tweet={"note_tweet_results": {"result": {
            "text": text, "richtext": {"richtext_tags": tags}}}})

    def test_tags_are_captured(self):
        r = from_graphql(self._note(
            "长文开头，重点在后面" + "填充" * 140,
            [{"from_index": 5, "to_index": 7, "richtext_types": ["Bold"]}],
        ), NOW, NOW)
        assert r.richtext_tags == [
            {"from_index": 5, "to_index": 7, "richtext_types": ["Bold"]}
        ]

    def test_indices_shift_with_leading_strip(self):
        """正文入库前 strip 过，标记下标指向未 strip 的原文——必须跟着平移。"""
        r = from_graphql(self._note(
            "\n\n重点在开头",
            [{"from_index": 2, "to_index": 4, "richtext_types": ["Bold"]}],
        ), NOW, NOW)
        assert r.richtext_tags == [
            {"from_index": 0, "to_index": 2, "richtext_types": ["Bold"]}
        ]
        assert r.text[0:2] == "重点"

    def test_short_tweet_has_no_tags(self):
        assert from_graphql(result(), NOW, NOW).richtext_tags == []

    def test_display_text_weaves_markdown_but_title_stays_clean(self):
        from sourcepilot.channels.x.tweet import display_fields

        title, text = display_fields({
            "text": "重点内容在此",
            "richtext_tags": [{"from_index": 0, "to_index": 2,
                               "richtext_types": ["Bold"]}],
        })
        assert text == "**重点**内容在此"
        assert title == "重点内容在此"

    def test_bold_italic_combo_nests(self):
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields({
            "text": "abc",
            "richtext_tags": [{"from_index": 0, "to_index": 3,
                               "richtext_types": ["Bold", "Italic"]}],
        })
        assert text == "***abc***"

    def test_inline_media_position_is_captured_and_shifted(self):
        """note tweet 的行内图按 media_id 指到正文位置，随 strip 平移。"""
        r = from_graphql(result(
            legacy={"extended_entities": {"media": [
                {"type": "photo", "id_str": "m1", "url": "https://t.co/pic",
                 "media_url_https": "https://pbs.twimg.com/a.jpg"},
            ]}},
            note_tweet={"note_tweet_results": {"result": {
                "text": "\n开头段落，图在这之后。结尾。",
                "media": {"inline_media": [{"media_id": "m1", "index": 10}]},
            }}},
        ), NOW, NOW)
        assert r.media[0]["inline_index"] == 9
        assert r.media[0]["tco"] == "https://t.co/pic"


class TestMediaInDisplayText:
    """图片拼进 display_text——富文本正文自带配图，下游不用再对照 media 数组。"""

    def _row(self, **over):
        return {"text": "看这张图 https://t.co/pic", **over}

    def test_photo_appended_and_tco_stripped(self):
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields(self._row(media=[
            {"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
             "thumbnail": "https://pbs.twimg.com/a.jpg", "tco": "https://t.co/pic"},
        ]))
        assert text == "看这张图\n\n![](https://pbs.twimg.com/a.jpg)"

    def test_video_is_clickable_thumbnail(self):
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields({"text": "视频", "media": [
            {"type": "video", "url": "https://video.twimg.com/v.mp4",
             "thumbnail": "https://pbs.twimg.com/thumb.jpg"},
        ]})
        assert text == "视频\n\n[![](https://pbs.twimg.com/thumb.jpg)](https://video.twimg.com/v.mp4)"

    def test_inline_media_lands_mid_text(self):
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields({"text": "上文。下文。", "media": [
            {"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
             "thumbnail": "https://pbs.twimg.com/a.jpg", "inline_index": 3},
        ]})
        assert text == "上文。\n\n![](https://pbs.twimg.com/a.jpg)\n\n下文。"

    def test_legacy_rows_without_tco_keep_text_untouched(self):
        """老数据的 media 没存 tco——正文里的短链清不掉，原样保留不算错。"""
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields(self._row(media=[
            {"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
             "thumbnail": "https://pbs.twimg.com/a.jpg"},
        ]))
        assert text == "看这张图 https://t.co/pic\n\n![](https://pbs.twimg.com/a.jpg)"

    def test_article_does_not_get_media_appended(self):
        """长文的配图已内嵌在 article_markdown，推文自身的封面预览不再拼一次。"""
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields({
            "text": "长文入口", "has_article": True,
            "article_title": "标题", "article_markdown": "# 正文",
            "media": [{"type": "photo", "url": "https://pbs.twimg.com/cover.jpg",
                       "thumbnail": "https://pbs.twimg.com/cover.jpg"}],
        })
        assert text == "# 正文"

    def test_repost_carries_original_media(self):
        from sourcepilot.channels.x.tweet import display_fields

        _, text = display_fields({
            "text": "RT @a: 截断…", "is_retweet": True, "retweeted_text": "原文全文",
            "media": [{"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
                       "thumbnail": "https://pbs.twimg.com/a.jpg"}],
        })
        assert text == "原文全文\n\n![](https://pbs.twimg.com/a.jpg)"

    def test_round_trip_through_store(self, store):
        """media 的 tco/inline_index 经 JSON 列往返不丢，display_text 拼好图。"""
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            tweet_id="1", author_handle="a", text="看图 https://t.co/z",
            fetched_at=NOW, created_at=NOW,
            media=[{"type": "photo", "url": "https://pbs.twimg.com/a.jpg",
                    "thumbnail": "https://pbs.twimg.com/a.jpg",
                    "media_id": "m1", "tco": "https://t.co/z"}],
        )])
        (row,) = store.query_tweets(limit=10)
        assert row["display_text"] == "看图\n\n![](https://pbs.twimg.com/a.jpg)"

    def test_richtext_round_trip_through_store(self, store):
        """richtext_tags 经 JSON 列往返不丢，读出来的 display_text 已带标记。"""
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            tweet_id="1", author_handle="a", text="重点内容在此", fetched_at=NOW,
            created_at=NOW,
            richtext_tags=[{"from_index": 0, "to_index": 2, "richtext_types": ["Bold"]}],
        )])
        (row,) = store.query_tweets(limit=10)
        assert row["richtext_tags"] == [
            {"from_index": 0, "to_index": 2, "richtext_types": ["Bold"]}
        ]
        assert row["display_text"] == "**重点**内容在此"


class TestArticleIsFlaggedButNotFetchedInline:
    """搜索与时间线返回的 article 只有预览，正文要单独一次请求。"""

    def test_search_result_marks_has_article_without_body(self):
        from sourcepilot.channels.x.tweet import from_graphql

        r = from_graphql({
            "rest_id": "1",
            "core": {"user_results": {"result": {"core": {"screen_name": "a"}}}},
            "legacy": {"id_str": "1", "full_text": "看我的长文"},
            "article": {"article_results": {"result": {
                "rest_id": "art1", "title": "长文标题", "preview_text": "预览……"}}},
        }, NOW, NOW)
        assert r.has_article is True
        assert r.article_title == "长文标题"
        assert r.article_markdown is None, "常规接口给不出正文"

    def test_plain_tweet_is_not_flagged(self):
        from sourcepilot.channels.x.tweet import from_graphql

        r = from_graphql({
            "rest_id": "1",
            "core": {"user_results": {"result": {"core": {"screen_name": "a"}}}},
            "legacy": {"id_str": "1", "full_text": "普通推文"},
        }, NOW, NOW)
        assert r.has_article is False


class TestArticleTextSurvivesRecollection:
    def test_body_is_not_wiped_by_a_later_plain_collect(self, store):
        """正文是单独一次请求换来的；常规采集拿不到它，不能覆盖成空。"""
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            "1", "a", "看我的长文", NOW, has_article=True,
            article_title="标题", article_markdown="# 完整正文")])
        # 下一轮搜索又碰到这条，但这次只有预览
        store.upsert_tweets([TweetRecord(
            "1", "a", "看我的长文", NOW, has_article=True, likes=99)])
        (row,) = store.query_tweets(limit=10)
        assert row["article_markdown"] == "# 完整正文", "正文不该被抹掉"
        assert row["likes"] == 99, "但互动数照常更新"

    def test_missing_article_text_finds_the_backlog(self, store):
        """补抓任务照这个清单干活。"""
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([
            TweetRecord("1", "a", "有长文没正文", NOW, has_article=True),
            TweetRecord("2", "a", "有长文有正文", NOW, has_article=True,
                        article_markdown="# 有了"),
            TweetRecord("3", "a", "普通推文", NOW),
        ])
        pending = store.query_tweets(missing_article_text=True, limit=10)
        assert [p["tweet_id"] for p in pending] == ["1"]


class TestContentKind:
    """推文不是一种内容，是几种。

    一篇 3 万阅读的长文和一句 59 字的吐槽塞进同一个列表位，两边都不对。
    判定是确定性规则，不涉及语义理解——那是下游的事。
    """

    def _row(self, **over):
        base = {"text": "短推文", "has_article": False, "external_urls": [], "is_quote": False}
        return {**base, **over}

    def test_article_wins_over_everything(self):
        from sourcepilot.channels.x.tweet import classify

        # 长文推文常常同时带链接、又是引用——但它首先是长文
        assert classify(self._row(
            has_article=True, external_urls=["https://a"], is_quote=True)) == "article"

    def test_longform_by_length(self):
        from sourcepilot.channels.x.tweet import LONGFORM_CHARS, classify

        assert classify(self._row(text="x" * (LONGFORM_CHARS + 1))) == "longform"
        assert classify(self._row(text="x" * LONGFORM_CHARS)) == "brief"

    def test_link_and_quote_and_brief(self):
        from sourcepilot.channels.x.tweet import classify

        assert classify(self._row(external_urls=["https://a"])) == "link"
        assert classify(self._row(is_quote=True)) == "quote"
        assert classify(self._row()) == "brief"


class TestDisplayFields:
    def test_article_shows_the_body_not_the_teaser(self):
        """长文的 text 只是一句入口语，按它渲染会让 3 万阅读的内容显示成一句废话。"""
        from sourcepilot.channels.x.tweet import display_fields

        title, body = display_fields({
            "text": "我整理成一篇长文，建议自查",
            "has_article": True,
            "article_title": "Giffgaff 大规模封号",
            "article_markdown": "## 一、发生了什么\n\n正文……",
        })
        assert title == "Giffgaff 大规模封号"
        assert body.startswith("## 一、发生了什么")

    def test_empty_article_body_falls_back_to_text(self):
        """has_article 但正文还没补到时，别给一个空白正文。"""
        from sourcepilot.channels.x.tweet import display_fields

        _, body = display_fields({
            "text": "我整理成一篇长文", "has_article": True, "article_markdown": "  "
        })
        assert body == "我整理成一篇长文"

    def test_title_takes_the_first_line_only(self):
        from sourcepilot.channels.x.tweet import display_fields

        title, body = display_fields({"text": "第一行标题\n第二行正文", "has_article": False})
        assert title == "第一行标题"
        assert body == "第一行标题\n第二行正文"


class TestThread:
    def _add(self, store, tweet_id, author, reply_to=None, text="内容", minute=0):
        from datetime import timedelta

        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            tweet_id, author, text, NOW, created_at=NOW + timedelta(minutes=minute),
            conversation_id="c1", is_reply=bool(reply_to), reply_to_handle=reply_to)])

    def test_thread_is_chronological(self, store):
        """作者连发几条讲一件事，拆成几个卡片会很碎。"""
        self._add(store, "3", "alice", "alice", "第三条", 20)
        self._add(store, "1", "alice", None, "第一条", 0)
        self._add(store, "2", "alice", "alice", "第二条", 10)
        assert [t["text"] for t in store.query_thread("c1")] == ["第一条", "第二条", "第三条"]

    def test_others_replies_are_excluded(self, store):
        self._add(store, "1", "alice", None, "原推", 0)
        self._add(store, "2", "bob", "alice", "路人评论", 5)
        assert [t["author_handle"] for t in store.query_thread("c1")] == ["alice"]

    def test_author_replying_to_someone_else_is_excluded(self, store):
        """作者在自己线程下回复网友提问——作者对、线程对，但那是评论区互动。"""
        self._add(store, "1", "alice", None, "原推", 0)
        self._add(store, "2", "alice", "alice", "接着说", 5)
        self._add(store, "3", "alice", "路人甲", "@路人甲 是的", 10)
        assert [t["text"] for t in store.query_thread("c1")] == ["原推", "接着说"]

    def test_author_only_off_keeps_everything(self, store):
        self._add(store, "1", "alice", None, "原推", 0)
        self._add(store, "2", "bob", "alice", "路人评论", 5)
        assert len(store.query_thread("c1", author_only=False)) == 2


class TestKindFilterEndpoint:
    @pytest.fixture
    def client(self, store):
        from fastapi.testclient import TestClient

        from sourcepilot.api import create_app
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([
            TweetRecord("1", "a", "短", NOW, created_at=NOW),
            TweetRecord("2", "a", "带长文", NOW, created_at=NOW,
                        has_article=True, article_markdown="# 正文", article_title="标题"),
        ])
        return TestClient(create_app(store=store, sources={}, scheduler=False))

    def test_filter_by_kind(self, client):
        r = client.get("/api/v1/x/tweets?kind=article")
        (tweet,) = r.json()["data"]["tweets"]
        assert tweet["content_kind"] == "article"

    def test_unknown_kind_lists_the_valid_ones(self, client):
        r = client.get("/api/v1/x/tweets?kind=文章")
        assert r.status_code == 400
        assert "article" in r.json()["error"]["message"]


class TestTwoSummariesAreKeptApart:
    """X 给了两个摘要字段，性质完全不同，混在一起会让来源随抓取时机漂移。

    实测：同一篇长文早抓只有 preview_text（正文截断），晚抓才有 summary_text
    （Grok 生成）。旧写法 `summary_text or preview_text` 的结果就取决于运气，
    下游拿到手也分不清是原文还是机器概括——而且见过中文长文配英文 AI 摘要。
    """

    def _article(self, **over):
        base = {
            "rest_id": "a1",
            "title": "标题",
            "content_state": {"blocks": [{"type": "unstyled", "text": "正文段落"}]},
            "preview_text": "正文段落开头的截断",
            "summary_text": "- 机器归纳的要点",
        }
        return {**base, **over}

    def test_summary_is_the_faithful_excerpt(self):
        from sourcepilot.channels.x.article import parse

        out = parse(self._article())
        assert out["article_summary"] == "正文段落开头的截断"

    def test_ai_summary_is_kept_separately(self):
        from sourcepilot.channels.x.article import parse

        assert parse(self._article())["article_ai_summary"] == "- 机器归纳的要点"

    def test_missing_grok_summary_does_not_fall_back(self):
        """X 没生成 Grok 摘要时，ai_summary 就该是空的。

        退回 preview_text 的话，下游会把一段原文截断当成机器摘要用——
        实测确实有长文拿不到 summary_text。
        """
        from sourcepilot.channels.x.article import parse

        out = parse(self._article(summary_text=None))
        assert out["article_ai_summary"] is None
        assert out["article_summary"] == "正文段落开头的截断", "忠实摘要不受影响"

    def test_neither_summary_is_the_full_text(self):
        """两个都是摘要，全文只在 article_markdown。"""
        from sourcepilot.channels.x.article import parse

        out = parse(self._article())
        assert out["article_markdown"] == "正文段落"
        assert len(out["article_summary"]) != len(out["article_markdown"])

    def test_both_survive_a_later_plain_collect(self, store):
        """常规采集拿不到长文字段，不能把已抓好的两个摘要覆盖成空。"""
        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            "1", "a", "入口语", NOW, has_article=True, article_markdown="# 正文",
            article_summary="忠实截断", article_ai_summary="- 机器要点")])
        store.upsert_tweets([TweetRecord("1", "a", "入口语", NOW, has_article=True, likes=7)])
        (row,) = store.query_tweets(limit=10)
        assert row["article_summary"] == "忠实截断"
        assert row["article_ai_summary"] == "- 机器要点"
        assert row["likes"] == 7


class TestRetweetIsRecognized:
    """转发不识别出来的话，作者归属直接错。

    实测：@AnthropicAI 转发 @claudeai 的推文，外层 text 是
    `RT @claudeai: …` 的截断，作者记成 AnthropicAI——下游会以为
    这是 Anthropic 官方原创，而真正的作者是 claudeai。
    """

    def _rt(self, inner_handle="claudeai", inner_text="原推完整正文"):
        return {
            "rest_id": "1",
            "core": {"user_results": {"result": {"core": {"screen_name": "AnthropicAI"}}}},
            "legacy": {
                "id_str": "1",
                "full_text": f"RT @{inner_handle}: {inner_text[:20]}…",
                "retweeted_status_result": {"result": {
                    "legacy": {"id_str": "999", "full_text": inner_text},
                    "core": {"user_results": {"result": {"core": {"screen_name": inner_handle}}}},
                }},
            },
        }

    def test_original_author_is_captured(self):
        from sourcepilot.channels.x.tweet import from_graphql

        r = from_graphql(self._rt(), NOW, NOW)
        assert r.is_retweet is True
        assert r.author_handle == "AnthropicAI", "外层作者仍是转发者"
        assert r.retweeted_handle == "claudeai", "原作者必须留下"
        assert r.retweeted_text == "原推完整正文"

    def test_plain_tweet_is_not_a_retweet(self):
        from sourcepilot.channels.x.tweet import from_graphql

        r = from_graphql({
            "rest_id": "1",
            "core": {"user_results": {"result": {"core": {"screen_name": "a"}}}},
            "legacy": {"id_str": "1", "full_text": "普通推文"},
        }, NOW, NOW)
        assert r.is_retweet is False and r.retweeted_handle is None

    def test_display_text_shows_the_original(self):
        """外层的 `RT @某某: …` 是截断版，按它渲染会把完整推文显示成半句话。"""
        from sourcepilot.channels.x.tweet import display_fields

        _, body = display_fields({
            "text": "RT @claudeai: Introducing Claude…",
            "is_retweet": True,
            "retweeted_text": "Introducing Claude Opus 5. 完整的原文内容",
        })
        assert body == "Introducing Claude Opus 5. 完整的原文内容"


class TestTweetTypeIsSeparateFromContentKind:
    """两个维度：tweet_type 是 X 的客观关系，content_kind 是展示形态。"""

    def test_four_relations(self):
        from sourcepilot.channels.x.tweet import tweet_type

        assert tweet_type({"is_retweet": True, "is_quote": True}) == "repost"
        assert tweet_type({"is_quote": True, "is_reply": True}) == "quote"
        assert tweet_type({"is_reply": True}) == "reply"
        assert tweet_type({}) == "original"

    def test_repost_wins_in_content_kind_too(self):
        """转发的外层没有自己的内容，这个事实盖过长文/长推文等其它判据。"""
        from sourcepilot.channels.x.tweet import classify

        assert classify({"is_retweet": True, "has_article": True, "text": "x" * 500}) == "repost"


class TestTypeFilterIsPushedToSql:
    """按关系过滤必须下推到 SQL，不能取出来再筛。

    这个 bug 真实发生过：端点先取 limit*5 条再在应用层过滤，而转发在这个
    账号里全集中在较早的时间段——最新 20 条里一条都没有，接口返回空列表，
    但库里明明有 4 条。占比低的类别多取多少倍都可能漏。
    """

    def _add(self, store, tid, *, retweet=False, quote=False, reply=False, minute=0):
        from datetime import timedelta

        from sourcepilot.channels.x.tweet import TweetRecord

        store.upsert_tweets([TweetRecord(
            tid, "a", "内容", NOW, created_at=NOW + timedelta(minutes=minute),
            is_retweet=retweet, is_quote=quote, is_reply=reply,
            retweeted_handle="orig" if retweet else None)])

    def test_finds_old_reposts_beyond_the_first_page(self, store):
        # 转发很老，前面压着一堆新的原创
        self._add(store, "old_rt", retweet=True, minute=0)
        for i in range(30):
            self._add(store, f"new_{i}", minute=10 + i)
        found = store.query_tweets(tweet_types={"repost"}, limit=5)
        assert [t["tweet_id"] for t in found] == ["old_rt"]

    def test_each_relation_is_exclusive(self, store):
        self._add(store, "rt", retweet=True)
        self._add(store, "q", quote=True)
        self._add(store, "r", reply=True)
        self._add(store, "o")
        for kind, tid in (("repost", "rt"), ("quote", "q"), ("reply", "r"), ("original", "o")):
            got = store.query_tweets(tweet_types={kind}, limit=10)
            assert [t["tweet_id"] for t in got] == [tid], f"{kind} 应只匹配 {tid}"

    def test_multiple_types(self, store):
        self._add(store, "rt", retweet=True)
        self._add(store, "o")
        got = store.query_tweets(tweet_types={"repost", "original"}, limit=10)
        assert len(got) == 2
