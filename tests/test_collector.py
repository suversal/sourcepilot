"""采集器与调度器测试。抓取被打桩，不联网。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FAKE_CONFIG_DICT, FAKE_PAYLOAD

from sourcepilot.collector import Collector, Scheduler
from sourcepilot.contracts import ErrorCode, SourceType, UpstreamDown
from sourcepilot.sources import SourceConfig, engine

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


@pytest.fixture
def sources() -> dict[str, SourceConfig]:
    hot = SourceConfig(**FAKE_CONFIG_DICT)
    vendor = SourceConfig(
        **{
            **FAKE_CONFIG_DICT,
            "name": "vendorsrc",
            "display_name": "厂商测试源",
            "platform": "vendorsrc",
            "type": "vendor",
        }
    )
    return {"fake": hot, "vendorsrc": vendor}


@pytest.fixture
def broken() -> set[str]:
    return set()


@pytest.fixture
def calls(monkeypatch, broken) -> list[str]:
    seen: list[str] = []

    def fake_fetch(config, client=None):
        seen.append(config.name)
        if config.name in broken:
            raise UpstreamDown(f"{config.name} 挂了")
        return FAKE_PAYLOAD

    monkeypatch.setattr(engine, "fetch_raw", fake_fetch)
    return seen


@pytest.fixture
def collector(store, sources, calls) -> Collector:
    return Collector(store, sources)


class TestDueLogic:
    def test_never_fetched_is_due(self, collector, sources):
        assert collector.is_due(sources["fake"], NOW) is True

    def test_not_due_within_interval(self, collector, sources, store):
        store.record_success("fake", 2, NOW)
        assert collector.is_due(sources["fake"], NOW + timedelta(seconds=60)) is False

    def test_due_again_after_interval(self, collector, sources, store):
        store.record_success("fake", 2, NOW)
        later = NOW + timedelta(seconds=sources["fake"].min_interval + 1)
        assert collector.is_due(sources["fake"], later) is True

    def test_failure_does_not_reset_the_clock(self, collector, sources, store):
        """失败不该算作一次成功采集，否则源崩了反而不再重试。"""
        store.record_success("fake", 2, NOW)
        store.record_failure("fake", ErrorCode.UPSTREAM_DOWN, NOW + timedelta(seconds=10))
        assert collector.is_due(sources["fake"], NOW + timedelta(seconds=60)) is False


class TestRefresh:
    def test_success_stores_items_and_state(self, collector, store):
        outcome = collector.refresh(collector.sources["fake"], NOW)
        assert outcome.error is None and outcome.fetched is True
        assert store.count_items() == 2
        assert store.get_state("fake")["last_item_count"] == 2

    def test_failure_is_returned_not_raised(self, collector, broken):
        """采集失败不该把异常抛给上层——上层要决定降级，不是崩掉。"""
        broken.add("fake")
        outcome = collector.refresh(collector.sources["fake"], NOW)
        assert outcome.error is not None
        assert outcome.error.code is ErrorCode.UPSTREAM_DOWN
        assert outcome.fetched is False

    def test_failure_keeps_previous_collected_at(self, collector, store, broken):
        collector.refresh(collector.sources["fake"], NOW)
        broken.add("fake")
        later = NOW + timedelta(hours=1)
        outcome = collector.refresh(collector.sources["fake"], later)
        assert outcome.collected_at == NOW, "降级时要能说清数据有多旧"

    def test_degraded_only_when_stale_data_exists(self, collector, broken):
        broken.add("fake")
        outcome = collector.refresh(collector.sources["fake"], NOW)
        assert outcome.degraded is False, "库里没有旧数据就不是降级，是彻底失败"


class TestRefreshDue:
    def test_refreshes_every_enabled_source(self, collector, calls):
        collector.refresh_due(now=NOW)
        assert sorted(calls) == ["fake", "vendorsrc"]

    def test_skips_sources_not_due(self, collector, calls, store):
        store.record_success("fake", 2, NOW)
        collector.refresh_due(now=NOW + timedelta(seconds=30))
        assert calls == ["vendorsrc"]

    def test_type_filter(self, collector, calls):
        collector.refresh_due(types=[SourceType.VENDOR], now=NOW)
        assert calls == ["vendorsrc"]

    def test_disabled_source_never_fetched(self, store, calls):
        cfg = SourceConfig(**{**FAKE_CONFIG_DICT, "enabled": False})
        Collector(store, {"fake": cfg}).refresh_due(now=NOW)
        assert calls == []


class TestScheduler:
    def test_runs_collection_in_background(self, collector, calls):
        """厂商发布那类源只走 /items，没有后台采集就永远不会被抓。"""
        done = threading.Event()
        original = collector.refresh_due

        def wrapped(*a, **kw):
            result = original(*a, **kw)
            done.set()
            return result

        collector.refresh_due = wrapped
        scheduler = Scheduler(collector, tick_seconds=0.05)
        scheduler.start()
        try:
            assert done.wait(timeout=5), "调度器没有在后台跑起来"
        finally:
            scheduler.stop()
        assert set(calls) == {"fake", "vendorsrc"}

    def test_stop_is_idempotent(self, collector):
        scheduler = Scheduler(collector, tick_seconds=0.05)
        scheduler.start()
        scheduler.stop()
        scheduler.stop()

    def test_survives_a_failing_round(self, collector):
        """单轮出错不能让调度线程死掉，否则一次意外就再也不采集了。"""
        rounds: list[int] = []

        def flaky(*a, **kw):
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("这一轮炸了")
            return []

        collector.refresh_due = flaky
        scheduler = Scheduler(collector, tick_seconds=0.05)
        scheduler.start()
        try:
            deadline = threading.Event()
            deadline.wait(0.5)
        finally:
            scheduler.stop()
        assert len(rounds) >= 2, "出错后应该继续下一轮"
