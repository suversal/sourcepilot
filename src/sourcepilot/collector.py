"""采集器与调度器。

把「什么时候该抓、抓失败了怎么记」从出口层剥出来——热榜的按需刷新和后台
定时刷新共用同一条路径，否则两处判断迟早会不一致。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from .contracts import Item, SourceHealth, SourcePilotError, SourceType
from .sources import SourceConfig, collect
from .store import Store

log = logging.getLogger("sourcepilot.collector")


@dataclass
class Outcome:
    """一个源刷新一次的结果，够填 meta.sources。"""

    name: str
    error: SourcePilotError | None = None
    fetched: bool = False
    collected_at: datetime | None = None
    items: list[Item] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """该刷新却没刷成，只能拿旧数据顶——这就是降级。"""
        return self.error is not None and bool(self.items)

    def to_health(self) -> SourceHealth:
        return SourceHealth(
            name=self.name,
            ok=self.error is None or bool(self.items),
            from_cache=not self.fetched,
            item_count=len(self.items),
            error_code=self.error.code if self.error else None,
        )


class Collector:
    def __init__(self, store: Store, sources: dict[str, SourceConfig]) -> None:
        self.store = store
        self.sources = sources

    def enabled(self, types: Sequence[SourceType] | None = None) -> list[SourceConfig]:
        configs = [c for c in self.sources.values() if c.enabled]
        if types is None:
            return configs
        return [c for c in configs if c.type in types]

    def is_due(self, config: SourceConfig, now: datetime) -> bool:
        state = self.store.get_state(config.name)
        if state is None or state["last_success_at"] is None:
            return True
        return now - state["last_success_at"] >= timedelta(seconds=config.min_interval)

    def last_success(self, name: str) -> datetime | None:
        state = self.store.get_state(name)
        return state["last_success_at"] if state else None

    def refresh(
        self, config: SourceConfig, now: datetime, client: httpx.Client | None = None
    ) -> Outcome:
        """抓一个源并落库。抓失败不抛——错误装进 Outcome，交给上层决定怎么降级。"""
        try:
            items = collect(config, client)
            self.store.upsert_items(items)
            self.store.record_success(config.name, len(items), now)
            return Outcome(config.name, fetched=True, collected_at=now, items=items)
        except SourcePilotError as exc:
            self.store.record_failure(config.name, exc.code, now)
            log.warning("源 %s 采集失败：%s %s", config.name, exc.code.value, exc.message)
            return Outcome(config.name, error=exc, collected_at=self.last_success(config.name))

    def refresh_due(
        self, types: Sequence[SourceType] | None = None, now: datetime | None = None
    ) -> list[Outcome]:
        """刷新所有到点的源。没到点的跳过——自适应间隔就是靠这个省下请求。"""
        now = now or datetime.now(UTC)
        outcomes: list[Outcome] = []
        with httpx.Client(follow_redirects=True) as client:
            for config in self.enabled(types):
                if self.is_due(config, now):
                    outcomes.append(self.refresh(config, now, client))
        return outcomes


class Scheduler:
    """后台定时采集。

    没有它，只有被 `/hotlist` 请求打到的源才会更新——厂商发布这类走 `/items`
    的源永远不会被抓，库里会一直是空的。
    """

    def __init__(
        self,
        collector: Collector,
        tick_seconds: float = 60.0,
        retention=None,
        sweep_every: float = 6 * 3600,
    ) -> None:
        self.collector = collector
        self.tick_seconds = tick_seconds
        #: 保留策略。给 None 就不清理——测试和一次性脚本不该动数据。
        self.retention = retention
        self.sweep_every = sweep_every
        self._last_sweep = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sourcepilot-scheduler", daemon=True
        )
        self._thread.start()
        log.info("调度器已启动，每 %.0fs 检查一次到点的源", self.tick_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                outcomes = self.collector.refresh_due()
                if outcomes:
                    ok = sum(1 for o in outcomes if o.error is None)
                    log.info("定时采集：%d 个源到点，%d 个成功", len(outcomes), ok)
            except Exception:  # 调度线程绝不能因为单次异常而死掉
                log.exception("定时采集出错，下一轮继续")

            # 清理挂在采集之后、同一个线程里——它是低频操作，不值得单开线程，
            # 而且和采集串行能避免「边删边写」。
            if self.retention is not None:
                now = time.monotonic()
                if now - self._last_sweep >= self.sweep_every:
                    self._last_sweep = now
                    try:
                        self.retention.sweep()
                    except Exception:
                        log.exception("保留策略清理出错，不影响采集")
            self._stop.wait(self.tick_seconds)
