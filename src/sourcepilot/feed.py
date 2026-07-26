"""RSS 出口。第三个协议壳，与 `api.py` / `mcp_server.py` 平级。

**为什么要有它**：REST 给程序、MCP 给 AI 客户端、SKILL.md 给 Agent——三者都要求
消费方「会写代码或会配置」。RSS 的消费者是**现成的阅读器**（Reeder、Feedly、
Inoreader）和自动化工具（n8n、Zapier），订阅一个 URL 就能用，零代码。
对标平台把它列为三条接入路径之一，不是可有可无的附属品。

**只出摘要，不内联正文**。这不只是因为库里本来就没存正文，也是边界：
RSS 是公开阅读面，不代表第三方内容因此获得了再分发许可。每条都保留
原文链接与来源署名，让读者落到原站去读。将来若要做全文版，也只能对
**明确允许再分发的源**开放，而不是一刀切内联。

查询参数与 `/api/v1/items` 完全一致——同一套过滤能力，换个输出格式而已，
所以这里没有任何业务判断，只做协议翻译。
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from .contracts import Item

#: RSS 2.0 要求 pubDate 是 RFC 822 格式，不是 ISO8601。
#: 阅读器按它排序与去重，格式错了会被当成「没有日期」。


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt.astimezone(UTC))


def _describe(item: Item) -> str:
    """条目描述：摘要 + 来源署名 + 时间依据说明。

    时间那句是必要的——`time_basis=discovered` 意味着我们只知道「什么时候收录的」，
    在阅读器里如果不说明，读者会把它当成发布时间。
    """
    parts: list[str] = []
    if item.summary:
        parts.append(item.summary)

    origin = item.source.name
    if item.author:
        origin += f" · {item.author}"
    parts.append(f"来源：{origin}")

    if item.time_basis.value == "discovered":
        parts.append("（该源未提供发布时间，此处为本平台收录时间）")

    return "\n\n".join(parts)


def render_feed(
    items: list[Item],
    *,
    title: str = "SourcePilot",
    description: str = "多信源 AI 资讯聚合",
    link: str = "https://github.com/suversal/sourcepilot",
    self_url: str | None = None,
    now: datetime | None = None,
) -> str:
    """把 Item 列表渲染成 RSS 2.0。

    用 ElementTree 而不是拼字符串——标题与摘要来自第三方，里面什么字符都可能有，
    手拼必然在某天遇到未转义的 `&` 或 `<` 而产出坏 XML。
    """
    now = now or datetime.now(UTC)

    rss = Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = title
    SubElement(channel, "link").text = link
    SubElement(channel, "description").text = description
    SubElement(channel, "language").text = "zh-CN"
    SubElement(channel, "lastBuildDate").text = _rfc822(now)
    SubElement(channel, "generator").text = "SourcePilot"
    if self_url:
        # 阅读器靠它识别订阅源本身的地址，换域名时不至于当成新源重新全量拉。
        SubElement(
            channel,
            "atom:link",
            {"href": self_url, "rel": "self", "type": "application/rss+xml"},
        )

    for item in items:
        entry = SubElement(channel, "item")
        SubElement(entry, "title").text = item.title
        SubElement(entry, "link").text = str(item.url)
        SubElement(entry, "description").text = _describe(item)
        # guid 用平台内部 id 而不是 url：同一条内容的 url 可能因规范化而变化，
        # id 是稳定的。isPermaLink=false 告诉阅读器别把它当地址访问。
        SubElement(entry, "guid", {"isPermaLink": "false"}).text = item.id
        SubElement(entry, "pubDate").text = _rfc822(item.effective_time)
        SubElement(entry, "source", {"url": link}).text = item.source.name
        for category in item.categories:
            SubElement(entry, "category").text = category.value
        for media in item.media:
            # enclosure 让阅读器能显示配图
            SubElement(
                entry,
                "enclosure",
                {"url": str(media.url), "type": f"{media.type.value}/*", "length": "0"},
            )

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(rss, encoding="unicode")
