"""保留策略测试。

删数据是不可逆的，所以这里的每条断言都在防一种「删错」：删了不该删的、
按错误的时间判定、或者把一刀切当成分级。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sourcepilot.contracts import Item, Source, SourceType, TimeBasis
from sourcepilot.retention import RETENTION_DAYS, Retention

NOW = datetime(2026, 7, 26, tzinfo=UTC)


def make(source_type: SourceType, days_ago: int, n: int = 1) -> list[Item]:
    published = NOW - timedelta(days=days_ago)
    return [
        Item(
            id=f"{source_type.value}:{source_type.value}_{days_ago}_{i}",
            source=Source(type=source_type, name="测试源", platform=source_type.value),
            title=f"{source_type.value} 第 {i} 条",
            url=f"https://example.com/{source_type.value}/{days_ago}/{i}",
            published_at=published,
            discovered_at=NOW,
            time_basis=TimeBasis.PUBLISHED,
            score=0.0,
        )
        for i in range(n)
    ]


@pytest.fixture
def retention(store):
    return Retention(store)


class TestTieredRetention:
    """分级而不是一刀切：热榜是快照，厂商发布是一手资料，价值衰减完全不同。"""

    def test_old_hotlist_is_swept(self, retention, store):
        store.upsert_items(make(SourceType.HOTLIST, days_ago=120))
        assert retention.sweep(NOW) == {"hotlist": 1}
        assert store.count_items() == 0

    def test_vendor_is_kept_forever(self, retention, store):
        """OpenAI 三年前那篇发布说明今天检索起来照样有用（window=all 就为这个）。"""
        store.upsert_items(make(SourceType.VENDOR, days_ago=1000))
        assert retention.sweep(NOW) == {}
        assert store.count_items() == 1

    def test_recent_hotlist_is_kept(self, retention, store):
        store.upsert_items(make(SourceType.HOTLIST, days_ago=10))
        retention.sweep(NOW)
        assert store.count_items() == 1

    def test_x_expires_sooner_than_wechat(self):
        """推文时效性远强于公众号文章，保留期该不一样。"""
        assert RETENTION_DAYS[SourceType.X] < RETENTION_DAYS[SourceType.WECHAT]

    def test_each_type_uses_its_own_window(self, retention, store):
        store.upsert_items(make(SourceType.X, days_ago=60))       # X 保留 30 天 → 删
        store.upsert_items(make(SourceType.WECHAT, days_ago=60))  # 公众号 365 天 → 留
        result = retention.sweep(NOW)
        assert result == {"x": 1}
        assert store.count_items() == 1


class TestJudgedByPublishTime:
    def test_uses_publish_time_not_discovery_time(self, retention, store):
        """按收录时间判的话，一篇今天才被发现的旧文会被立刻删掉。"""
        old = make(SourceType.HOTLIST, days_ago=200)[0]
        # 发布于 200 天前，但今天才收录
        store.upsert_items([old.model_copy(update={"discovered_at": NOW})])
        assert retention.sweep(NOW) == {"hotlist": 1}

    def test_item_without_publish_time_uses_discovery(self, retention, store):
        """没有发布时间的条目退回收录时间——那是我们唯一知道的时间。"""
        item = make(SourceType.HOTLIST, days_ago=0)[0].model_copy(
            update={
                "published_at": None,
                "time_basis": TimeBasis.DISCOVERED,
                "discovered_at": NOW - timedelta(days=200),
            }
        )
        store.upsert_items([item])
        assert retention.sweep(NOW) == {"hotlist": 1}


class TestPlanIsDryRun:
    def test_plan_reports_without_deleting(self, retention, store):
        """删数据不可逆，得能先看会删什么。"""
        store.upsert_items(make(SourceType.HOTLIST, days_ago=120, n=3))
        assert retention.plan(NOW) == {"hotlist": 3}
        assert store.count_items() == 3, "plan 不该真的删"

    def test_sweep_on_empty_db_is_a_noop(self, retention):
        assert retention.sweep(NOW) == {}


class TestSchedulerIntegration:
    def test_scheduler_without_retention_never_deletes(self, store):
        """给 None 就不清理——测试和一次性脚本不该动数据。"""
        from sourcepilot.collector import Collector, Scheduler

        scheduler = Scheduler(Collector(store, {}), tick_seconds=0.01)
        assert scheduler.retention is None
