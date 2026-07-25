"""服务层：把信源、缓存、降级策略拼成工具的实际行为。

出口层（REST / MCP）只负责翻译协议，业务判断全在这里，三出口才可能真正共用一套核心。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from .collector import Collector, Outcome
from .contracts import (
    WINDOW_SECONDS,
    BadRequest,
    Envelope,
    GetFeedParams,
    GetHotlistParams,
    ItemsPayload,
    Meta,
    Mode,
    SourceType,
)
from .store import Store, encode_cursor


class HotlistService:
    """`get_hotlist`：缓存模式。

    契约把热榜定为缓存取数，所以这里永远回报 `mode=cache`——刷新是平台自己的
    节奏（自适应间隔），不是用户请求的一部分。`collected_at` 才是给下游判断
    新鲜度的依据。
    """

    def __init__(self, collector: Collector) -> None:
        self.collector = collector
        self.store: Store = collector.store

    def _configs(self, platform: str | None):
        """只认 hotlist 类型的源——厂商发布那类走 /items，不该混进热榜。"""
        configs = self.collector.enabled(types=[SourceType.HOTLIST])
        if platform is None:
            return configs
        picked = [c for c in configs if c.platform == platform or c.name == platform]
        if not picked:
            known = sorted({c.platform or c.name for c in configs})
            raise BadRequest(f"未知平台 {platform!r}，可用：{', '.join(known)}")
        return picked

    def get(self, params: GetHotlistParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        now = datetime.now(UTC)
        configs = self._configs(params.platform)

        outcomes: list[Outcome] = []
        for config in configs:
            if self.collector.is_due(config, now):
                outcome = self.collector.refresh(config, now)
            else:
                outcome = Outcome(
                    config.name, collected_at=self.collector.last_success(config.name)
                )
            # 无论现抓还是读缓存，都统一从库里取——保证排序和截断口径一致。
            outcome.items = self.store.query_items(
                platforms=[config.platform or config.name],
                limit=params.limit,
                order_by_score=True,
            )
            outcomes.append(outcome)

        items = sorted(
            (i for o in outcomes for i in o.items),
            key=lambda i: (i.score, i.discovered_at),
            reverse=True,
        )
        stamps = [o.collected_at for o in outcomes if o.collected_at is not None]
        all_failed = bool(outcomes) and all(
            o.error is not None and not o.items for o in outcomes
        )

        meta = Meta(
            mode=Mode.CACHE,
            stale=any(o.degraded for o in outcomes),
            collected_at=min(stamps) if stamps else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            sources=[o.to_health() for o in outcomes],
        )

        if all_failed:
            first = next(o.error for o in outcomes if o.error is not None)
            return Envelope[ItemsPayload].failure(first.code, first.message, meta)
        return Envelope[ItemsPayload].success(ItemsPayload(items=items), meta)


class FeedService:
    """`get_feed`：喂 AIRADAR 的归一化信息流。纯缓存，带增量与分页。

    这里不触发抓取——库由后台调度器填。查询路径上不做网络请求，才能保证
    AIRADAR 每次拿数据都是毫秒级。
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, params: GetFeedParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        now = datetime.now(UTC)
        span = WINDOW_SECONDS[params.window]
        window_start = None if span is None else now - timedelta(seconds=span)

        # 多取一条用来判断 has_more，返回前砍掉。
        rows = self.store.query_items(
            source_type=params.source,
            platforms=[params.platform] if params.platform else None,
            category=params.category,
            q=params.q,
            since=params.since,
            published_after=window_start,
            limit=params.limit + 1,
            cursor=params.cursor,
        )
        has_more = len(rows) > params.limit
        items = rows[: params.limit]

        meta = Meta(
            mode=Mode.CACHE,
            stale=False,
            collected_at=max((i.discovered_at for i in items), default=None),
            next_cursor=encode_cursor(items[-1]) if items and has_more else None,
            has_more=has_more,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return Envelope[ItemsPayload].success(ItemsPayload(items=items), meta)
