"""已知容器的 HTML → Markdown。

trafilatura 的强项是「猜正文在哪」，代价是激进降噪——实测公众号文章正文
提得很干净，但 6 张配图和 4 个小标题全被当噪音丢了。容器已知时就不必
承担猜错的代价，这里按标签逐个翻译。
"""

from __future__ import annotations

from sourcepilot.htmlmd import extract

WRAP = '<html><body><div id="c">{}</div></body></html>'


def md(inner: str) -> str:
    return extract(WRAP.format(inner), "#c") or ""


class TestLazyLoadedImages:
    """懒加载站点把真实地址放在 data-src，src 缺失或是占位图。

    公众号 6 张配图全部只有 data-src——只认 src 的话一张都拿不到。
    """

    def test_data_src_is_used(self):
        assert md('<p><img data-src="https://i/1.jpg"></p>') == "![](https://i/1.jpg)"

    def test_real_src_still_works(self):
        assert md('<p><img src="https://i/2.jpg"></p>') == "![](https://i/2.jpg)"

    def test_inline_placeholder_is_skipped(self):
        """data: 开头的是内联占位图，不是内容。"""
        assert md('<p><img src="data:image/gif;base64,R0lGOD"></p>') == ""

    def test_image_without_any_source_is_dropped(self):
        """宁可少一张图，不要一个坏链接。"""
        assert md("<p>正文<img></p>") == "正文"


class TestHeadings:
    def test_levels(self):
        assert md("<h1>一</h1><h2>二</h2><h3>三</h3>") == "# 一\n\n## 二\n\n### 三"

    def test_heading_wrapped_in_span_keeps_its_prefix(self):
        """站点常把标题文字再包一层 span 做样式（公众号的 4 个 h2 全是这样）。

        当成「纯容器」下钻的话文字出来了、`##` 前缀丢在半路，
        结果整篇正文一个小标题都没有。
        """
        assert md("<h2><span>小标题</span></h2>") == "## 小标题"


class TestImagesAreBlockLevel:
    def test_image_is_lifted_out_of_its_wrapper(self):
        """公众号用 `<h6><strong><img>△说明</strong></h6>` 排版图注。

        不把图摘出来的话会产出 `###### **![](url)△**说明`
        ——既是标题又是强调的畸形串。
        """
        out = md('<h6><strong><img data-src="https://i/1.jpg">△说明</strong></h6>')
        assert out.startswith("![](https://i/1.jpg)")
        assert "![](https://i/1.jpg)△" not in out


class TestInlineMarks:
    def test_bold_and_italic(self):
        assert md("<p><strong>粗</strong>和<em>斜</em></p>") == "**粗**和*斜*"

    def test_marks_hug_the_text(self):
        """`** x **` 不是合法的 Markdown 强调，渲染器会原样吐出星号。"""
        assert md("<p><strong> 粗 </strong></p>").strip() == "**粗**"

    def test_link(self):
        assert md('<p><a href="https://a.com">锚</a></p>') == "[锚](https://a.com)"

    def test_anchor_and_js_links_degrade_to_text(self):
        assert md('<p><a href="#top">回顶</a></p>') == "回顶"
        assert md('<p><a href="javascript:void(0)">点</a></p>') == "点"


class TestStructure:
    def test_lists(self):
        assert md("<ul><li>甲</li><li>乙</li></ul>") == "- 甲\n\n- 乙"
        assert md("<ol><li>甲</li><li>乙</li></ol>") == "1. 甲\n\n2. 乙"

    def test_nested_containers_do_not_duplicate_text(self):
        """纯容器要下钻，但下钻不能让同一段文字在每层都输出一次。"""
        assert md("<div><div><div><p>只出现一次</p></div></div></div>") == "只出现一次"

    def test_script_and_style_are_dropped(self):
        assert md("<p>正文</p><script>alert(1)</script><style>a{}</style>") == "正文"


class TestFallback:
    def test_missing_container_returns_none(self):
        """站点改版把容器改名了——返回 None 让调用方回落到通用提取。"""
        assert extract("<html><body><p>x</p></body></html>", "#nope") is None

    def test_empty_container_returns_none(self):
        assert extract('<div id="c"><script>x</script></div>', "#c") is None

    def test_nested_list_is_indented(self):
        out = md("<ul><li>甲<ul><li>甲一</li></ul></li></ul>")
        assert out == "- 甲\n\n  - 甲一"
