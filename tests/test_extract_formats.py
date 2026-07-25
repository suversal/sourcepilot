"""HTML 与 RSS 提取器测试。用固定样本，不联网。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sourcepilot.contracts import TimeBasis, UpstreamDown
from sourcepilot.sources import SourceConfig, normalize

HTML_PAGE = """
<html><body>
  <div id="list"><ul>
    <li><a class="t" href="/p/1.htm">某公司发布新模型</a><i>1小时前</i></li>
    <li><a class="t" href="/p/2.htm">京东补贴优惠券速领</a><i>2小时前</i></li>
    <li><a class="t" href="https://other.com/p/3">绝对链接的一条</a><i>3小时前</i></li>
    <li><span>没有链接的一行</span></li>
  </ul></div>
</body></html>
"""

HTML_CONFIG = {
    "name": "fakehtml",
    "display_name": "HTML 测试源",
    "platform": "fakehtml",
    "base_url": "https://example.com",
    "request": {"url": "https://example.com/list"},
    "extract": {
        "format": "html",
        "list": "#list li",
        "fields": {
            "native_id": {"select": "a.t", "attr": "href"},
            "title": {"select": "a.t"},
            "url": {"select": "a.t", "attr": "href"},
            "summary": {"select": "i"},
        },
        "exclude_if": {"title": ["优惠", "补贴"]},
    },
}

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>示例源</title>
  <item>
    <title>第一条</title>
    <link>https://example.com/a</link>
    <guid>tag:a</guid>
    <description>&lt;p&gt;带 &lt;b&gt;HTML&lt;/b&gt; 的摘要&lt;/p&gt;</description>
    <author>作者甲</author>
    <pubDate>Fri, 24 Jul 2026 16:13:35 +0000</pubDate>
  </item>
  <item>
    <title>第二条</title>
    <link>https://example.com/b</link>
    <guid>tag:b</guid>
  </item>
</channel></rss>
"""

RSS_CONFIG = {
    "name": "fakerss",
    "display_name": "RSS 测试源",
    "platform": "fakerss",
    "request": {"url": "https://example.com/feed"},
    "extract": {"format": "rss"},
}


@pytest.fixture
def html_config() -> SourceConfig:
    return SourceConfig(**HTML_CONFIG)


@pytest.fixture
def rss_config() -> SourceConfig:
    return SourceConfig(**RSS_CONFIG)


class TestHtml:
    def test_extracts_rows(self, html_config):
        items = normalize(html_config, HTML_PAGE)
        assert [i.title for i in items] == ["某公司发布新模型", "绝对链接的一条"]

    def test_relative_url_joined_with_base(self, html_config):
        assert str(normalize(html_config, HTML_PAGE)[0].url) == "https://example.com/p/1.htm"

    def test_absolute_url_left_alone(self, html_config):
        assert str(normalize(html_config, HTML_PAGE)[1].url) == "https://other.com/p/3"

    def test_exclude_if_drops_ads(self, html_config):
        """IT之家那类列表里混着推广位，靠关键词剔掉。"""
        assert all("优惠" not in i.title for i in normalize(html_config, HTML_PAGE))

    def test_row_without_required_field_skipped(self, html_config):
        assert len(normalize(html_config, HTML_PAGE)) == 2

    def test_text_is_whitespace_collapsed(self, html_config):
        assert normalize(html_config, HTML_PAGE)[0].summary == "1小时前"

    def test_selector_matching_nothing_raises(self, html_config):
        """对方改版把结构换了——要报出来，不是静悄悄返回空。"""
        with pytest.raises(UpstreamDown, match="一个元素都没选中"):
            normalize(html_config, "<html><body><p>换版了</p></body></html>")

    def test_html_config_rejects_path_fields(self):
        bad = {**HTML_CONFIG}
        bad["extract"] = {**bad["extract"], "fields": {**bad["extract"]["fields"], "title": "t"}}
        with pytest.raises(ValidationError, match="要用 select 而非 path"):
            SourceConfig(**bad)

    def test_html_config_requires_list_selector(self):
        bad = {**HTML_CONFIG}
        bad["extract"] = {k: v for k, v in bad["extract"].items() if k != "list"}
        with pytest.raises(ValidationError, match="必须给 extract.list"):
            SourceConfig(**bad)


class TestRss:
    def test_default_field_mapping_needs_no_config(self, rss_config):
        items = normalize(rss_config, RSS_FEED)
        assert [i.title for i in items] == ["第一条", "第二条"]

    def test_summary_html_is_stripped(self, rss_config):
        assert normalize(rss_config, RSS_FEED)[0].summary == "带 HTML 的摘要"

    def test_pubdate_parsed_to_utc(self, rss_config):
        first = normalize(rss_config, RSS_FEED)[0]
        assert first.time_basis is TimeBasis.PUBLISHED
        assert first.published_at.isoformat() == "2026-07-24T16:13:35+00:00"

    def test_entry_without_pubdate_falls_back_to_discovered(self, rss_config):
        second = normalize(rss_config, RSS_FEED)[1]
        assert second.published_at is None
        assert second.time_basis is TimeBasis.DISCOVERED

    def test_author_mapped(self, rss_config):
        assert normalize(rss_config, RSS_FEED)[0].author == "作者甲"

    def test_garbage_feed_raises(self, rss_config):
        with pytest.raises(UpstreamDown, match="RSS 解析失败"):
            normalize(rss_config, "{'这不是':'xml'}")


class TestImpersonate:
    def test_config_accepts_impersonate(self):
        cfg = SourceConfig(
            **{**RSS_CONFIG, "request": {"url": "https://x.test/f", "impersonate": "safari"}}
        )
        assert cfg.request.impersonate == "safari"

    def test_absent_by_default(self, rss_config):
        assert rss_config.request.impersonate is None
