"""公众号 channel 测试。全部离线——这条线需要登录态，CI 里不可能有真凭据。"""

from __future__ import annotations

import json

import pytest

from sourcepilot.channels.wechat import (
    COOLDOWNS,
    Credentials,
    MpBackend,
    SogouBackend,
    WechatClient,
    collect_wechat,
)
from sourcepilot.channels.wechat.mp import RET_FREQ_LIMIT, RET_INVALID_SESSION
from sourcepilot.contracts import (
    AuthExpired,
    ErrorCode,
    RateLimited,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
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
    "backends": ["mp"],
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


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    """冷却注册表是全局单例，不重置的话上一个用例的惩罚会漏进下一个。"""
    COOLDOWNS.reset()
    yield
    COOLDOWNS.reset()


@pytest.fixture
def config() -> SourceConfig:
    return SourceConfig(**CONFIG)


@pytest.fixture
def creds(monkeypatch, tmp_path):
    path = tmp_path / "cred.yaml"
    path.write_text("token: '123'\ncookie: 'slave_sid=x'\n", encoding="utf-8")
    monkeypatch.setattr("sourcepilot.channels.wechat.mp.CREDENTIALS_FILE", path)
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
            "sourcepilot.channels.wechat.mp.CREDENTIALS_FILE", tmp_path / "absent.yaml"
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
        # 对方改版要报出来。静默返回空会让人以为「这个号最近没发文章」。
        with pytest.raises(UpstreamDown, match="改版"):
            collect_wechat(config)

    def test_content_failure_does_not_cool_down_the_backend(self, creds, monkeypatch):
        """单个号抓不到是这一个号的事——冷却整个后端会把后面的号一起饿死。"""
        import httpx

        seen: list[str] = []

        def fake_get(self, url, **kw):
            query = kw.get("params", {}).get("query")
            if "searchbiz" in url:
                seen.append(query)
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
        collect_wechat(SourceConfig(**{**CONFIG, "accounts": ["坏号", "好号"]}))
        assert COOLDOWNS.blocked("mp") is False, "内容级失败不该冷却后端"
        assert seen == ["坏号", "好号"]

    def test_rate_limit_does_cool_down_the_backend(self, creds, monkeypatch):
        """限流是在说「再捅要出事」——这才该停手。"""
        import httpx

        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200,
                json={"base_resp": {"ret": RET_FREQ_LIMIT}},
                request=httpx.Request("GET", url),
            ),
        )
        with pytest.raises(RateLimited):
            collect_wechat(SourceConfig(**{**CONFIG, "accounts": ["甲"]}))
        assert COOLDOWNS.blocked("mp") is True


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


class TestFallbackChain:
    """两条路的失效方式互不相关，串起来才能保证不会整块没数据。"""

    def _chain_config(self, **over):
        return SourceConfig(**{**CONFIG, "backends": ["mp", "sogou"], **over})

    def test_falls_back_when_primary_has_no_credentials(self, monkeypatch, tmp_path):
        """没配凭据时主力自动跳过，直接走免凭据的应急后端。"""
        monkeypatch.setattr(
            "sourcepilot.channels.wechat.mp.CREDENTIALS_FILE", tmp_path / "absent.yaml"
        )
        called: list[str] = []

        def fake_fetch(self, account, limit):
            called.append(account)
            return []

        monkeypatch.setattr(SogouBackend, "fetch", fake_fetch)
        collect_wechat(self._chain_config(accounts=["量子位"]))
        assert called == ["量子位"], "主力不可用时应落到降级后端"

    def test_missing_credentials_is_not_penalized(self, monkeypatch, tmp_path):
        """没凭据不是故障，只是这条路现在走不了，不该记进冷却。"""
        monkeypatch.setattr(
            "sourcepilot.channels.wechat.mp.CREDENTIALS_FILE", tmp_path / "absent.yaml"
        )
        monkeypatch.setattr(SogouBackend, "fetch", lambda self, a, n: [])
        collect_wechat(self._chain_config(accounts=["量子位"]))
        assert COOLDOWNS.blocked("mp") is False

    def test_primary_wins_when_it_works(self, creds, monkeypatch):
        monkeypatch.setattr(MpBackend, "fetch", lambda self, a, n: ["主力给的"])
        monkeypatch.setattr(
            SogouBackend,
            "fetch",
            lambda self, a, n: pytest.fail("主力成功时不该再问降级后端"),
        )
        assert collect_wechat(self._chain_config()) == ["主力给的"]

    def test_all_backends_down_raises_first_error(self, creds, monkeypatch):
        monkeypatch.setattr(
            MpBackend, "fetch", lambda self, a, n: (_ for _ in ()).throw(RateLimited("限流"))
        )
        monkeypatch.setattr(
            SogouBackend, "fetch", lambda self, a, n: (_ for _ in ()).throw(UpstreamDown("挂了"))
        )
        with pytest.raises(RateLimited):
            collect_wechat(self._chain_config())

    def test_everything_in_cooldown_is_reported_not_silent(self, creds):
        """全在冷却里就说清楚，别返回空让人以为「这些号没发文章」。"""
        COOLDOWNS.penalize("mp", ErrorCode.AUTH_EXPIRED)
        COOLDOWNS.penalize("sogou", ErrorCode.CAPTCHA)
        with pytest.raises(AuthExpired, match="冷却中"):
            collect_wechat(self._chain_config())


class TestCooldownStateMachine:
    """区分「临时挡一下」和「这条路废了」——两者的正确反应完全相反。"""

    def test_auth_failure_cools_far_longer_than_rate_limit(self):
        from sourcepilot.channels.wechat.cooldown import COOLDOWN_SECONDS

        assert COOLDOWN_SECONDS[ErrorCode.AUTH_EXPIRED] > COOLDOWN_SECONDS[ErrorCode.RATE_LIMITED]

    def test_blocked_until_deadline_then_released(self):
        COOLDOWNS.penalize("x", ErrorCode.RATE_LIMITED, now=1000.0)
        assert COOLDOWNS.blocked("x", now=1000.0) is True
        assert COOLDOWNS.blocked("x", now=1000.0 + 30 * 60 + 1) is False

    def test_success_clears_the_penalty(self):
        COOLDOWNS.penalize("x", ErrorCode.CAPTCHA)
        COOLDOWNS.clear("x")
        assert COOLDOWNS.blocked("x") is False

    def test_reason_is_recorded_for_diagnosis(self):
        COOLDOWNS.penalize("x", ErrorCode.AUTH_EXPIRED)
        assert COOLDOWNS.reason("x") is ErrorCode.AUTH_EXPIRED
