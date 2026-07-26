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


class TestStrptime:
    """网页上的日期是给人看的格式（Jul 9, 2026），得声明式地告诉引擎怎么读。"""

    PAGE = """
    <div class="list">
      <a href="/news/a"><span class="title">标题甲</span><time>Jul 9, 2026</time></a>
      <a href="/news/b"><span class="title">标题乙</span><time>看不懂的日期</time></a>
    </div>
    """
    CONFIG = {
        "name": "faketime",
        "display_name": "日期测试源",
        "base_url": "https://example.com",
        "request": {"url": "https://example.com/n"},
        "extract": {
            "format": "html",
            "list": ".list a",
            "fields": {
                "native_id": {"select": ".", "attr": "href"},
                "title": {"select": ".title"},
                "url": {"select": ".", "attr": "href"},
                "published_at": {
                    "select": "time",
                    "type": "strptime",
                    "format": "%b %d, %Y",
                },
            },
        },
    }

    def test_human_readable_date_parsed(self):
        items = normalize(SourceConfig(**self.CONFIG), self.PAGE)
        assert items[0].published_at.date().isoformat() == "2026-07-09"
        assert items[0].time_basis is TimeBasis.PUBLISHED

    def test_unparseable_date_falls_back_not_crashes(self):
        """解析不了就当没有发布时间，而不是让整条数据消失。"""
        items = normalize(SourceConfig(**self.CONFIG), self.PAGE)
        assert items[1].published_at is None
        assert items[1].time_basis is TimeBasis.DISCOVERED

    def test_strptime_requires_format(self):
        bad = {**self.CONFIG}
        bad["extract"] = {
            **bad["extract"],
            "fields": {
                **bad["extract"]["fields"],
                "published_at": {"select": "time", "type": "strptime"},
            },
        }
        with pytest.raises(ValidationError, match="strptime 与 format 必须成对"):
            SourceConfig(**bad)


class TestSlug:
    """有些站点的文章卡片不是链接，地址得从标题推出来（字节 Seed 就是）。"""

    def test_slugify_basic(self):
        from sourcepilot.sources.extract import slugify

        assert slugify("Seed2.1 Officially Released: Advancing AI") == (
            "seed2-1-officially-released-advancing-ai"
        )

    def test_slugify_collapses_separators(self):
        from sourcepilot.sources.extract import slugify

        assert slugify("A | B — C") == "a-b-c"

    def test_slugify_strips_edges(self):
        from sourcepilot.sources.extract import slugify

        assert slugify("  (Hello!)  ") == "hello"

    def test_template_can_chain_off_slug(self):
        """native_id 由标题 slug 化，url 再引用 native_id——模板按声明顺序求值。"""
        config = SourceConfig(
            **{
                "name": "fakeslug",
                "display_name": "slug 测试源",
                "base_url": "https://example.com",
                "request": {"url": "https://example.com/blog"},
                "extract": {
                    "format": "html",
                    "list": "div.card",
                    "fields": {
                        "title": {"select": "img", "attr": "alt"},
                        "native_id": {"template": "{title}", "type": "slug"},
                        "url": {"template": "{base_url}/p/{native_id}"},
                    },
                },
            }
        )
        page = '<div class="card"><img alt="Hello World: Part 2"/></div>'
        (item,) = normalize(config, page)
        assert item.id == "hotlist:fakeslug_hello-world-part-2"
        assert str(item.url) == "https://example.com/p/hello-world-part-2"


class TestPatternExtraction:
    """想要的东西埋在更长的字符串里时，先按正则抽一段再转类型。"""

    CONFIG = {
        "name": "fakepat",
        "display_name": "pattern 测试源",
        "base_url": "https://example.com",
        "request": {"url": "https://example.com/n"},
        "extract": {
            "format": "html",
            "list": "div.u",
            "fields": {
                "native_id": {"select": ".", "attr": "id"},
                "title": {"select": ".t"},
                "url": {"template": "{base_url}/#{native_id}"},
                # 同一天多条公告时 id 是 2025-12-11-3，日期只是前一段
                "published_at": {
                    "select": ".",
                    "attr": "id",
                    "pattern": r"^(\d{4}-\d{2}-\d{2})",
                    "type": "iso",
                },
            },
        },
    }

    PAGE = (
        '<div class="u" id="2025-12-11-3"><span class="t">同日第三条</span></div>'
        '<div class="u" id="2026-06-16"><span class="t">当日唯一一条</span></div>'
    )

    def test_date_extracted_from_suffixed_id(self):
        """不抽的话解析失败 → published_at 为空 → 旧公告会混进近期窗口。"""
        items = normalize(SourceConfig(**self.CONFIG), self.PAGE)
        assert items[0].published_at.date().isoformat() == "2025-12-11"
        assert items[0].time_basis is TimeBasis.PUBLISHED

    def test_plain_id_still_works(self):
        items = normalize(SourceConfig(**self.CONFIG), self.PAGE)
        assert items[1].published_at.date().isoformat() == "2026-06-16"

    def test_no_match_yields_none_not_garbage(self):
        """抽不到就当没有——宁可缺字段也不要错字段。"""
        from sourcepilot.sources.extract import apply_pattern

        assert apply_pattern("没有日期", r"^(\d{4}-\d{2}-\d{2})") is None


class TestSummaryClipping:
    """RSS 的 description 常常直接放全文——实测 BAIR 17873 字、VentureBeat 13176 字。

    契约 §2 说 summary 是「客观摘要，抽取式」。原样收下会让 /items?limit=50
    的响应涨到几百 KB，而下游真要全文该走 read_article。
    """

    def test_short_summary_is_untouched(self):
        from sourcepilot.sources.engine import clip_summary

        assert clip_summary("一句短摘要") == "一句短摘要"

    def test_long_summary_is_clipped(self):
        from sourcepilot.sources.engine import SUMMARY_MAX_CHARS, clip_summary

        assert len(clip_summary("啊" * 5000)) <= SUMMARY_MAX_CHARS + 1

    def test_prefers_a_sentence_boundary(self):
        """切在半句话中间读起来是坏的。"""
        from sourcepilot.sources.engine import clip_summary

        text = "第一句。" + "填" * 500 + "。" + "尾巴" * 200
        assert clip_summary(text).endswith("。")

    def test_falls_back_to_a_hard_cut(self):
        """整段没有一个句号时不能因此放弃截断。"""
        from sourcepilot.sources.engine import SUMMARY_MAX_CHARS, clip_summary

        out = clip_summary("无标点" * 1000)
        assert out.endswith("…") and len(out) <= SUMMARY_MAX_CHARS + 1

    def test_empty_stays_none(self):
        from sourcepilot.sources.engine import clip_summary

        assert clip_summary(None) is None
        assert clip_summary("") is None


class TestRssGuidNormalisation:
    """很多 RSS 直接拿链接当 guid，那串链接常带入口标记。

    36氪的 feed 给每条挂 `?f=rss`。不规范化的话，同一篇文章从 RSS 和从
    列表页进来会得到两个不同的 id，在信息流里变成两条。
    """

    CONFIG = {
        "name": "guidsrc",
        "display_name": "拿链接当 guid 的源",
        "platform": "guidsrc",
        "request": {"url": "https://example.com/feed"},
        "extract": {"format": "rss"},
    }

    FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>某条</title><link>https://example.com/p/1?f=rss</link>
    <guid>https://example.com/p/1?f=rss</guid></item></channel></rss>"""

    def test_tracking_param_is_stripped_from_the_id(self):
        from sourcepilot.sources import SourceConfig, engine

        (item,) = engine.normalize(SourceConfig(**self.CONFIG), self.FEED)
        assert "?f=rss" not in item.id
        assert item.id.endswith("https://example.com/p/1")
