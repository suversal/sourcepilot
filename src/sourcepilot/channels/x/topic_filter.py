"""X 话题订阅的确定性质量过滤。

X SearchTimeline 只保证整段文本命中查询词，不保证两个词彼此相关。长推文在末尾
堆一串热门词时，会同时命中「Grok」和「announced」却与 AI 新闻毫无关系。
这里不做语义判断，只校验词是否出现在主要内容区域、彼此是否足够接近。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...sources.config import ChannelTopic


def _value(record: object, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def _term_spans(text: str, terms: Sequence[str]) -> list[tuple[int, int]]:
    folded = text.casefold()
    spans: list[tuple[int, int]] = []
    for term in terms:
        needle = term.strip().casefold()
        if not needle:
            continue
        start = 0
        while (index := folded.find(needle, start)) >= 0:
            spans.append((index, index + len(needle)))
            start = index + max(1, len(needle))
    return spans


def _segment_matches(topic: ChannelTopic, text: str) -> bool:
    focus = _term_spans(text, topic.focus_terms)
    if not focus:
        return False
    if not topic.context_terms:
        return True

    context = _term_spans(text, topic.context_terms)
    if not context:
        return False
    if topic.max_term_distance is None:
        return True

    for focus_start, focus_end in focus:
        for context_start, context_end in context:
            gap = max(0, max(focus_start, context_start) - min(focus_end, context_end))
            if gap <= topic.max_term_distance:
                return True
    return False


def topic_content_matches(topic: ChannelTopic, record: object) -> bool:
    """判断核心词是否出现在主要内容区域，并与上下文词足够接近。"""
    if not topic.focus_terms:
        return True

    # X Article 的搜索载荷已经带标题和机械摘要，正文则要另发请求才能取得。
    # 把标题+摘要视作同一内容段，既能保住纯 t.co 入口，也不依赖额外请求。
    article = "\n".join(
        str(value).strip()
        for value in (
            _value(record, "article_title"),
            _value(record, "article_summary"),
        )
        if value and str(value).strip()
    )
    text = str(_value(record, "text", "") or "")
    if topic.focus_window_chars:
        text = text[: topic.focus_window_chars]

    return any(
        _segment_matches(topic, segment)
        for segment in (article, text)
        if segment
    )


def topic_record_matches(topic: ChannelTopic, record: object) -> bool:
    """判断一条推文是否同时通过点赞与主要内容位置规则。"""
    if topic.min_likes and (_value(record, "likes", 0) or 0) < topic.min_likes:
        return False
    return topic_content_matches(topic, record)


def limit_topic_authors[T](topic: ChannelTopic, records: Sequence[T]) -> list[T]:
    """保持 X 原排序，同一作者超过单轮限额的条目直接略过。"""
    if not topic.per_author_limit:
        return list(records)

    kept: list[T] = []
    counts: dict[str, int] = {}
    for record in records:
        author = str(_value(record, "author_handle", "") or "").lstrip("@").casefold()
        if counts.get(author, 0) >= topic.per_author_limit:
            continue
        counts[author] = counts.get(author, 0) + 1
        kept.append(record)
    return kept
