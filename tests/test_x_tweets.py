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
