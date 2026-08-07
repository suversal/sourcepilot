"""公众号 channel 测试。全部离线——这条线需要登录态，CI 里不可能有真凭据。"""

from __future__ import annotations

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

#: 字段名照真实响应（2026-07-26 实测 appmsg?action=list_ex）。
ARTICLE = {
    "aid": "2650000001_1",
    "appmsgid": "2650000001",
    "title": "某模型发布",
    "link": "https://mp.weixin.qq.com/s/abc?chksm=xxx",
    "digest": "一句话摘要",
    "cover": "https://mmbiz.qlogo.cn/cover.jpg",
    "update_time": 1784711789,
    "create_time": 1784711000,
}


def _publish_payload(articles):
    """list_ex 的响应形状——扁平的 app_msg_list，不是嵌套两层的 publish_page。"""
    return {"base_resp": {"ret": 0}, "app_msg_list": articles}


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

    def test_article_list_freq_limit_says_capability_closed_not_retry_later(
        self, monkeypatch
    ):
        """appmsg 的 200013 与 searchbiz 的 200013 含义不同，报错必须分开说。

        2026-07-30 起微信关掉了第三方跨号拉列表的能力（换账号、换 IP 都无效），
        此时报「稍后重试」会把运维带向错误的修复动作——那正是我们花了几天
        去换 token、换账号才排除掉的弯路。
        """
        client = self._client_returning(monkeypatch, {"base_resp": {"ret": RET_FREQ_LIMIT}})
        with pytest.raises(RateLimited) as exc:
            client.list_articles("MzIzNjc1NzUzMw==")
        assert "不是临时频控" in exc.value.message
        assert "wechat.yaml" in exc.value.message
        # searchbiz 那条仍是真频控，不该被换成同一句
        with pytest.raises(RateLimited) as search_exc:
            client.search_account("量子位")
        assert search_exc.value.message != exc.value.message

    def test_mp_closure_stays_documented_in_the_config(self):
        """mp 被关的证据必须留在配置文件里。

        channel 已靠 weread 恢复工作，正因如此更要留档——否则下一个人看到
        「公众号采集好好的」，会顺手把 mp 加回 backends，然后重新花几天去查
        那个 200013 是不是自己把额度打爆了。
        """
        from sourcepilot.settings import PROJECT_ROOT

        text = (PROJECT_ROOT / "config" / "sources" / "wechat.yaml").read_text(
            encoding="utf-8"
        )
        assert "200013" in text and "searchbiz 仍返回 ret=0" in text


class TestCollect:
    def test_without_credentials_is_a_config_state_not_a_failure(
        self, config, monkeypatch, tmp_path
    ):
        """从没配过凭据是配置状态，不是故障。

        仓库里这个源默认开着，别人克隆下来没凭据——那时候满屏报错没有意义，
        /health 里的 last_item_count=0 已经把情况说清楚了。
        """
        monkeypatch.setattr(
            "sourcepilot.channels.wechat.mp.CREDENTIALS_FILE", tmp_path / "absent.yaml"
        )
        assert collect_wechat(config) == []

    def test_all_backends_cooling_down_is_a_failure(self, config, creds):
        """全在冷却里说明刚被限流或凭据被拒——那是要让人看见的故障。"""
        COOLDOWNS.penalize("mp", ErrorCode.RATE_LIMITED)
        with pytest.raises(AuthExpired, match="冷却"):
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

    def test_uses_list_ex_endpoint(self, config, creds, monkeypatch):
        """必须打 appmsg?action=list_ex。

        appmsgpublish 返回的是转义两层的 publish_page（publish_list → publish_info
        → appmsgex），解析链长且脆；实测同一个号，list_ex 直接给扁平的
        app_msg_list，一次 20 条、字段齐全。
        """
        import httpx

        seen: list[dict] = []

        def fake_get(self, url, **kw):
            seen.append({"url": url, **kw.get("params", {})})
            if "searchbiz" in url:
                body = {"base_resp": {"ret": 0}, "list": [{"nickname": "量子位", "fakeid": "M=="}]}
            else:
                body = _publish_payload([ARTICLE])
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        collect_wechat(config)
        listing = seen[-1]
        assert listing["url"].endswith("/cgi-bin/appmsg")
        assert listing["action"] == "list_ex"
        assert listing["type"] == 9

    def test_cover_becomes_media(self, config, creds, monkeypatch):
        import httpx

        def fake_get(self, url, **kw):
            body = (
                {"base_resp": {"ret": 0}, "list": [{"nickname": "量子位", "fakeid": "M=="}]}
                if "searchbiz" in url
                else _publish_payload([ARTICLE])
            )
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        (item,) = collect_wechat(config)
        assert len(item.media) == 1

    def test_malformed_publish_page_is_reported(self, config, creds, monkeypatch):
        import httpx

        def fake_get(self, url, **kw):
            if "searchbiz" in url:
                body = {"base_resp": {"ret": 0}, "list": [{"nickname": "量子位", "fakeid": "M=="}]}
                return httpx.Response(200, json=body, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                json={"base_resp": {"ret": 0}},  # 少了 app_msg_list
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
    def test_wechat_source_uses_the_channel(self):
        from sourcepilot.settings import SOURCES_DIR
        from sourcepilot.sources import load_sources

        cfg = load_sources(SOURCES_DIR)["wechat"]
        assert cfg.channel == "wechat"
        assert "sogou" not in cfg.backends, "搜狗实测兜不住，不该在默认链里"
        # 2026-08-06：mp 的跨号列表被微信关了，主力换成微信读书。
        # mp 也不该留在链里——它每轮只会白撞一次 200013 消耗额度。
        assert cfg.backends == ["weread"]

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
            # 真实后端拿到的是 ChannelAccount，取 name 与它们保持一致。
            called.append(getattr(account, "name", account))
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
        from sourcepilot.channels.cooldown import COOLDOWN_SECONDS

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


class TestAccountsAcceptFakeid:
    """按名字搜会搜错号——实测搜「智谱AI」命中的是个 2022 年就停更的同名号，
    搜「Kimi」命中的是 2018 年一个毫不相干的号。

    而且不给 fakeid 的话每个号每轮都要先搜一次，搜索正是公众平台上最容易
    触发风控的动作。
    """

    def test_plain_string_still_works(self):
        """老配置不该因为这个特性而失效。"""
        from sourcepilot.sources.config import ChannelAccount, SourceConfig

        c = SourceConfig(
            name="w", display_name="公众号", channel="wechat", accounts=["量子位"]
        )
        assert c.accounts == [ChannelAccount(name="量子位", fakeid=None)]

    def test_mapping_carries_the_fakeid(self):
        from sourcepilot.sources.config import SourceConfig

        c = SourceConfig(
            name="w", display_name="公众号", channel="wechat",
            accounts=[{"name": "智谱清言", "fakeid": "MzkwMDU2MTEwMg=="}],
        )
        assert c.accounts[0].fakeid == "MzkwMDU2MTEwMg=="

    def test_fakeid_skips_the_search_call(self, monkeypatch, tmp_path):
        """给了 fakeid 就不该再去搜——这是省下风控面的关键。"""
        from sourcepilot.channels.wechat.mp import Credentials, MpBackend, WechatClient
        from sourcepilot.sources.config import ChannelAccount

        monkeypatch.setattr(Credentials, "load", classmethod(lambda cls, path=None: Credentials("t", "c")))
        monkeypatch.setattr(
            WechatClient, "search_account",
            lambda self, kw: pytest.fail("给了 fakeid 就不该再调搜索"),
        )
        seen = {}
        monkeypatch.setattr(
            WechatClient, "list_articles",
            lambda self, fakeid, count=20: seen.setdefault("fakeid", fakeid) and [],
        )
        MpBackend().fetch(ChannelAccount(name="智谱清言", fakeid="FAKEID123"), 5)
        assert seen["fakeid"] == "FAKEID123"

    def test_missing_fakeid_falls_back_to_search(self, monkeypatch):
        """只有名字时仍要能work——否则老配置直接不采了。"""
        from sourcepilot.channels.wechat.mp import Credentials, MpBackend, WechatClient
        from sourcepilot.sources.config import ChannelAccount

        monkeypatch.setattr(Credentials, "load", classmethod(lambda cls, path=None: Credentials("t", "c")))
        monkeypatch.setattr(WechatClient, "search_account", lambda self, kw: {"fakeid": "SEARCHED"})
        seen = {}
        monkeypatch.setattr(
            WechatClient, "list_articles",
            lambda self, fakeid, count=20: seen.setdefault("fakeid", fakeid) and [],
        )
        MpBackend().fetch(ChannelAccount(name="量子位"), 5)
        assert seen["fakeid"] == "SEARCHED"

    def test_alias_beats_nickname_in_search(self):
        """微信号全平台唯一且不可改，昵称既会改也会重名。

        拿微信号搜时不能被某个昵称更像的结果抢先——搜 `minimax-platform`
        返回的第一条是「MiniMax 稀宇科技」（alias 是 minimax-openplatform），
        那是另一个号。
        """
        from sourcepilot.channels.wechat.mp import Credentials, WechatClient

        client = WechatClient(Credentials("t", "c"))
        client._get = lambda url, params: {
            "list": [
                {"nickname": "MiniMax 稀宇科技", "alias": "minimax-openplatform", "fakeid": "A"},
                {"nickname": "MiniMax开放平台", "alias": "minimax-platform", "fakeid": "B"},
            ]
        }
        assert client.search_account("minimax-platform")["fakeid"] == "B"

    def test_nickname_still_matches_when_no_alias_hit(self):
        from sourcepilot.channels.wechat.mp import Credentials, WechatClient

        client = WechatClient(Credentials("t", "c"))
        client._get = lambda url, params: {
            "list": [
                {"nickname": "别的号", "alias": "other", "fakeid": "A"},
                {"nickname": "量子位", "alias": "QbitAI", "fakeid": "B"},
            ]
        }
        assert client.search_account("量子位")["fakeid"] == "B"

    def test_alias_is_recorded_but_never_sent(self, monkeypatch):
        """alias 是给人看的身份凭据，不参与请求——请求只认 fakeid。"""
        from sourcepilot.channels.wechat.mp import Credentials, MpBackend, WechatClient
        from sourcepilot.sources.config import ChannelAccount

        monkeypatch.setattr(
            Credentials, "load", classmethod(lambda cls, path=None: Credentials("t", "c"))
        )
        monkeypatch.setattr(
            WechatClient, "search_account",
            lambda self, kw: pytest.fail("有 fakeid 时不该搜索"),
        )
        seen = {}
        monkeypatch.setattr(
            WechatClient, "list_articles",
            lambda self, fakeid, count=20: seen.setdefault("fakeid", fakeid) and [],
        )
        MpBackend().fetch(
            ChannelAccount(name="火山引擎", fakeid="FID", alias="volcengine"), 5
        )
        assert seen["fakeid"] == "FID"


class TestCredentialCheckUsesTheRealEndpoint:
    """公众平台**按接口分别限流**，验证凭据必须打采集真正用的那个。

    实测撞到过：searchbiz 返回 ret=0（看着一切正常），appmsg 同时返回
    200013 freq control——而采集走的是 appmsg。只验证前者会得出「凭据没问题」
    的错误结论，然后继续困惑为什么采集一直失败。
    """

    def _creds(self, tmp_path, monkeypatch):
        from sourcepilot.channels.wechat import check as check_mod

        path = tmp_path / "cred.yaml"
        path.write_text("token: '123'\ncookie: 'a=b'\n", encoding="utf-8")
        monkeypatch.setattr(check_mod, "CREDENTIALS_FILE", path)
        return check_mod

    def test_probes_both_endpoints(self, tmp_path, monkeypatch):
        mod = self._creds(tmp_path, monkeypatch)
        hit = []
        monkeypatch.setattr(mod, "_call", lambda url, p, c: hit.append(url) or {"ret": 0})
        mod.check()
        assert any("appmsg" in u for u in hit), "必须打采集真正用的接口"
        assert any("searchbiz" in u for u in hit)

    def test_rate_limit_is_not_the_same_as_bad_credentials(self, tmp_path, monkeypatch):
        """限流时凭据是好的——报成「要换凭据」会让人白折腾一遍登录。"""
        mod = self._creds(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "_call", lambda url, p, c: {"ret": 200013})
        assert mod.check() == 1  # 1 = 受限，2 才是凭据失效

    def test_invalid_session_is_reported_as_fatal(self, tmp_path, monkeypatch):
        mod = self._creds(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "_call", lambda url, p, c: {"ret": 200003})
        assert mod.check() == 2

    def test_missing_credentials_file(self, tmp_path, monkeypatch):
        from sourcepilot.channels.wechat import check as check_mod

        monkeypatch.setattr(check_mod, "CREDENTIALS_FILE", tmp_path / "nope.yaml")
        assert check_mod.check() == 1
