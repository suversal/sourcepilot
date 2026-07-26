"""RSS 出口测试。

这一层没有业务逻辑（都在服务层），所以测的是两件事：
**生成的 XML 阅读器真能读**，以及**不越过内容边界**（只出摘要、保留原文链接与署名）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import feedparser
import pytest
from conftest import FAKE_CONFIG_DICT, FAKE_PAYLOAD
from fastapi.testclient import TestClient

from sourcepilot.api import create_app
from sourcepilot.contracts import Category, Item, Media, MediaType, Source, SourceType, TimeBasis
from sourcepilot.feed import render_feed
from sourcepilot.sources import SourceConfig, engine

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def make_item(**over) -> Item:
    base = {
        "id": "hotlist:fake_1",
        "source": Source(type=SourceType.HOTLIST, name="测试源", platform="fake"),
        "title": "某模型发布",
        "summary": "一句话摘要",
        "url": "https://example.com/a",
        "author": "作者甲",
        "published_at": NOW,
        "discovered_at": NOW,
        "time_basis": TimeBasis.PUBLISHED,
        "score": 0.5,
    }
    return Item(**{**base, **over})


class TestRenderedXml:
    def _parse(self, items, **kw):
        return feedparser.parse(render_feed(items, **kw))

    def test_is_valid_rss20(self):
        """bozo=False 意味着标准解析器没报错——阅读器用的就是同一套。"""
        feed = self._parse([make_item()])
        assert feed.bozo is False
        assert feed.version == "rss20"

    def test_special_characters_do_not_break_xml(self):
        """标题来自第三方，什么字符都可能有。手拼字符串迟早在这里炸。"""
        nasty = make_item(title="A & B < C > D \"引号\" '撇号' <script>")
        feed = self._parse([nasty])
        assert feed.bozo is False
        assert feed.entries[0].title == "A & B < C > D \"引号\" '撇号' <script>"

    def test_pubdate_is_rfc822_not_iso(self):
        """RSS 2.0 要 RFC 822。给 ISO8601 的话阅读器会当成「没有日期」。"""
        feed = self._parse([make_item()])
        assert feed.entries[0].published_parsed is not None

    def test_guid_is_stable_id_not_url(self):
        """url 可能因规范化而变，id 是稳定的——阅读器靠 guid 判重。"""
        feed = self._parse([make_item()])
        assert feed.entries[0].id == "hotlist:fake_1"

    def test_empty_feed_is_still_valid(self):
        feed = self._parse([])
        assert feed.bozo is False and feed.entries == []

    def test_title_is_escaped_not_cdata(self):
        """CDATA 在 RSS 里意味着「这是 HTML」，而标题是纯文本。

        包成 CDATA 的话，含 `<script>` 的第三方标题会被按 HTML 解析，
        纯文本消费方拿到的是一串实体而不是原文。
        """
        xml = render_feed([make_item(title="A & B <script>")])
        assert "<title>A &amp; B &lt;script&gt;</title>" in xml

    def test_description_is_cdata_html(self):
        """描述**是** HTML——阅读器要渲染它，纯文本会挤成一坨。"""
        xml = render_feed([make_item()])
        assert "<description><![CDATA[" in xml

    def test_cdata_html_is_not_double_escaped(self):
        """ET 会把整段当普通文本转义，不还原的话阅读器显示的是 `&lt;p&gt;` 字面量。"""
        xml = render_feed([make_item()])
        assert "<p>" in xml and "&lt;p&gt;" not in xml

    def test_cdata_terminator_in_content_cannot_break_out(self):
        """`]]>` 出现在第三方摘要里就能提前闭合 CDATA，让后面的内容变成裸 XML。"""
        feed = self._parse([make_item(summary="危险 ]]><script>alert(1)</script>")])
        assert feed.bozo is False
        assert len(feed.entries) == 1

    def test_ttl_tells_readers_not_to_hammer(self):
        feed = self._parse([make_item()])
        assert feed.feed.ttl == "30"


class TestContentBoundary:
    """RSS 是公开阅读面，不代表第三方内容因此获得再分发许可。"""

    def test_only_summary_never_full_text(self):
        """条目里给的是摘要，读者要读全文得落到原站。"""
        item = make_item(summary="这是摘要")
        feed = feedparser.parse(render_feed([item]))
        assert "这是摘要" in feed.entries[0].description

    def test_original_link_is_preserved(self):
        feed = feedparser.parse(render_feed([make_item()]))
        assert feed.entries[0].link == "https://example.com/a"

    def test_source_is_attributed(self):
        """署名必须留——镜像或再分发时读者要知道内容来自哪。"""
        feed = feedparser.parse(render_feed([make_item()]))
        assert "测试源" in feed.entries[0].description

    def test_original_link_also_appears_in_body(self):
        """摘要为空的条目在阅读器里只剩标题，正文区得有个可点的原文入口。"""
        feed = feedparser.parse(render_feed([make_item(summary=None)]))
        assert "阅读原文" in feed.entries[0].description
        assert "https://example.com/a" in feed.entries[0].description

    def test_discovered_time_is_disclosed_as_such(self):
        """time_basis=discovered 时不说明的话，阅读器里会被当成发布时间。"""
        item = make_item(published_at=None, time_basis=TimeBasis.DISCOVERED)
        feed = feedparser.parse(render_feed([item]))
        assert "收录时间" in feed.entries[0].description

    def test_published_time_needs_no_disclaimer(self):
        feed = feedparser.parse(render_feed([make_item()]))
        assert "收录时间" not in feed.entries[0].description


class TestOptionalFields:
    def test_categories_become_rss_categories(self):
        item = make_item(categories=[Category.MODEL])
        feed = feedparser.parse(render_feed([item]))
        assert "model" in [t["term"] for t in feed.entries[0].tags]

    def test_media_becomes_enclosure(self):
        item = make_item(
            media=[Media(type=MediaType.IMAGE, url="https://example.com/a.jpg")]
        )
        feed = feedparser.parse(render_feed([item]))
        assert feed.entries[0].enclosures[0]["href"] == "https://example.com/a.jpg"

    def test_author_is_its_own_element(self):
        """塞进描述文字里的话，阅读器没法按作者分组或过滤。"""
        feed = feedparser.parse(render_feed([make_item()]))
        assert "作者甲" in feed.entries[0].author

    def test_author_is_omitted_when_unknown(self):
        """宁可没有，也不要伪造一个「未知作者」占位。"""
        xml = render_feed([make_item(author=None)])
        assert "<author>" not in xml

    def test_self_link_helps_readers_track_the_source(self):
        feed = feedparser.parse(
            render_feed([make_item()], self_url="https://host/api/v1/feed.xml?x=1")
        )
        assert any(link.get("rel") == "self" for link in feed.feed.links)


class TestEndpoint:
    @pytest.fixture
    def client(self, store):
        sources = {"fake": SourceConfig(**FAKE_CONFIG_DICT)}
        return TestClient(create_app(store=store, sources=sources, scheduler=False))

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr(engine, "fetch_raw", lambda config, client=None, *a, **kw: FAKE_PAYLOAD)

    def test_content_type_is_rss(self, client):
        r = client.get("/api/v1/feed.xml")
        assert r.status_code == 200
        assert "application/rss+xml" in r.headers["content-type"]

    def test_same_filters_as_items_endpoint(self, client):
        """查询参数与 /items 完全一致——同一套过滤能力，换个输出格式。"""
        client.get("/api/v1/hotlist")
        feed = feedparser.parse(client.get("/api/v1/feed.xml?platform=fake&window=30d").text)
        assert len(feed.entries) > 0

    def test_title_reflects_the_filter(self, client):
        """阅读器里并排十几个源，全叫 SourcePilot 的话分不出谁是谁。"""
        feed = feedparser.parse(client.get("/api/v1/feed.xml?q=模型&window=7d").text)
        assert "模型" in feed.feed.title

    def test_bad_platform_still_reports_the_error(self, client):
        """过滤条件写错时不能默默返回空订阅源。"""
        r = client.get("/api/v1/feed.xml?platform=不存在的源")
        assert r.status_code == 400
