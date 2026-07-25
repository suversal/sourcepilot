"""契约不变量测试。这些断言就是契约本身——改红了说明在破坏合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sourcepilot.contracts import (
    CONTRACT_VERSION,
    Envelope,
    ErrorCode,
    GetFeedParams,
    GetXTimelineParams,
    Item,
    ItemsPayload,
    Meta,
    Mode,
    SearchXParams,
    Source,
    SourceType,
    TimeBasis,
    Timeout,
    Window,
)

NOW = datetime(2026, 7, 25, 10, 3, 0, tzinfo=UTC)
X_SOURCE = Source(type=SourceType.X, name="X / Twitter")


def make_item(**overrides) -> Item:
    fields = {
        "id": "x:1234567890",
        "source": X_SOURCE,
        "title": "示例推文",
        "url": "https://x.com/a/status/1234567890",
        "published_at": NOW - timedelta(minutes=3),
        "discovered_at": NOW,
        "time_basis": TimeBasis.PUBLISHED,
        "score": 0.5,
    }
    return Item(**{**fields, **overrides})


class TestItem:
    def test_id_prefix_must_match_source_type(self):
        with pytest.raises(ValidationError, match="不一致"):
            make_item(id="hotlist:123")

    def test_id_must_be_namespaced(self):
        with pytest.raises(ValidationError, match="source_type:native_id"):
            make_item(id="1234567890")

    def test_published_time_basis_requires_published_at(self):
        with pytest.raises(ValidationError, match="published_at 为空"):
            make_item(published_at=None, time_basis=TimeBasis.PUBLISHED)

    def test_missing_published_at_is_not_backfilled(self):
        """契约修订 #6：取不到发布时间就是 null，绝不用收录时间冒充。"""
        item = make_item(published_at=None, time_basis=TimeBasis.DISCOVERED)
        assert item.published_at is None
        assert item.effective_time == item.discovered_at
        assert item.model_dump()["published_at"] is None

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError, match="必须带时区"):
            make_item(discovered_at=datetime(2026, 7, 25, 10, 3, 0))

    def test_time_normalized_to_utc_z(self):
        beijing = timezone(timedelta(hours=8))
        item = make_item(discovered_at=datetime(2026, 7, 25, 18, 3, 0, tzinfo=beijing))
        assert item.discovered_at == NOW
        assert item.model_dump()["discovered_at"] == "2026-07-25T10:03:00Z"

    def test_score_bounded_to_unit_interval(self):
        """契约修订 #5：score 固定 [0,1]，源内相对热度。"""
        for bad in (-0.1, 1.5, 42.0):
            with pytest.raises(ValidationError):
                make_item(score=bad)

    def test_categories_default_empty(self):
        assert make_item().categories == []

    def test_author_at_sign_stripped(self):
        assert make_item(author="@elonmusk").author == "elonmusk"

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            make_item(rank=1)

    def test_build_derives_id_and_time_basis(self):
        item = Item.build(
            source_type=SourceType.HOTLIST,
            native_id="weibo_9",
            published_at=None,
            source=Source(type=SourceType.HOTLIST, name="微博热搜", platform="weibo"),
            title="某热搜",
            url="https://s.weibo.com/weibo?q=x",
            score=0.9,
        )
        assert item.id == "hotlist:weibo_9"
        assert item.time_basis is TimeBasis.DISCOVERED


class TestEnvelope:
    def test_success_shape(self):
        env = Envelope[ItemsPayload].success(ItemsPayload(items=[make_item()]))
        assert env.ok is True
        assert env.error is None
        assert env.meta.contract_version == CONTRACT_VERSION
        assert env.meta.stale is False

    def test_degraded_is_not_an_error(self):
        """契约 §3：现查失败但缓存兜住了 → ok=True + stale=True，不是错误。"""
        env = Envelope[ItemsPayload].degraded(
            ItemsPayload(items=[make_item()]), collected_at=NOW
        )
        assert env.ok is True
        assert env.error is None
        assert env.meta.stale is True
        assert env.meta.mode is Mode.CACHE

    def test_explicit_cache_request_is_not_stale(self):
        """live=false 时用户要的就是缓存，拿到缓存不算降级。"""
        env = Envelope[ItemsPayload].success(
            ItemsPayload(items=[]), Meta(mode=Mode.CACHE, collected_at=NOW)
        )
        assert env.meta.stale is False

    def test_failure_carries_code(self):
        env = Envelope[ItemsPayload].failure(ErrorCode.TIMEOUT, "现查超时且无缓存")
        assert env.ok is False
        assert env.data is None
        assert env.error.code is ErrorCode.TIMEOUT
        assert env.meta.contract_version == CONTRACT_VERSION

    def test_from_exception(self):
        env = Envelope[ItemsPayload].from_exception(Timeout("上游 8s 未响应"))
        assert env.error.code is ErrorCode.TIMEOUT

    def test_partial_source_failure_still_ok(self):
        """一个源崩了不许拖垮全局：整体 ok，失败详情在 meta.sources。"""
        meta = Meta(
            mode=Mode.CACHE,
            sources=[
                {"name": "weibo", "ok": True, "from_cache": True, "item_count": 20},
                {
                    "name": "zhihu",
                    "ok": False,
                    "item_count": 0,
                    "error_code": ErrorCode.UPSTREAM_DOWN,
                },
            ],
        )
        env = Envelope[ItemsPayload].success(ItemsPayload(items=[make_item()]), meta)
        assert env.ok is True
        assert [s.ok for s in env.meta.sources] == [True, False]


class TestToolParams:
    def test_window_and_live_are_independent(self):
        """契约修订 #1：window 只表时间范围，live 只控取数模式。"""
        p = SearchXParams(q="AI", window=Window.H24, live=False)
        assert p.window is Window.H24 and p.live is False
        assert "live" not in {w.value for w in Window}

    def test_search_x_defaults(self):
        p = SearchXParams(q="AI")
        assert (p.limit, p.window, p.live, p.cursor) == (20, Window.D7, True, None)

    def test_limit_upper_bound_enforced(self):
        with pytest.raises(ValidationError):
            SearchXParams(q="AI", limit=101)

    def test_unknown_param_rejected(self):
        with pytest.raises(ValidationError):
            SearchXParams(q="AI", mode="live")

    def test_handle_at_sign_stripped(self):
        assert GetXTimelineParams(handle="@elonmusk").handle == "elonmusk"

    def test_since_and_cursor_are_orthogonal(self):
        """契约修订 #4：since 是过滤条件，cursor 是分页位置，可同时存在。"""
        p = GetFeedParams(since=NOW, cursor="opaque-token", window=Window.D7)
        assert p.since == NOW and p.cursor == "opaque-token"

    def test_registry_covers_all_six_tools(self):
        from sourcepilot.contracts import TOOL_REGISTRY

        assert set(TOOL_REGISTRY) == {
            "search_x",
            "get_x_timeline",
            "get_hotlist",
            "get_wechat_feed",
            "read_article",
            "get_feed",
        }


class TestErrors:
    def test_http_status_mapping_complete(self):
        from sourcepilot.contracts import HTTP_STATUS

        assert set(HTTP_STATUS) == set(ErrorCode)

    def test_degradable_excludes_client_errors(self):
        """参数错误不该触发降级——降级救不了打错的 query。"""
        from sourcepilot.contracts import DEGRADABLE

        assert ErrorCode.BAD_REQUEST not in DEGRADABLE
        assert ErrorCode.NOT_FOUND not in DEGRADABLE
        assert ErrorCode.RATE_LIMITED in DEGRADABLE
