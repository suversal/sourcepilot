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
    GetWechatFeedParams,
    ItemsPayload,
    Meta,
    Mode,
    SourceType,
    split_platforms,
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
        wanted = split_platforms(platform)
        if not wanted:
            return configs
        known = {c.platform or c.name for c in configs}
        unknown = [p for p in wanted if p not in known]
        if unknown:
            # 报出到底哪个名字不认识 + 可用清单，调用方才改得动。
            raise BadRequest(
                f"未知平台 {', '.join(repr(p) for p in unknown)}，可用：{', '.join(sorted(known))}"
            )
        return [c for c in configs if (c.platform or c.name) in wanted]

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
    """`get_feed`：喂下游的归一化信息流。纯缓存，带增量与分页。

    这里不触发抓取——库由后台调度器填。查询路径上不做网络请求，才能保证
    消费方每次拿数据都是毫秒级。
    """

    def __init__(self, store: Store, sources: dict | None = None) -> None:
        self.store = store
        # 用来校验 platform 名字。消费方按名单挑信源，名字打错必须当场说清楚，
        # 否则表现为「查不到数据」，那会被误认为是没有新内容。
        self._sources = sources or {}

    def _platforms(self, value: str | None) -> list[str] | None:
        wanted = split_platforms(value)
        if not wanted:
            return None
        if self._sources:
            known = {c.platform or name for name, c in self._sources.items()}
            unknown = [p for p in wanted if p not in known]
            if unknown:
                raise BadRequest(
                    f"未知平台 {', '.join(repr(p) for p in unknown)}，"
                    f"可用：{', '.join(sorted(known))}"
                )
        return wanted

    def get(self, params: GetFeedParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        now = datetime.now(UTC)
        span = WINDOW_SECONDS[params.window]
        window_start = None if span is None else now - timedelta(seconds=span)

        # 多取一条用来判断 has_more，返回前砍掉。
        rows = self.store.query_items(
            source_type=params.source,
            platforms=self._platforms(params.platform),
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


class WechatFeedService:
    """`get_wechat_feed`：订阅公众号的最新文章（缓存）。

    与信息流走同一个库、同一套分页——公众号只是 source_type 不同，
    没必要为它单开一条查询路径。
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, params: GetWechatFeedParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        span = WINDOW_SECONDS[params.window]
        window_start = (
            None if span is None else datetime.now(UTC) - timedelta(seconds=span)
        )

        rows = self.store.query_items(
            source_type=SourceType.WECHAT,
            platforms=[params.account] if params.account else None,
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
