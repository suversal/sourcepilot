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

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape
from xml.etree.ElementTree import Element, SubElement, tostring

from .contracts import Item

#: RSS 2.0 要求 pubDate 是 RFC 822 格式，不是 ISO8601。
#: 阅读器按它排序与去重，格式错了会被当成「没有日期」。

#: CDATA 的占位标记。ElementTree 会把 `<![CDATA[` 转义掉，所以先用不可能出现在
#: 正文里的哨兵占位，序列化之后再换回来。
_CDATA_OPEN = "\x00CDATA_OPEN\x00"
_CDATA_CLOSE = "\x00CDATA_CLOSE\x00"
_CDATA_RE = re.compile(re.escape(_CDATA_OPEN) + "(.*?)" + re.escape(_CDATA_CLOSE), re.DOTALL)

#: 告诉阅读器多少分钟内不必重复拉取。信源本身的更新间隔从 5 分钟到 1 小时不等，
#: 30 分钟是个不会漏太多、又能明显减轻服务压力的折中。
DEFAULT_TTL_MINUTES = 30


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt.astimezone(UTC))


def _describe(item: Item) -> str:
    """条目描述，输出 HTML（外层由 CDATA 包）。

    阅读器会渲染这段 HTML，所以分段和链接都有意义——纯文本在阅读器里会挤成一坨。

    末尾那句时间说明是必要的：`time_basis=discovered` 意味着我们只知道「什么时候
    收录的」，不说明的话读者会把 pubDate 当成发布时间。
    """
    parts: list[str] = []
    if item.summary:
        parts.append(f"<p>{escape(item.summary)}</p>")

    # 原文入口做成可点链接。我们的 <link> 已经指向原文，但阅读器里
    # 正文区放一个显式入口更好用（尤其是摘要为空、只有标题的条目）。
    parts.append(f'<p>🔗 <a href="{escape(str(item.url))}">阅读原文</a></p>')

    origin = escape(item.source.name)
    if item.time_basis.value == "discovered":
        origin += "（该源未提供发布时间，下方时间为本平台收录时间）"
    parts.append(f"<p>来源：{origin}</p>")

    return "\n".join(parts)


def _cdata(parent: Element, tag: str, text: str) -> None:
    """写一个 CDATA 包裹的元素。

    ElementTree 不支持 CDATA，所以先塞哨兵占位，序列化之后再换成真的 CDATA
    标记——比手拼整个 XML 安全得多，其余元素仍然享受它的自动转义。

    `]]>` 是 CDATA 的结束标记，正文里出现它就会提前闭合并让后面的内容
    变成裸 XML。标准做法是把它拆成两段 CDATA。
    """
    text = text.replace("]]>", "]]" + _CDATA_CLOSE + _CDATA_OPEN + ">")
    SubElement(parent, tag).text = f"{_CDATA_OPEN}{text}{_CDATA_CLOSE}"


def _restore_cdata(xml: str) -> str:
    """把哨兵换回真 CDATA，并撤销 ElementTree 在区域内做的那层转义。

    ElementTree 把整段当普通文本，会把里面的 `<p>` 转义成 `&lt;p&gt;`；
    但 CDATA 区域内本就不需要转义，留着的话阅读器显示的是实体字面量。
    这里按 ET 转义的**精确逆序**还原（`&amp;` 必须最后），所以正文里原本
    就存在的实体字面量不会被多剥一层。
    """

    def unescape(match: re.Match[str]) -> str:
        body = match.group(1)
        for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
            body = body.replace(entity, char)
        return f"<![CDATA[{body}]]>"

    return _CDATA_RE.sub(unescape, xml)


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
    SubElement(channel, "ttl").text = str(DEFAULT_TTL_MINUTES)
    SubElement(channel, "generator").text = "SourcePilot (https://github.com/suversal/sourcepilot)"
    if self_url:
        # 阅读器靠它识别订阅源本身的地址，换域名时不至于当成新源重新全量拉。
        SubElement(
            channel,
            "atom:link",
            {"href": self_url, "rel": "self", "type": "application/rss+xml"},
        )

    for item in items:
        entry = SubElement(channel, "item")
        # 标题走普通转义而**不是** CDATA：CDATA 在 RSS 里意味着「这段是 HTML」，
        # 而标题是纯文本。第三方标题里出现 `<script>` 或 `&` 时，包成 CDATA
        # 会让解析器按 HTML 处理，纯文本消费方拿到的是一串实体。
        SubElement(entry, "title").text = item.title
        # link 指向**第三方原文**。对标平台这里指向自己的站内阅读页以留住流量，
        # 我们不做展示层（职责边界），所以直接给原文。
        SubElement(entry, "link").text = str(item.url)
        _cdata(entry, "description", _describe(item))
        # guid 用平台内部 id 而不是 url：同一条内容的 url 可能因规范化而变化，
        # id 是稳定的。isPermaLink=false 告诉阅读器别把它当地址访问。
        SubElement(entry, "guid", {"isPermaLink": "false"}).text = item.id
        SubElement(entry, "pubDate").text = _rfc822(item.effective_time)
        if item.author:
            # 单独成元素而不是塞进描述——阅读器能按作者分组和过滤。
            # RSS 2.0 的 author 规定是邮箱格式，没有邮箱时用括号注名是通行做法。
            SubElement(entry, "author").text = f"noreply@sourcepilot.local ({item.author})"
        for category in item.categories:
            SubElement(entry, "category").text = category.value
        for media in item.media:
            # enclosure 让阅读器能显示配图
            SubElement(
                entry,
                "enclosure",
                {"url": str(media.url), "type": f"{media.type.value}/*", "length": "0"},
            )

    xml = _restore_cdata(tostring(rss, encoding="unicode"))
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml
