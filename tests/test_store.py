"""存储与游标测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sourcepilot.contracts import (
    BadRequest,
    Category,
    ErrorCode,
    Item,
    Source,
    SourceType,
    TimeBasis,
)
from sourcepilot.store import Store, decode_cursor, encode_cursor

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


def item(n: int, *, at: datetime | None = None, categories=()) -> Item:
    return Item(
        id=f"hotlist:fake_{n}",
        source=Source(type=SourceType.HOTLIST, name="测试源", platform="fake"),
        title=f"条目 {n}",
        url=f"https://example.com/{n}",
        published_at=None,
        discovered_at=at or (NOW - timedelta(minutes=n)),
        time_basis=TimeBasis.DISCOVERED,
        score=1.0 - n / 100,
        categories=list(categories),
    )


class TestRoundTrip:
    def test_item_survives_storage(self, store):
        original = item(1, categories=[Category.MODEL])
        store.upsert_items([original])
        (restored,) = store.query_items(limit=10)
        assert restored.model_dump() == original.model_dump()

    def test_upsert_is_idempotent(self, store):
        store.upsert_items([item(1)])
        store.upsert_items([item(1)])
        assert store.count_items() == 1

    def test_discovered_at_is_first_seen(self, store):
        """增量拉取靠 discovered_at 稳定；重抓不该把它往后推。"""
        store.upsert_items([item(1, at=NOW)])
        store.upsert_items([item(1, at=NOW + timedelta(hours=1))])
        (stored,) = store.query_items(limit=10)
        assert stored.discovered_at == NOW


class TestQuery:
    def test_filter_by_platform(self, store):
        store.upsert_items([item(1), item(2)])
        assert len(store.query_items(platforms=["fake"], limit=10)) == 2
        assert store.query_items(platforms=["nope"], limit=10) == []

    def test_filter_by_category(self, store):
        store.upsert_items([item(1, categories=[Category.MODEL]), item(2)])
        got = store.query_items(category=Category.MODEL, limit=10)
        assert [i.id for i in got] == ["hotlist:fake_1"]

    def test_filter_by_since_is_exclusive(self, store):
        store.upsert_items([item(1, at=NOW), item(2, at=NOW - timedelta(hours=2))])
        got = store.query_items(since=NOW - timedelta(hours=1), limit=10)
        assert [i.id for i in got] == ["hotlist:fake_1"]

    def test_order_by_score_for_hotlist(self, store):
        store.upsert_items([item(5), item(1)])
        got = store.query_items(limit=10, order_by_score=True)
        assert got[0].score > got[1].score


class TestCursor:
    def test_round_trip(self, store):
        it = item(1)
        stamp, item_id = decode_cursor(encode_cursor(it))
        assert item_id == it.id and stamp.endswith("Z")

    def test_cursor_is_opaque_base64(self):
        assert "|" not in encode_cursor(item(1))

    def test_garbage_cursor_is_bad_request(self):
        with pytest.raises(BadRequest) as exc:
            decode_cursor("这不是游标")
        assert exc.value.code is ErrorCode.BAD_REQUEST

    def test_keyset_pagination_has_no_gaps_or_repeats(self, store):
        store.upsert_items([item(n) for n in range(1, 26)])
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = store.query_items(limit=7, cursor=cursor)
            if not page:
                break
            seen.extend(i.id for i in page)
            cursor = encode_cursor(page[-1])
        assert len(seen) == 25
        assert len(set(seen)) == 25


class TestSourceState:
    def test_success_resets_failure_streak(self, store):
        store.record_failure("fake", ErrorCode.UPSTREAM_DOWN, NOW)
        store.record_failure("fake", ErrorCode.UPSTREAM_DOWN, NOW)
        assert store.get_state("fake")["consecutive_failures"] == 2
        store.record_success("fake", 20, NOW)
        state = store.get_state("fake")
        assert state["consecutive_failures"] == 0
        assert state["last_error_code"] is None
        assert state["last_item_count"] == 20

    def test_failure_keeps_last_success_time(self, store):
        """源刚崩时仍要知道上次成功是什么时候——降级判断和 Canary 都靠它。"""
        store.record_success("fake", 10, NOW)
        store.record_failure("fake", ErrorCode.RATE_LIMITED, NOW + timedelta(minutes=5))
        state = store.get_state("fake")
        assert state["last_success_at"] == NOW
        assert state["last_error_code"] == ErrorCode.RATE_LIMITED.value

    def test_unknown_source_has_no_state(self, store):
        assert store.get_state("never-seen") is None


class TestEffectiveAtStability:
    """排序键必须稳定。它一漂，信息流的顺序和分页就都不可信了。"""

    def test_effective_at_does_not_drift_on_recollect(self, store):
        """没有发布时间的条目，重抓时排序键不能跟着「现在」往前跑。

        漂了的话，这类条目会永远浮在信息流顶部、永远落在时间窗内，
        翻页时还会因为排序键中途变化而漏条重条。
        """
        store.upsert_items([item(1, at=NOW)])
        store.upsert_items([item(1, at=NOW + timedelta(hours=2))])
        (stored,) = store.query_items(limit=10)
        assert stored.effective_time == NOW

    def test_publish_time_wins_when_present(self, store):
        published = NOW - timedelta(days=3)
        it = item(2, at=NOW).model_copy(
            update={"published_at": published, "time_basis": TimeBasis.PUBLISHED}
        )
        store.upsert_items([it])
        (stored,) = store.query_items(limit=10)
        assert stored.effective_time == published

    def test_window_query_uses_publish_time(self, store):
        """发布于三天前的条目，不该出现在「最近 1 小时」里。"""
        published = NOW - timedelta(days=3)
        it = item(3, at=NOW).model_copy(
            update={"published_at": published, "time_basis": TimeBasis.PUBLISHED}
        )
        store.upsert_items([it])
        assert store.query_items(published_after=NOW - timedelta(hours=1), limit=10) == []
        assert store.query_items(published_after=NOW - timedelta(days=7), limit=10) != []


class TestOriginSeparatesSubscribedFromSearched:
    """现查缓存与订阅采集必须分开。

    `search_x` 的 q 是调用方随口给的——搜「Opus」会捞回「magnum opus」这种
    毫不相干的推文。落库是对的（降级链靠它兜底），但混进信息流就等于让
    任何人的一次临时查询污染所有 RSS 订阅者和 AIRADAR 的内容。
    """

    def test_searched_items_stay_out_of_the_feed(self, store):
        store.upsert_items([item(1)], origin="searched")
        assert store.query_items(limit=10) == []

    def test_degradation_path_can_still_read_them(self, store):
        """它们本来就是为现查失败那一刻落的库，降级时必须看得见。"""
        store.upsert_items([item(1)], origin="searched")
        assert len(store.query_items(limit=10, include_searched=True)) == 1

    def test_collected_is_the_default(self, store):
        """定时采集不传 origin，落下来就该是订阅内容。"""
        store.upsert_items([item(1)])
        assert len(store.query_items(limit=10)) == 1

    def test_search_cannot_demote_collected_items(self, store):
        """已在采集范围内的条目，不该因为有人搜到它而被踢出信息流。"""
        store.upsert_items([item(1)])
        store.upsert_items([item(1)], origin="searched")
        assert len(store.query_items(limit=10)) == 1

    def test_collection_promotes_a_previously_searched_item(self, store):
        """反过来要升级：定时采集也抓到了，说明它确实属于订阅内容。"""
        store.upsert_items([item(1)], origin="searched")
        assert store.query_items(limit=10) == []
        store.upsert_items([item(1)])
        assert len(store.query_items(limit=10)) == 1

    def test_old_db_backfills_to_collected(self, tmp_path):
        """老库没有这一列。默认 searched 会让整个库从信息流里消失。"""
        import sqlite3

        path = tmp_path / "legacy.db"
        store = Store(path)
        store.upsert_items([item(1)])
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE items DROP COLUMN origin")
        assert len(Store(path).query_items(limit=10)) == 1
