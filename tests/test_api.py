"""REST 出口与服务层测试。抓取被打桩，不联网。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FAKE_CONFIG_DICT, FAKE_PAYLOAD
from fastapi.testclient import TestClient

from sourcepilot.api import create_app
from sourcepilot.contracts import CONTRACT_VERSION, UpstreamDown
from sourcepilot.sources import SourceConfig, engine


@pytest.fixture
def sources() -> dict[str, SourceConfig]:
    second = {
        **FAKE_CONFIG_DICT,
        "name": "other",
        "display_name": "另一个源",
        "platform": "other",
    }
    return {
        "fake": SourceConfig(**FAKE_CONFIG_DICT),
        "other": SourceConfig(**second),
    }


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """记录每次真实抓取，用来验证缓存有没有生效。"""
    seen: list[str] = []

    def fake_fetch(config, client=None):
        seen.append(config.name)
        if config.name in BROKEN:
            raise UpstreamDown(f"{config.name} 挂了")
        return FAKE_PAYLOAD

    monkeypatch.setattr(engine, "fetch_raw", fake_fetch)
    return seen


BROKEN: set[str] = set()


@pytest.fixture(autouse=True)
def _reset_broken():
    BROKEN.clear()
    yield
    BROKEN.clear()


@pytest.fixture
def client(store, sources, calls) -> TestClient:
    return TestClient(create_app(store=store, sources=sources, scheduler=False))


class TestHotlist:
    def test_returns_items_from_all_sources(self, client):
        body = client.get("/api/v1/hotlist").json()
        assert body["ok"] is True
        assert {s["name"] for s in body["meta"]["sources"]} == {"fake", "other"}
        assert len(body["data"]["items"]) == 4

    def test_mode_is_always_cache(self, client):
        """契约把热榜定为缓存取数；刷新是平台自己的节奏，不是用户请求的一部分。"""
        assert client.get("/api/v1/hotlist").json()["meta"]["mode"] == "cache"

    def test_second_call_serves_cache_without_refetching(self, client, calls):
        client.get("/api/v1/hotlist")
        assert sorted(calls) == ["fake", "other"]
        body = client.get("/api/v1/hotlist").json()
        assert sorted(calls) == ["fake", "other"], "间隔内不该再次抓取"
        assert all(s["from_cache"] for s in body["meta"]["sources"])
        assert body["data"]["items"], "读缓存也要有数据"

    def test_platform_filter(self, client):
        body = client.get("/api/v1/hotlist", params={"platform": "fake"}).json()
        assert [s["name"] for s in body["meta"]["sources"]] == ["fake"]

    def test_unknown_platform_is_bad_request(self, client):
        r = client.get("/api/v1/hotlist", params={"platform": "weibo"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "BAD_REQUEST"
        assert "可用" in r.json()["error"]["message"]

    def test_limit_upper_bound(self, client):
        assert client.get("/api/v1/hotlist", params={"limit": 999}).status_code == 400

    def test_unknown_query_param_rejected(self, client):
        assert client.get("/api/v1/hotlist", params={"live": "true"}).status_code == 400


class TestPartialFailure:
    def test_one_broken_source_does_not_sink_the_rest(self, client, store):
        client.get("/api/v1/hotlist")  # 先把两个源的数据都灌进缓存
        BROKEN.add("other")
        # 把 other 的成功时间推老，逼它重抓
        store.record_success("other", 2, datetime.now(UTC) - timedelta(hours=2))

        body = client.get("/api/v1/hotlist").json()
        assert body["ok"] is True, "一个源崩了不许拖垮全局"
        health = {s["name"]: s for s in body["meta"]["sources"]}
        assert health["fake"]["ok"] is True
        assert health["other"]["error_code"] == "UPSTREAM_DOWN"
        assert health["other"]["item_count"] > 0, "该源仍应回落到自己的旧缓存"

    def test_failed_refresh_with_stale_cache_sets_stale_flag(self, client, store):
        client.get("/api/v1/hotlist")
        BROKEN.add("other")
        store.record_success("other", 2, datetime.now(UTC) - timedelta(hours=2))
        body = client.get("/api/v1/hotlist").json()
        assert body["meta"]["stale"] is True, "该刷新却没刷成、只能给旧数据 = 降级"

    def test_all_sources_broken_with_empty_cache_is_an_error(self, store, sources, calls):
        BROKEN.update({"fake", "other"})
        client = TestClient(create_app(store=store, sources=sources, scheduler=False))
        r = client.get("/api/v1/hotlist")
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "UPSTREAM_DOWN"
        assert r.json()["data"] is None


class TestFeed:
    def test_window_filters_on_publish_time_not_discovery(self, client, store):
        """时间窗问的是「最近发生了什么」。

        按收录时间过滤的话，首次采集会把陈年旧文全变成「今天的新闻」——
        OpenAI 官网 RSS 里几年前的文章会一股脑挤进 24h 窗口。
        """
        client.get("/api/v1/hotlist")
        assert client.get("/api/v1/items", params={"window": "30d"}).json()["data"]["items"]

        # 收录时间保持今天，只把发布时间推到很久以前
        with store._conn() as conn:
            conn.execute("UPDATE items SET effective_at = '2020-01-01T00:00:00Z'")

        body = client.get("/api/v1/items", params={"window": "30d"}).json()
        assert body["data"]["items"] == [], "发布于 2020 年的条目不该出现在 30 天窗口里"

    def test_since_still_tracks_discovery_time(self, client, store):
        """增量同步问的是「上次拉取之后你们又收到了什么」，那必须看收录时间。"""
        client.get("/api/v1/hotlist")
        with store._conn() as conn:
            conn.execute("UPDATE items SET effective_at = '2020-01-01T00:00:00Z'")
        body = client.get(
            "/api/v1/items",
            params={"window": "30d", "since": "2020-06-01T00:00:00Z"},
        ).json()
        assert body["data"]["items"] == [], "window 与 since 是与的关系，各管各的时间"

    def test_pagination_yields_each_item_once(self, client):
        client.get("/api/v1/hotlist")
        seen, cursor = [], None
        for _ in range(5):
            params = {"limit": 1, "window": "30d", **({"cursor": cursor} if cursor else {})}
            body = client.get("/api/v1/items", params=params).json()
            seen += [i["id"] for i in body["data"]["items"]]
            cursor = body["meta"]["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 4

    def test_last_page_has_no_cursor(self, client):
        client.get("/api/v1/hotlist")
        body = client.get("/api/v1/items", params={"limit": 100, "window": "30d"}).json()
        assert body["meta"]["has_more"] is False
        assert body["meta"]["next_cursor"] is None

    def test_since_and_cursor_coexist(self, client):
        client.get("/api/v1/hotlist")
        old = "2020-01-01T00:00:00Z"
        first = client.get(
            "/api/v1/items", params={"since": old, "limit": 1, "window": "30d"}
        ).json()
        cursor = first["meta"]["next_cursor"]
        second = client.get(
            "/api/v1/items",
            params={"since": old, "cursor": cursor, "limit": 1, "window": "30d"},
        ).json()
        assert second["ok"] is True
        assert second["data"]["items"][0]["id"] != first["data"]["items"][0]["id"]

    def test_bad_cursor_is_bad_request(self, client):
        r = client.get("/api/v1/items", params={"cursor": "!!!"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "BAD_REQUEST"

    def test_feed_never_reports_stale(self, client):
        """纯缓存查询是用户要的结果，不算降级。"""
        assert client.get("/api/v1/items").json()["meta"]["stale"] is False


class TestMetaEndpoints:
    def test_root_advertises_contract_version(self, client):
        assert client.get("/").json()["contract_version"] == CONTRACT_VERSION

    def test_health_reports_per_source_state(self, client):
        client.get("/api/v1/hotlist")
        body = client.get("/api/v1/health").json()
        assert body["cached_items"] == 4
        states = {s["name"]: s for s in body["sources"]}
        assert states["fake"]["last_success_at"] is not None
        assert states["fake"]["consecutive_failures"] == 0

    def test_health_surfaces_failures(self, client, store):
        BROKEN.add("fake")
        client.get("/api/v1/hotlist")
        states = {s["name"]: s for s in client.get("/api/v1/health").json()["sources"]}
        assert states["fake"]["last_error_code"] == "UPSTREAM_DOWN"
        assert states["fake"]["consecutive_failures"] == 1
