"""Canary 判定逻辑测试。

它的价值全在判得准不准：报警太松等于没有，太紧会让人开始无视告警——
后者比没有告警更糟，因为它会训练人忽略真问题。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FAKE_CONFIG_DICT

from sourcepilot.canary import Canary, Health
from sourcepilot.contracts import ErrorCode
from sourcepilot.sources import SourceConfig

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def sources():
    return {"fake": SourceConfig(**{**FAKE_CONFIG_DICT, "min_interval": 600})}


@pytest.fixture
def canary(store, sources):
    return Canary(store, sources)


class TestHealthy:
    def test_recent_success_with_items_is_ok(self, canary, store):
        store.record_success("fake", 20, NOW - timedelta(minutes=5))
        assert canary.check("fake", NOW).status is Health.OK

    def test_within_its_own_interval_is_not_stale(self, canary, store):
        """1 小时抓一次的源，40 分钟没更新是正常的——用固定阈值会把慢源全报红。"""
        store.record_success("fake", 20, NOW - timedelta(minutes=25))
        assert canary.check("fake", NOW).status is Health.OK


class TestNotYetRun:
    def test_never_run_and_never_failed_is_idle(self, canary):
        """刚启动还没轮到它，不是故障。"""
        report = canary.check("fake", NOW)
        assert report.status is Health.IDLE
        assert "尚未采集" in report.reason

    def test_disabled_source_is_idle(self, store):
        cfg = SourceConfig(**{**FAKE_CONFIG_DICT, "enabled": False})
        report = Canary(store, {"fake": cfg}).check("fake", NOW)
        assert report.status is Health.IDLE

    def test_never_succeeded_but_failing_is_down(self, canary, store):
        for _ in range(3):
            store.record_failure("fake", ErrorCode.UPSTREAM_DOWN, NOW)
        report = canary.check("fake", NOW)
        assert report.status is Health.DOWN
        assert "从未成功" in report.reason


class TestDegraded:
    def test_stale_beyond_its_own_interval(self, canary, store):
        """落后判定按源自己的间隔算倍数，不用统一阈值。"""
        store.record_success("fake", 20, NOW - timedelta(seconds=600 * 3 + 60))
        report = canary.check("fake", NOW)
        assert report.status is Health.DEGRADED
        assert "距上次成功" in report.reason

    def test_zero_items_despite_success(self, canary, store):
        """采集"成功"但零条目，通常是选择器还在、内容没了——比整个挂掉更隐蔽。"""
        store.record_success("fake", 0, NOW - timedelta(minutes=1))
        report = canary.check("fake", NOW)
        assert report.status is Health.DEGRADED
        assert "零条目" in report.reason

    def test_a_few_failures_but_still_succeeding(self, canary, store):
        store.record_success("fake", 20, NOW - timedelta(minutes=2))
        store.record_failure("fake", ErrorCode.RATE_LIMITED, NOW)
        assert canary.check("fake", NOW).status is Health.DEGRADED


class TestDown:
    def test_consecutive_failures_cross_threshold(self, canary, store):
        store.record_success("fake", 20, NOW - timedelta(minutes=5))
        for _ in range(3):
            store.record_failure("fake", ErrorCode.UPSTREAM_DOWN, NOW)
        report = canary.check("fake", NOW)
        assert report.status is Health.DOWN
        assert report.consecutive_failures == 3

    def test_one_hiccup_is_not_down(self, canary, store):
        """一两次失败多半是网络抖动。为此报警会训练人忽略告警。"""
        store.record_success("fake", 20, NOW - timedelta(minutes=5))
        store.record_failure("fake", ErrorCode.TIMEOUT, NOW)
        assert canary.check("fake", NOW).status is not Health.DOWN


class TestSummary:
    def _two_sources(self, store):
        cfgs = {
            "good": SourceConfig(**{**FAKE_CONFIG_DICT, "name": "good", "platform": "good"}),
            "bad": SourceConfig(**{**FAKE_CONFIG_DICT, "name": "bad", "platform": "bad"}),
        }
        return Canary(store, cfgs)

    def test_one_dead_source_makes_overall_not_ok(self, store):
        canary = self._two_sources(store)
        store.record_success("good", 10, NOW - timedelta(minutes=1))
        for _ in range(3):
            store.record_failure("bad", ErrorCode.UPSTREAM_DOWN, NOW)
        summary = canary.summary(NOW)
        assert summary["ok"] is False
        assert [p["name"] for p in summary["problems"]] == ["bad"]

    def test_degraded_alone_does_not_flip_overall_ok(self, store):
        """一个源落后不等于平台不可用——整体 ok 只由「彻底不产出」决定。"""
        canary = self._two_sources(store)
        store.record_success("good", 10, NOW - timedelta(minutes=1))
        store.record_success("bad", 0, NOW - timedelta(minutes=1))  # 零条目 → degraded
        summary = canary.summary(NOW)
        assert summary["ok"] is True
        assert summary["counts"]["degraded"] == 1

    def test_problems_only_lists_the_broken_ones(self, store):
        canary = self._two_sources(store)
        store.record_success("good", 10, NOW - timedelta(minutes=1))
        store.record_success("bad", 10, NOW - timedelta(minutes=1))
        summary = canary.summary(NOW)
        assert summary["problems"] == []
        assert summary["counts"]["ok"] == 2


class TestCooldownPersistence:
    """冷却必须落盘。

    只放进程内的话，重启一次就清零——真被封号时重启一下就又去捅了。
    这是账号安全问题，不是体验问题。
    """

    def test_survives_a_restart(self, store):
        from sourcepilot.channels.cooldown import CooldownRegistry

        first = CooldownRegistry(store)
        first.penalize("x:acct1", ErrorCode.AUTH_EXPIRED)
        assert first.blocked("x:acct1") is True

        # 模拟进程重启：全新的注册表，只能靠库恢复
        second = CooldownRegistry(store)
        assert second.blocked("x:acct1") is True
        assert second.reason("x:acct1") is ErrorCode.AUTH_EXPIRED

    def test_expired_entries_are_not_restored(self, store):
        """过期的不该复活，否则重启会凭空延长冷却。"""
        from datetime import timedelta

        from sourcepilot.channels.cooldown import CooldownRegistry

        store.save_cooldown(
            "old", datetime.now(UTC) - timedelta(minutes=1), ErrorCode.RATE_LIMITED.value
        )
        assert CooldownRegistry(store).blocked("old") is False

    def test_clear_removes_it_from_disk_too(self, store):
        from sourcepilot.channels.cooldown import CooldownRegistry

        reg = CooldownRegistry(store)
        reg.penalize("b", ErrorCode.CAPTCHA)
        reg.clear("b")
        assert CooldownRegistry(store).blocked("b") is False

    def test_without_a_store_it_still_works_in_memory(self):
        """没挂 Store 时退化成纯内存——测试和临时脚本不该被迫建库。"""
        from sourcepilot.channels.cooldown import CooldownRegistry

        reg = CooldownRegistry()
        reg.penalize("c", ErrorCode.RATE_LIMITED)
        assert reg.blocked("c") is True

    def test_storage_failure_does_not_break_the_in_memory_penalty(self, store, monkeypatch):
        """落盘失败时内存里的冷却仍要生效——宁可少一层保险，不能当场不冷却。"""
        from sourcepilot.channels.cooldown import CooldownRegistry

        reg = CooldownRegistry(store)
        monkeypatch.setattr(
            store, "save_cooldown", lambda *a: (_ for _ in ()).throw(RuntimeError("盘满了"))
        )
        reg.penalize("d", ErrorCode.AUTH_EXPIRED)
        assert reg.blocked("d") is True
