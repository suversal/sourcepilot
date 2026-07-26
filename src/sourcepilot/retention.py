"""数据保留策略：定期清掉不再有价值的条目。

**为什么不能一刀切按时间删**：库里同时躺着两类性质完全不同的数据。

热榜是**快照**——「今天 B站排行榜第 3 名」这件事，一周后没有任何价值，
它既不是新闻也不构成记录。而厂商官方发布是**一手资料**，OpenAI 2023 年那篇
发布说明今天检索起来照样有用（`window=all` 就是为这种检索留的）。

所以按 `source_type` 分级保留，而不是统一 TTL。

**清理的真正动机也不是磁盘**。实测 2400 条才 2 MB，一年十万条也就 100 MB，
Mac mini 毫无压力。真正会痛的是 `q=` 关键词检索——它是全表扫描（当初选
LIKE 而非 FTS5，因为 FTS5 的分词器对中文都不好使），条目数上去之后会明显退化。
所以清理的目标是**把表控制在扫得动的规模**，不是省空间。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .contracts import SourceType
from .store import Store

log = logging.getLogger("sourcepilot.retention")

#: 各类型保留多少天。None = 永久保留。
#:
#: hotlist / x  —— 快照与时效内容，过期即无价值
#: vendor       —— 一手资料，历史检索有用，永久留
#: wechat       —— 订阅的号本来就是精选，量小，留久一点
#: rss / web    —— 保守起见按 vendor 处理（目前没有这两类源）
RETENTION_DAYS: dict[SourceType, int | None] = {
    SourceType.HOTLIST: 90,
    SourceType.X: 30,
    SourceType.WECHAT: 365,
    SourceType.VENDOR: None,
    SourceType.RSS: None,
    SourceType.WEB: None,
}

#: 正文缓存保留多久。它是请求驱动才产生的，不是主动囤积；
#: 30 天够覆盖「一条新闻的生命周期 + 下游流水线重跑」，过期即清。
ARTICLE_CACHE_DAYS = 30


class Retention:
    def __init__(self, store: Store, days: dict[SourceType, int | None] | None = None) -> None:
        self.store = store
        self.days = days if days is not None else RETENTION_DAYS

    def plan(self, now: datetime | None = None) -> dict[str, int]:
        """算一遍会删多少，但不删。用于 `--dry-run` 和 /health 展示。"""
        return self._run(now or datetime.now(UTC), commit=False)

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """真的删。返回各类型删掉的条数。"""
        result = self._run(now or datetime.now(UTC), commit=True)
        total = sum(result.values())
        if total:
            log.info("保留策略清理了 %d 条：%s", total, result)
        return result

    def _run(self, now: datetime, *, commit: bool) -> dict[str, int]:
        result: dict[str, int] = {}
        for source_type, days in self.days.items():
            if days is None:
                continue
            cutoff = now - timedelta(days=days)
            # 按 effective_at（发布时间，取不到则收录时间）判定，与时间窗口径一致。
            # 用 discovered_at 的话，一篇今天才被发现的旧文会被立刻删掉。
            count = self.store.count_items_before(source_type, cutoff)
            if count and commit:
                self.store.delete_items_before(source_type, cutoff)
            if count:
                result[source_type.value] = count
        return result
