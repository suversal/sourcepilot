"""X 长文（X Articles）正文解析。

X 的长文不是推文——推文里只挂一个 article 实体，正文在 `content_state` 里，
格式是 Draft.js 的 `{blocks, entityMap}`。搜索与时间线接口返回的 article
**只有 `preview_text`**（约 100 字预览），正文必须单独请求并打开
`withArticleRichContentState` 才拿得到。

**为什么转 Markdown 而不是直接用 `plain_text`**：X 同时给了纯文本版，但那一版
把二级标题和正文段落拍平成同样的行，链接也只剩锚文本——下游拿到的是一堆看不出
结构的段落。而 Draft.js 里标题、链接、加粗都在，转成 Markdown 后展示端能直接渲染。
块级结构走 `_BLOCK_PREFIX`，行内加粗/斜体走 `inlineStyleRanges`（实测 X 的
style 值是 `Bold` 这种首字母大写）。`plain_text` 保留作兜底：结构解析失败时
它总比没有强。
"""

from __future__ import annotations

from typing import Any

#: Draft.js 块类型 → Markdown 前缀。X 的长文编辑器只产出这几种。
_BLOCK_PREFIX = {
    "header-one": "# ",
    "header-two": "## ",
    "header-three": "### ",
    "header-four": "#### ",
    "blockquote": "> ",
    "code-block": "    ",
    "unordered-list-item": "- ",
    "ordered-list-item": "1. ",
}


def _entity_map(raw: Any) -> dict[str, dict[str, Any]]:
    """entityMap 有两种形态，两种都得认。

    X 有时给 `{"0": {...}}`，有时给 `[{"key": "0", "value": {...}}]`——
    同一个字段两种形状，只认一种就会在另一种上静默丢掉所有链接。
    """
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(e.get("key")): e.get("value") or {} for e in raw if isinstance(e, dict)}
    return {}


#: 行内样式 → Markdown 标记。实测 X 给的值是 `Bold` 这种首字母大写，
#: 不是 Draft.js 文档里的全大写 `BOLD`——两种都认，免得对方哪天又改回去。
_INLINE_MARK = {"bold": "**", "italic": "*"}


def weave_spans(text: str, spans: list[tuple[int, int, str, str]]) -> str:
    """把若干 (offset, length, 前缀, 后缀) 区间编织进文本。

    不能逐个切片替换：链接和加粗可以嵌套（加粗的链接），外层区间的终点
    会被内层插入的标记推移，按原始坐标切片就错位了。所以改成收集全部
    插入点、按位置从后往前逐个插——每次插入只影响它右边的内容，而右边
    的都已经插完了。

    同一位置的处理顺序决定嵌套是否正确（后插入的排在更左边）：
    先开启后关闭 → 关闭标记落在开启标记左边（相邻区间不粘连）；
    开启之间先内后外、关闭之间先外后内 → 嵌套区间产出 `**[x](url)**`
    而不是交叉的 `**[x**](url)`。起止完全相同的两个区间（加粗的链接）
    没有天然的内外之分，取列表顺序：**后加入的套在外面**——to_markdown
    先加链接后加样式，于是样式在外，链接结构不被拆散。
    """
    inserts: list[tuple[int, int, tuple[int, int], str]] = []
    for seq, (offset, length, prefix, suffix) in enumerate(spans):
        inserts.append((offset, 0, (length, seq), prefix))
        inserts.append((offset + length, 1, (offset, -seq), suffix))
    inserts.sort(key=lambda t: (-t[0], t[1], t[2]))
    for pos, _, _, marker in inserts:
        text = f"{text[:pos]}{marker}{text[pos:]}"
    return text


def _style_spans(text: str, ranges: list[dict]) -> list[tuple[int, int, str, str]]:
    """inlineStyleRanges → 编织区间。

    区间边缘的空白要缩进去：`**加粗 **` 不是合法的 Markdown 强调，
    渲染器会原样吐出星号。
    """
    spans = []
    for r in ranges or []:
        mark = _INLINE_MARK.get(str(r.get("style") or "").lower())
        offset, length = r.get("offset"), r.get("length")
        if not mark or not isinstance(offset, int) or not isinstance(length, int):
            continue
        chunk = text[offset : offset + length]
        stripped = chunk.strip()
        if not stripped:
            continue
        offset += len(chunk) - len(chunk.lstrip())
        spans.append((offset, len(stripped), mark, mark))
    return spans


def _link_spans(ranges: list[dict], entities: dict[str, dict]) -> list[tuple[int, int, str, str]]:
    spans = []
    for r in ranges or []:
        entity = entities.get(str(r.get("key")))
        if not entity or entity.get("type") != "LINK":
            continue
        url = (entity.get("data") or {}).get("url")
        offset, length = r.get("offset"), r.get("length")
        if url and isinstance(offset, int) and isinstance(length, int):
            spans.append((offset, length, "[", f"]({url})"))
    return spans


def _media_url(block: dict, entities: dict[str, dict]) -> str | None:
    """atomic 块指向 entityMap 里的一个媒体实体。"""
    for r in block.get("entityRanges") or []:
        entity = entities.get(str(r.get("key"))) or {}
        data = entity.get("data") or {}
        for item in data.get("mediaItems") or []:
            key = item.get("mediaId") or item.get("localMediaId")
            if key:
                return str(key)
    return None


def to_markdown(content_state: dict[str, Any], media_urls: dict[str, str] | None = None) -> str:
    """Draft.js → Markdown。

    `media_urls` 把 media id 映射成真实图片地址（来自 article 的 media_entities）；
    映射不到的图片**整块跳过而不是留个坏链接**——一个 `![](None)` 比没有图更糟。
    """
    blocks = content_state.get("blocks") or []
    entities = _entity_map(content_state.get("entityMap"))
    media_urls = media_urls or {}

    lines: list[str] = []
    for block in blocks:
        kind = block.get("type") or "unstyled"

        if kind == "atomic":
            key = _media_url(block, entities)
            url = media_urls.get(key or "")
            if url:
                lines.append(f"![]({url})")
            continue

        text = block.get("text") or ""
        if not text.strip():
            continue
        spans = _link_spans(block.get("entityRanges") or [], entities)
        # 代码块里不套强调标记——那里的星号是字面内容。
        if kind != "code-block":
            spans += _style_spans(text, block.get("inlineStyleRanges") or [])
        text = weave_spans(text, spans)
        lines.append(f"{_BLOCK_PREFIX.get(kind, '')}{text}")

    return "\n\n".join(lines)


def parse(article: dict[str, Any]) -> dict[str, Any] | None:
    """把 GraphQL 的 article_results.result 拍平成可存的字段。

    结构解析不出来时回落到 `plain_text`——长文的价值在正文，宁可少格式也不能没内容。
    """
    if not article:
        return None

    media_urls = {}
    for entity in article.get("media_entities") or []:
        key = entity.get("media_id") or entity.get("media_key")
        url = ((entity.get("media_info") or {}).get("original_img_url")) or entity.get(
            "media_url_https"
        )
        if key and url:
            media_urls[str(key)] = url

    markdown = ""
    content_state = article.get("content_state")
    if isinstance(content_state, dict):
        markdown = to_markdown(content_state, media_urls)
    if not markdown.strip():
        markdown = (article.get("plain_text") or "").strip()
    if not markdown:
        return None

    return {
        "article_id": article.get("rest_id"),
        "article_title": article.get("title"),
        "article_markdown": markdown,
        # 两个摘要分开，因为性质完全不同：
        #   preview_text 是正文前 ~90 字的机械截断，逐字忠实，抓到就有；
        #   summary_text 是 Grok 生成的要点归纳，延迟生成，还可能与正文语言
        #   不一致（实测见过中文长文配英文摘要）。
        # 混在一个字段里的后果是**来源随抓取时机漂移**——早抓拿到截断、晚抓
        # 拿到机器概括，下游没法判断自己手里是哪种。平台的职责是归一化而不是
        # 分析，所以默认给忠实原文那个，AI 版另放一格由下游决定用不用。
        "article_summary": article.get("preview_text"),
        "article_ai_summary": article.get("summary_text"),
        "article_cover": (
            ((article.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
        ),
    }
