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
