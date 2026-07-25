"""公众号 channel 测试。全部离线——这条线需要登录态，CI 里不可能有真凭据。"""

from __future__ import annotations

import json

import pytest

from sourcepilot.channels.wechat import (
    RET_FREQ_LIMIT,
    RET_INVALID_SESSION,
    Credentials,
    WechatClient,
    collect_wechat,
)
from sourcepilot.contracts import AuthExpired, ErrorCode, RateLimited, SourceType, TimeBasis
from sourcepilot.sources import SourceConfig

CONFIG = {
    "name": "wechat",
    "display_name": "微信公众号",
    "type": "wechat",
    "platform": "wechat",
    "channel": "wechat",
    "min_interval": 3600,
    "accounts": ["量子位"],
    "per_account_limit": 5,
    "account_interval": 0,
}

ARTICLE = {
    "aid": "2650000001_1",
    "title": "某模型发布",
    "link": "https://mp.weixin.qq.com/s/abc?chksm=xxx",
    "digest": "一句话摘要",
    "update_time": 1784711789,
}


def _publish_payload(articles):
    return {
        "base_resp": {"ret": 0},
        "publish_page": json.dumps(
            {"publish_list": [{"publish_info": json.dumps({"appmsgex": articles})}]}
        ),
    }


@pytest.fixture
def config() -> SourceConfig:
    return SourceConfig(**CONFIG)


@pytest.fixture
def creds(monkeypatch, tmp_path):
    path = tmp_path / "cred.yaml"
    path.write_text("token: '123'\ncookie: 'slave_sid=x'\n", encoding="utf-8")
    monkeypatch.setattr("sourcepilot.channels.wechat.CREDENTIALS_FILE", path)
    return path


class TestCredentials:
    def test_missing_file_returns_none(self, tmp_path):
        assert Credentials.load(tmp_path / "nope.yaml") is None

    def test_incomplete_file_returns_none(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("token: '123'\n", encoding="utf-8")
        assert Credentials.load(p) is None

    def test_repr_hides_secrets(self):
        """凭据绝不能被日志或异常栈带出去。"""
        text = repr(Credentials("tok123", "cookie456"))
        assert "tok123" not in text and "cookie456" not in text


class TestErrorMapping:
    """公众平台永远回 HTTP 200，真状态在响应体的 ret 里。"""

    def _client_returning(self, monkeypatch, payload):
        import httpx

        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200, json=payload, request=httpx.Request("GET", url)
            ),
        )
        return WechatClient(Credentials("t", "c"))

    def test_invalid_session_becomes_auth_expired(self, monkeypatch):
        client = self._client_returning(
            monkeypatch, {"base_resp": {"ret": RET_INVALID_SESSION, "err_msg": "invalid session"}}
        )
        with pytest.raises(AuthExpired) as exc:
            client.search_account("量子位")
        assert exc.value.code is ErrorCode.AUTH_EXPIRED

    def test_auth_error_does_not_leak_account_details(self, monkeypatch):
        """契约 §5：对外只说平台侧不可用，不暴露是哪个账号、为什么失效。"""
        client = self._client_returning(
            monkeypatch, {"base_resp": {"ret": RET_INVALID_SESSION}}
        )
        with pytest.raises(AuthExpired) as exc:
            client.search_account("量子位")
        assert "cookie" not in exc.value.message.lower()
        assert "token" not in exc.value.message.lower()

    def test_frequency_limit_becomes_rate_limited(self, monkeypatch):
        client = self._client_returning(monkeypatch, {"base_resp": {"ret": RET_FREQ_LIMIT}})
        with pytest.raises(RateLimited):
            client.search_account("量子位")


class TestCollect:
    def test_without_credentials_reports_auth_expired(self, config, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "sourcepilot.channels.wechat.CREDENTIALS_FILE", tmp_path / "absent.yaml"
        )
        with pytest.raises(AuthExpired, match="未配置"):
            collect_wechat(config)

    def test_no_accounts_configured_is_not_an_error(self, creds, monkeypatch):
        cfg = SourceConfig(**{**CONFIG, "accounts": []})
        assert collect_wechat(cfg) == []

    def test_normalizes_articles(self, config, creds, monkeypatch):
        import httpx

        def fake_get(self, url, **kw):
            if "searchbiz" in url:
                body = {"base_resp": {"ret": 0}, "list": [{"nickname": "量子位", "fakeid": "MZ=="}]}
            else:
                body = _publish_payload([ARTICLE])
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        (item,) = collect_wechat(config)
        assert item.id == "wechat:量子位_2650000001_1"
        assert item.source.type is SourceType.WECHAT
        assert item.title == "某模型发布"
        assert item.summary == "一句话摘要"
        assert item.time_basis is TimeBasis.PUBLISHED
        assert item.score == 0.0, "订阅流不是排行榜，不该编造热度"

    def test_one_bad_account_does_not_sink_the_channel(self, creds, monkeypatch):
        import httpx

        calls: list[str] = []

        def fake_get(self, url, **kw):
            query = kw.get("params", {}).get("query")
            if "searchbiz" in url:
                calls.append(query)
                if query == "坏号":
                    return httpx.Response(500, request=httpx.Request("GET", url))
                return httpx.Response(
                    200,
                    json={"base_resp": {"ret": 0}, "list": [{"nickname": query, "fakeid": "M=="}]},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200, json=_publish_payload([ARTICLE]), request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        cfg = SourceConfig(**{**CONFIG, "accounts": ["坏号", "量子位"]})
        items = collect_wechat(cfg)
        assert calls == ["坏号", "量子位"], "前一个账号失败不该中断后面的"
        assert len(items) == 1

    def test_auth_failure_stops_the_whole_channel(self, creds, monkeypatch):
        """凭据失效是整块的事——继续遍历账号只会白白多捅几次。"""
        import httpx

        calls: list[str] = []

        def fake_get(self, url, **kw):
            calls.append(url)
            return httpx.Response(
                200,
                json={"base_resp": {"ret": RET_INVALID_SESSION}},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        cfg = SourceConfig(**{**CONFIG, "accounts": ["甲", "乙", "丙"]})
        with pytest.raises(AuthExpired):
            collect_wechat(cfg)
        assert len(calls) == 1, "第一次就该停手"

    def test_malformed_publish_page_is_reported(self, config, creds, monkeypatch):
        import httpx

        def fake_get(self, url, **kw):
            if "searchbiz" in url:
                body = {"base_resp": {"ret": 0}, "list": [{"nickname": "量子位", "fakeid": "M=="}]}
                return httpx.Response(200, json=body, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                json={"base_resp": {"ret": 0}, "publish_page": "这不是 JSON"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        # 对方改版属于单账号异常，被吞掉后返回空——channel 整体不崩
        assert collect_wechat(config) == []


class TestShippedConfig:
    def test_wechat_source_is_disabled_by_default(self):
        """没凭据就跑不了，默认开着只会让 /health 一直红。"""
        from sourcepilot.settings import SOURCES_DIR
        from sourcepilot.sources import load_sources

        cfg = load_sources(SOURCES_DIR)["wechat"]
        assert cfg.enabled is False
        assert cfg.channel == "wechat"

    def test_credentials_file_is_gitignored(self):
        from sourcepilot.settings import PROJECT_ROOT

        assert "config/wechat_credentials.yaml" in (PROJECT_ROOT / ".gitignore").read_text()
