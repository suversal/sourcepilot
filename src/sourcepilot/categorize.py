"""确定性分类打标。

采集侧不做 LLM 分析（铁律）——分类只来自源级映射 + 关键词/正则规则表。
规则是配置不是代码，改分类改 config/categories.yaml。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from .contracts import Category
from .settings import CATEGORIES_FILE


class Categorizer:
    def __init__(self, rules: dict) -> None:
        self._source_rules: dict[str, list[Category]] = {
            key: [Category(c) for c in value]
            for key, value in (rules.get("source_rules") or {}).items()
        }
        self._keyword_rules: list[tuple[Category, list[str], list[re.Pattern[str]]]] = []
        for name, spec in (rules.get("keyword_rules") or {}).items():
            spec = spec or {}
            self._keyword_rules.append(
                (
                    Category(name),
                    [k.lower() for k in (spec.get("keywords") or [])],
                    [re.compile(p) for p in (spec.get("patterns") or [])],
                )
            )

    def classify(
        self,
        *,
        title: str,
        summary: str | None = None,
        source_keys: tuple[str | None, ...] = (),
    ) -> list[Category]:
        hits: list[Category] = []
        for key in source_keys:
            if key and key in self._source_rules:
                hits.extend(self._source_rules[key])

        haystack = f"{title} {summary or ''}"
        lowered = haystack.lower()
        for category, keywords, patterns in self._keyword_rules:
            if any(k in lowered for k in keywords) or any(
                p.search(haystack) for p in patterns
            ):
                hits.append(category)

        seen: set[Category] = set()
        return [c for c in hits if not (c in seen or seen.add(c))]


@lru_cache(maxsize=1)
def get_categorizer(path: Path | None = None) -> Categorizer:
    path = path or CATEGORIES_FILE
    if not path.exists():
        return Categorizer({})
    return Categorizer(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
