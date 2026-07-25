"""`search_x` 与 `get_x_timeline` 的服务层。

这是整个平台**唯一的现查工具**，也是契约 §3 那条降级链真正落地的地方：

    live=true → 现查（带超时）
                  ├─ 成功            → mode=live,  stale=false
                  ├─ 超时/限流/验证码 → 回落缓存
                  │                    ├─ 缓存有 → mode=cache, stale=true, ok=true
                  │                    └─ 缓存空 → ok=false, 报原始错误码
                  └─ 参数错误        → ok=false（不降级）
    live=false → 只读缓存 → mode=cache, stale=false（用户要的就是缓存，不算降级）

「降级不是错误」这条是契约里最容易写错的地方：现查没成但缓存兜住了，
对调用方来说结果是可用的，只是不实时——所以 ok 仍是 true，靠 stale 说明情况。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from .channels.x import XRouter
from .contracts import (
    WINDOW_SECONDS,
    Envelope,
    ErrorCode,
    GetXTimelineParams,
    ItemsPayload,
    Meta,
    Mode,
    SearchXParams,
    SourcePilotError,
    SourceType,
)
from .settings import LIVE_TIMEOUT
from .store import Store, encode_cursor

log = logging.getLogger("sourcepilot.x")

#: 这些故障才值得降级到缓存。参数错误降级没有意义——缓存里也没有
#: 「用户打错的那个词」的结果，返回一堆不相干的旧数据比报错更糟。
DEGRADABLE_TO_CACHE = frozenset(
    {
        ErrorCode.TIMEOUT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.CAPTCHA,
        ErrorCode.UPSTREAM_DOWN,
        ErrorCode.AUTH_EXPIRED,
    }
)


class XSearchService:
    def __init__(self, store: Store, router: XRouter | None = None) -> None:
        self.store = store
        self.router = router or XRouter()

    def _from_cache(self, *, q: str | None, window, limit: int, cursor: str | None):
        span = WINDOW_SECONDS[window]
        return self.store.query_items(
            source_type=SourceType.X,
            q=q,
            published_after=(None if span is None else datetime.now(UTC) - timedelta(seconds=span)),
            limit=limit + 1,
            cursor=cursor,
        )

    def _cache_envelope(
        self, rows, limit: int, started: float, *, stale: bool
    ) -> Envelope[ItemsPayload]:
        has_more = len(rows) > limit
        items = rows[:limit]
        meta = Meta(
            mode=Mode.CACHE,
            stale=stale,
            collected_at=max((i.discovered_at for i in items), default=None),
            next_cursor=encode_cursor(items[-1]) if items and has_more else None,
            has_more=has_more,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return Envelope[ItemsPayload].success(ItemsPayload(items=items), meta)

    def search(self, params: SearchXParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()

        if not params.live:
            # 用户明确要缓存，拿到缓存就是正确结果——不是降级，所以 stale=false。
            rows = self._from_cache(
                q=params.q, window=params.window, limit=params.limit, cursor=params.cursor
            )
            return self._cache_envelope(rows, params.limit, started, stale=False)

        try:
            items, cursor = self.router.search(params.q, params.limit, params.cursor)
        except SourcePilotError as exc:
            if exc.code not in DEGRADABLE_TO_CACHE:
                return Envelope[ItemsPayload].failure(exc.code, exc.message)

            rows = self._from_cache(
                q=params.q, window=params.window, limit=params.limit, cursor=params.cursor
            )
            if not rows:
                # 缓存也空——这才是真失败，把原始错误码报上去让 Agent 能分支决策。
                return Envelope[ItemsPayload].failure(exc.code, exc.message)
            log.info("search_x 现查失败（%s），降级到缓存", exc.code.value)
            return self._cache_envelope(rows, params.limit, started, stale=True)

        # 现查到的结果顺手入库，下次现查失败时才有东西可降级。
        if items:
            self.store.upsert_items(items)

        meta = Meta(
            mode=Mode.LIVE,
            stale=False,
            collected_at=datetime.now(UTC),
            next_cursor=cursor,
            has_more=bool(cursor),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return Envelope[ItemsPayload].success(ItemsPayload(items=items), meta)


class XTimelineService:
    def __init__(self, store: Store, router: XRouter | None = None) -> None:
        self.store = store
        self.router = router or XRouter()

    def get(self, params: GetXTimelineParams) -> Envelope[ItemsPayload]:
        started = time.perf_counter()
        handle = params.handle

        def from_cache():
            span = WINDOW_SECONDS[params.window]
            return self.store.query_items(
                source_type=SourceType.X,
                q=None,
                published_after=(
                    None if span is None else datetime.now(UTC) - timedelta(seconds=span)
                ),
                limit=params.limit + 1,
                cursor=params.cursor,
            )

        def envelope(rows, *, stale: bool, mode: Mode):
            has_more = len(rows) > params.limit
            items = rows[: params.limit]
            return Envelope[ItemsPayload].success(
                ItemsPayload(items=items),
                Meta(
                    mode=mode,
                    stale=stale,
                    collected_at=max((i.discovered_at for i in items), default=None),
                    next_cursor=encode_cursor(items[-1]) if items and has_more else None,
                    has_more=has_more,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                ),
            )

        if not params.live:
            rows = [i for i in from_cache() if (i.author or "").lower() == handle.lower()]
            return envelope(rows, stale=False, mode=Mode.CACHE)

        try:
            items, cursor = self.router.timeline(handle, params.limit, params.cursor)
        except SourcePilotError as exc:
            if exc.code not in DEGRADABLE_TO_CACHE:
                return Envelope[ItemsPayload].failure(exc.code, exc.message)
            rows = [i for i in from_cache() if (i.author or "").lower() == handle.lower()]
            if not rows:
                return Envelope[ItemsPayload].failure(exc.code, exc.message)
            log.info("get_x_timeline 现查失败（%s），降级到缓存", exc.code.value)
            return envelope(rows, stale=True, mode=Mode.CACHE)

        if items:
            self.store.upsert_items(items)

        return Envelope[ItemsPayload].success(
            ItemsPayload(items=items),
            Meta(
                mode=Mode.LIVE,
                stale=False,
                collected_at=datetime.now(UTC),
                next_cursor=cursor,
                has_more=bool(cursor),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
        )


__all__ = ["LIVE_TIMEOUT", "XSearchService", "XTimelineService"]
