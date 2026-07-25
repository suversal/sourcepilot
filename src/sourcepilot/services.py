"""服务层：把信源、缓存、降级策略拼成工具的实际行为。

出口层（REST / MCP）只负责翻译协议，业务判断全在这里，三出口才可能真正共用一套核心。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx

from .contracts import (
    WINDOW_SECONDS,
    BadRequest,
    Envelope,
    GetFeedParams,
    GetHotlistParams,
    Item,
    ItemsPayload,
    Meta,
    Mode,
    SourceHealth,
    SourcePilotError,
)
from .sources import SourceConfig, collect, load_sources
from .store import Store, encode_cursor


class Platform:
    """一次热榜刷新的结果，够填 meta.sources 就行。"""

    __slots__ = ("name", "items", "error", "from_cache", "collected_at")

    def __init__(
        self,
        name: str,
        items: list[Item],
        error: SourcePilotError | None,
        from_cache: bool,
        collected_at: datetime | None,
    ) -> None:
        self.name = name
        self.items = items
        self.error = error
        self.from_cache = from_cache
        self.collected_at = collected_at

    def to_health(self) -> SourceHealth:
        return SourceHealth(
            name=self.name,
            ok=self.error is None or bool(self.items),
            from_cache=self.from_cache,
            item_count=len(self.items),
            error_code=self.error.code if self.error else None,
        )


class HotlistService:
    """`get_hotlist`：缓存模式。

    契约把热榜定为缓存取数，所以这里永远回报 `mode=cache`——刷新是平台自己的
    节奏（自适应间隔），不是用户请求的一部分。`collected_at` 才是给下游判断
    新鲜度的依据。
    """

    def __init__(self, store: Store, sources: dict[str, SourceConfig] | None = None) -> None:
        self.store = store
        self.sources = sources if sources is not None else load_sources()

    def enabled_sources(self, platform: str | None) -> list[SourceConfig]:
        configs = [c for c in self.sources.values() if c.enabled]
        if platform is None:
            return configs
        picked = [c for c in configs if c.platform == platform or c.name == platform]
        if not picked:
            known = sorted({c.platform or c.name for c in configs})
            raise BadRequest(f"未知平台 {platform!r}，可用：{', '.join(known)}")
        return picked

    def _needs_refresh(self, config: SourceConfig, now: datetime) -> bool:
        state = self.store.get_state(config.name)
        if state is None or state["last_success_at"] is None:
            return True
        return now - state["last_success_at"] >= timedelta(seconds=config.min_interval)

    def _refresh(self, config: SourceConfig, now: datetime, client: httpx.Client) -> Platform:
        try:
            items = collect(config, client)
            self.store.upsert_items(items)
            self.store.record_success(config.name, len(items), now)
            return Platform(config.name, items, None, from_cache=False, collected_at=now)
        except SourcePilotError as exc:
            self.store.record_failure(config.name, exc.code, now)
            return Platform(config.name, [], exc, from_cache=True, collected_at=None)

    def _from_cache(self, config: SourceConfig, limit: int) -> list[Item]:
        return self.store.query_items(
            platforms=[config.platform or config.name],
            limit=limit,
            order_by_score=True,
        )

    def get(self, params: GetHotlistParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        now = datetime.now(UTC)
        configs = self.enabled_sources(params.platform)

        results: list[Platform] = []
        with httpx.Client(follow_redirects=True) as client:
            for config in configs:
                if self._needs_refresh(config, now):
                    result = self._refresh(config, now, client)
                else:
                    state = self.store.get_state(config.name)
                    result = Platform(
                        config.name,
                        [],
                        None,
                        from_cache=True,
                        collected_at=state["last_success_at"] if state else None,
                    )
                # 无论现抓还是读缓存，都统一从库里取——保证排序和截断口径一致。
                cached = self._from_cache(config, params.limit)
                if result.error is not None and cached:
                    # 刷新该做而没做成，但库里还有旧数据：这就是降级。
                    state = self.store.get_state(config.name)
                    result.collected_at = state["last_success_at"] if state else None
                result.items = cached
                results.append(result)

        items = sorted(
            (i for r in results for i in r.items),
            key=lambda i: (i.score, i.discovered_at),
            reverse=True,
        )

        stamps = [r.collected_at for r in results if r.collected_at is not None]
        degraded = any(r.error is not None and r.items for r in results)
        all_failed = bool(results) and all(r.error is not None and not r.items for r in results)

        meta = Meta(
            mode=Mode.CACHE,
            stale=degraded,
            collected_at=min(stamps) if stamps else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            sources=[r.to_health() for r in results],
        )

        if all_failed:
            first = next(r.error for r in results if r.error is not None)
            return Envelope[ItemsPayload].failure(first.code, first.message, meta)
        return Envelope[ItemsPayload].success(ItemsPayload(items=items), meta)


class FeedService:
    """`get_feed`：喂 AIRADAR 的归一化信息流。纯缓存，带增量与分页。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, params: GetFeedParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        now = datetime.now(UTC)
        window_start = now - timedelta(seconds=WINDOW_SECONDS[params.window])

        # 多取一条用来判断 has_more，返回前砍掉。
        rows = self.store.query_items(
            source_type=params.source,
            category=params.category,
            since=params.since,
            discovered_after=window_start,
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
