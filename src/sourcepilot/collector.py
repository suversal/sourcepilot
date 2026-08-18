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
from .sources.engine import NotModified
from .store import Store

log = logging.getLogger("sourcepilot.collector")

#: 连续失败时重试间隔的上限。6 小时——足够长到不再浪费请求，又足够短到
#: 人把凭据换好之后不用等一整天才恢复采集。
MAX_BACKOFF = 6 * 3600


@dataclass
class Outcome:
    """一个源刷新一次的结果，够填 meta.sources。"""

    name: str
    error: SourcePilotError | None = None
    fetched: bool = False
    collected_at: datetime | None = None
    items: list[Item] = field(default_factory=list)
    #: 对方回了 304——这一轮不用干活，库里的旧数据依然有效。
    unchanged: bool = False

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
        """这个源到点该抓了吗。

        分两条路，因为**失败的源不能按成功的节奏重试**：

        正常时按 `min_interval` 从上次成功算起。而失败时 `last_success_at`
        根本不动——只看它的话，一个坏掉的源会在每一轮调度（60 秒）里都判为
        「到点了」，然后再失败一次。公众号凭据过期三天就这样堆了 1358 次
        无谓请求，而那种错误重试多少次都不会好。

        所以失败后改从**上次尝试**算起，并按连续失败次数指数退避。封顶
        `MAX_BACKOFF`，保证人把问题修好之后最多等这么久就会自动恢复
        ——不封顶的话退避会涨到几天，源修好了也长时间不采。
        """
        state = self.store.get_state(config.name)
        if state is None:
            return True

        failures = state["consecutive_failures"] or 0
        if failures == 0:
            last_success = state["last_success_at"]
            if last_success is None:
                return True
            return now - last_success >= timedelta(seconds=config.min_interval)

        last_attempt = state["last_attempt_at"]
        if last_attempt is None:
            return True
        return now - last_attempt >= timedelta(seconds=self.backoff_seconds(config, failures))

    @staticmethod
    def backoff_seconds(config: SourceConfig, failures: int) -> float:
        """连续失败后的重试间隔：按次数翻倍，封顶 MAX_BACKOFF。

        指数是因为失败原因通常分两类——网络抖动几分钟就好，凭据失效/改版
        要人介入。前者靠前几次快速重试就能恢复，后者拖久一点也没损失。
        用同一条曲线覆盖两者，不必先判断是哪种。
        """
        exponent = min(failures - 1, 10)  # 防止 2**failures 溢出成天文数字
        return min(config.min_interval * (2**exponent), MAX_BACKOFF)

    def last_success(self, name: str) -> datetime | None:
        state = self.store.get_state(name)
        return state["last_success_at"] if state else None

    def refresh(
        self, config: SourceConfig, now: datetime, client: httpx.Client | None = None
    ) -> Outcome:
        """抓一个源并落库。抓失败不抛——错误装进 Outcome，交给上层决定怎么降级。"""
        validators_out: dict[str, str | None] = {}
        try:
            items = collect(
                config, client, self.store.get_validators(config.name), validators_out
            )
            self.store.upsert_items(items)
            self.store.record_success(config.name, len(items), now)
            if validators_out:
                self.store.save_validators(
                    config.name, validators_out.get("etag"), validators_out.get("last_modified")
                )
            return Outcome(config.name, fetched=True, collected_at=now, items=items)
        except NotModified:
            # 记成成功，但**不覆盖 last_item_count**——那是「上次真抓到多少条」，
            # 写 0 会让 /health 看起来像这个源突然没数据了。
            self.store.touch_success(config.name, now)
            log.debug("源 %s 内容未变（304），跳过解析与入库", config.name)
            return Outcome(config.name, collected_at=now, unchanged=True)
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
        alerter=None,
    ) -> None:
        self.collector = collector
        self.tick_seconds = tick_seconds
        #: 保留策略。给 None 就不清理——测试和一次性脚本不该动数据。
        self.retention = retention
        self.sweep_every = sweep_every
        #: 采集中断告警。给 None 就不告警（没配 Telegram 时就是这样）。
        #: 挂在采集之后同一个线程里：它只读状态、不出网抓取，成本可忽略，
        #: 而串行能保证判定看到的就是刚刚这一轮的结果。
        self.alerter = alerter
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

            # 告警是「出问题时才用」的东西，它自己出问题不能反过来影响采集。
            if self.alerter is not None:
                try:
                    self.alerter.poll()
                except Exception:
                    log.exception("采集告警出错，不影响采集")

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
