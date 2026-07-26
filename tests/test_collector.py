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

    def fake_fetch(config, client=None, *a, **kw):
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


class TestVerifyUrls:
    """URL 是推导出来的时候，得能把「推导规则失效」变成可见的条目减少。"""

    PAGE = (
        '<div class="card"><img alt="Alive Post"/></div>'
        '<div class="card"><img alt="Dead Post"/></div>'
    )
    CONFIG = {
        "name": "slugsrc",
        "display_name": "推导 URL 的源",
        "base_url": "https://example.com",
        "verify_urls": True,
        "request": {"url": "https://example.com/blog"},
        "extract": {
            "format": "html",
            "list": "div.card",
            "fields": {
                "title": {"select": "img", "attr": "alt"},
                "native_id": {"template": "{title}", "type": "slug"},
                "url": {"template": "{base_url}/p/{native_id}"},
            },
        },
    }

    @pytest.fixture
    def stub_http(self, monkeypatch):
        import httpx

        from sourcepilot.sources import engine as eng

        monkeypatch.setattr(eng, "fetch_raw", lambda config, client=None, *a, **kw: self.PAGE)

        def fake_head(self_client, url, **kw):
            code = 404 if "dead-post" in url else 200
            return httpx.Response(code, request=httpx.Request("HEAD", url))

        monkeypatch.setattr(httpx.Client, "head", fake_head)

    def test_dead_urls_dropped(self, stub_http):
        from sourcepilot.sources import collect

        items = collect(SourceConfig(**self.CONFIG))
        assert [i.title for i in items] == ["Alive Post"]

    def test_disabled_by_default_keeps_everything(self, stub_http):
        from sourcepilot.sources import collect

        items = collect(SourceConfig(**{**self.CONFIG, "verify_urls": False}))
        assert len(items) == 2, "没开校验就不该多发请求，也不该丢条目"


class TestMaxItemsCapsTheWork:
    """源给多少不由我们定，但解析和入库多少由我们定。

    OpenAI 的 RSS 一次吐 1050 篇十年历史，每 15 分钟重解析一遍全量，
    而其中的新内容通常是 0 条。
    """

    CONFIG = {
        "name": "manysrc",
        "display_name": "条目很多的源",
        "platform": "manysrc",
        "request": {"url": "https://example.com/feed"},
        "extract": {
            "format": "json",
            "list": "rows",
            "fields": {"native_id": "id", "title": "title", "url": "link"},
        },
    }

    def _payload(self, n: int):
        return {
            "rows": [
                {"id": str(i), "title": f"第 {i} 条", "link": f"https://example.com/{i}"}
                for i in range(n)
            ]
        }

    def test_truncates_to_the_cap(self, monkeypatch):
        from sourcepilot.sources import SourceConfig, engine

        config = SourceConfig(**{**self.CONFIG, "max_items": 10})
        assert len(engine.normalize(config, self._payload(500))) == 10

    def test_keeps_the_head_not_a_random_slice(self, monkeypatch):
        """源给的顺序有意义——RSS 按时间倒序、榜单按名次，要的是最前面那些。"""
        from sourcepilot.sources import SourceConfig, engine

        config = SourceConfig(**{**self.CONFIG, "max_items": 3})
        items = engine.normalize(config, self._payload(50))
        assert [i.title for i in items] == ["第 0 条", "第 1 条", "第 2 条"]

    def test_none_means_unlimited(self):
        """接一个新源时要能一次收全历史，所以得留得掉这个盖子的口子。"""
        from sourcepilot.sources import SourceConfig, engine

        config = SourceConfig(**{**self.CONFIG, "max_items": None})
        assert len(engine.normalize(config, self._payload(500))) == 500

    def test_a_short_source_is_untouched(self):
        from sourcepilot.sources import SourceConfig, engine

        config = SourceConfig(**{**self.CONFIG, "max_items": 100})
        assert len(engine.normalize(config, self._payload(20))) == 20


class TestConditionalRequests:
    """304 既不是错误，也不是「抓到 0 条」——两种记法都会误导 /health。"""

    def _config(self):
        from sourcepilot.sources import SourceConfig

        return SourceConfig(
            name="condsrc",
            display_name="支持条件请求的源",
            platform="condsrc",
            request={"url": "https://example.com/feed"},
            extract={
                "format": "json",
                "list": "rows",
                "fields": {"native_id": "id", "title": "title", "url": "link"},
            },
        )

    def test_not_modified_is_not_a_failure(self, store, monkeypatch):
        from sourcepilot.collector import Collector
        from sourcepilot.sources import engine

        config = self._config()
        store.record_success("condsrc", 42, NOW)

        def raise_304(*a, **kw):
            raise engine.NotModified("condsrc")

        monkeypatch.setattr(engine, "fetch_raw", raise_304)
        outcome = Collector(store, {"condsrc": config}).refresh(config, NOW)

        assert outcome.error is None, "304 不是错误"
        assert outcome.unchanged is True
        assert store.get_state("condsrc")["consecutive_failures"] == 0

    def test_item_count_survives_a_304(self, store, monkeypatch):
        """写 0 会让 /health 看起来像这个源突然没数据了。"""
        from sourcepilot.collector import Collector
        from sourcepilot.sources import engine

        config = self._config()
        store.record_success("condsrc", 42, NOW)
        monkeypatch.setattr(
            engine, "fetch_raw", lambda *a, **kw: (_ for _ in ()).throw(engine.NotModified("x"))
        )
        Collector(store, {"condsrc": config}).refresh(config, NOW)
        assert store.get_state("condsrc")["last_item_count"] == 42

    def test_validators_round_trip(self, store):
        store.record_success("condsrc", 1, NOW)
        store.save_validators("condsrc", '"abc123"', "Wed, 21 Oct 2026 07:28:00 GMT")
        assert store.get_validators("condsrc") == (
            '"abc123"',
            "Wed, 21 Oct 2026 07:28:00 GMT",
        )

    def test_unknown_source_has_no_validators(self, store):
        """首次采集当然没有校验器，不能因此炸掉。"""
        assert store.get_validators("从没见过") == (None, None)
