"""X 长文（X Articles）正文解析。

X 的长文不是推文——推文里只挂一个 article 实体，正文在 `content_state` 里，
格式是 Draft.js 的 `{blocks, entityMap}`。搜索与时间线接口返回的 article
**只有 `preview_text`**（约 100 字预览），正文必须单独请求并打开
`withArticleRichContentState` 才拿得到。

**为什么转 Markdown 而不是直接用 `plain_text`**：X 同时给了纯文本版，但那一版
把二级标题和正文段落拍平成同样的行，链接也只剩锚文本——下游拿到的是一堆看不出
结构的段落。而 Draft.js 里标题、链接、加粗都在，转成 Markdown 后展示端能直接渲染。
`plain_text` 保留作兜底：结构解析失败时它总比没有强。
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


def _apply_links(text: str, ranges: list[dict], entities: dict[str, dict]) -> str:
    """把链接实体套回文本，产出 Markdown 链接。

    从后往前替换——每次替换都会改变字符串长度，从前往后做的话后面所有
    offset 就全错位了。
    """
    spans = []
    for r in ranges or []:
        entity = entities.get(str(r.get("key")))
        if not entity or entity.get("type") != "LINK":
            continue
        url = (entity.get("data") or {}).get("url")
        offset, length = r.get("offset"), r.get("length")
        if url and isinstance(offset, int) and isinstance(length, int):
            spans.append((offset, length, url))

    for offset, length, url in sorted(spans, key=lambda s: s[0], reverse=True):
        anchor = text[offset : offset + length]
        text = f"{text[:offset]}[{anchor}]({url}){text[offset + length:]}"
    return text


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
        text = _apply_links(text, block.get("entityRanges") or [], entities)
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
        "article_summary": article.get("summary_text") or article.get("preview_text"),
        "article_cover": (
            ((article.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
        ),
    }
