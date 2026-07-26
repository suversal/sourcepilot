"""X channel 测试。全部离线——账号池和签名在 CI 里没有真凭据。"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from sourcepilot.channels.cooldown import COOLDOWNS
from sourcepilot.channels.x import XRouter
from sourcepilot.channels.x.accounts import Account, AccountPool
from sourcepilot.channels.x.fxtwitter import FxTwitterBackend, tweet_to_item
from sourcepilot.channels.x.graphql import GraphQLBackend, walk_timeline
from sourcepilot.channels.x.nitter import NitterBackend
from sourcepilot.contracts import AuthExpired, RateLimited, SourceType, TimeBasis, UpstreamDown

NOW = datetime(2026, 7, 26, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset():
    COOLDOWNS.reset()
    yield
    COOLDOWNS.reset()


class TestAccountPool:
    def _pool(self, n=2):
        return AccountPool(
            [Account(name=f"a{i}", cookie=f"auth_token=x{i}; ct0=csrf{i}") for i in range(n)]
        )

    def test_csrf_extracted_from_cookie(self):
        """GraphQL 要求 x-csrf-token 等于 cookie 里的 ct0，不等就一定被拒。"""
        assert Account(name="a", cookie="foo=1; ct0=abc123; bar=2").csrf == "abc123"

    def test_user_agent_is_stable_per_account(self):
        """同一 session 里 UA 跳变本身就是可疑信号，所以用账号名做种子固定。

        不同账号撞同一个 UA 无所谓——真实世界里本来就大量用户共用 UA 串；
        要防的是同一个账号这次 Chrome、下次 Safari。
        """
        a = Account(name="alice", cookie="ct0=x")
        assert a.user_agent == Account(name="alice", cookie="ct0=y").user_agent
        assert a.user_agent == a.user_agent

    def test_repr_hides_cookie(self):
        assert "secret" not in repr(Account(name="a", cookie="ct0=secret"))

    def test_no_accounts_reports_not_configured(self):
        with pytest.raises(AuthExpired, match="未配置"):
            AccountPool([]).acquire("SearchTimeline")

    def test_all_dead_reports_auth_expired(self):
        pool = self._pool()
        for a in pool.accounts:
            a.active = False
        with pytest.raises(AuthExpired, match="失效"):
            pool.acquire("SearchTimeline")

    def test_all_locked_reports_rate_limited_not_auth(self):
        """全被限流和全被封是两回事——前者等一会儿能好，后者要换号。"""
        pool = self._pool()
        for a in pool.accounts:
            a.locked_until["SearchTimeline"] = 9e9
        with pytest.raises(RateLimited):
            pool.acquire("SearchTimeline")

    def test_rate_limit_is_per_endpoint(self):
        """搜索被限不代表时间线也被限，不该整个账号一起锁死。"""
        pool = self._pool(1)
        pool.accounts[0].locked_until["SearchTimeline"] = 9e9
        assert pool.acquire("UserTweets").name == "a0"


class TestRateLimitStateMachine:
    """区分「临时限流」和「账号废了」——搞混的代价不对称。"""

    def _resp(self, status=200, headers=None, json_body=None, text=""):
        return httpx.Response(
            status,
            headers=headers or {},
            json=json_body if json_body is not None else {},
            request=httpx.Request("GET", "https://x.com/i/api/graphql/x/Y"),
        ) if json_body is not None else httpx.Response(
            status, headers=headers or {}, text=text,
            request=httpx.Request("GET", "https://x.com/i/api/graphql/x/Y"),
        )

    def test_rate_limit_locks_but_keeps_account_active(self):
        pool = AccountPool([Account(name="a", cookie="ct0=x")])
        account = pool.accounts[0]
        resp = self._resp(headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": "99999"})
        with pytest.raises(RateLimited):
            pool.classify(account, "SearchTimeline", resp)
        assert account.active is True, "限流是临时的，账号还能用"
        assert account.locked_until["SearchTimeline"] == 99999

    @pytest.mark.parametrize("code", [32, 64, 88, 89, 326])
    def test_fatal_codes_deactivate_the_account(self, code):
        """这些码代表账号本身废了——继续用它只会加速关联封号。"""
        pool = AccountPool([Account(name="a", cookie="ct0=x")])
        account = pool.accounts[0]
        resp = self._resp(json_body={"errors": [{"code": code}]})
        with pytest.raises(AuthExpired):
            pool.classify(account, "SearchTimeline", resp)
        assert account.active is False

    def test_cloudflare_html_deactivates(self):
        pool = AccountPool([Account(name="a", cookie="ct0=x")])
        account = pool.accounts[0]
        resp = self._resp(headers={"cf-ray": "abc"}, text="<html>blocked</html>")
        with pytest.raises(AuthExpired):
            pool.classify(account, "SearchTimeline", resp)
        assert account.active is False

    def test_healthy_response_changes_nothing(self):
        pool = AccountPool([Account(name="a", cookie="ct0=x")])
        account = pool.accounts[0]
        pool.classify(account, "SearchTimeline", self._resp(json_body={"data": {}}))
        assert account.active is True and not account.locked_until


class TestFxTwitter:
    TWEET = {
        "id": "123",
        "text": "OpenAI ships something " * 5,
        "author": {"screen_name": "OpenAI"},
        "created_timestamp": 1784711789,
        "likes": 900,
        "retweets": 50,
        "replies": 0,
        "lang": "en",
        "media": {"photos": [{"url": "https://pbs.twimg.com/a.jpg"}]},
    }

    def test_maps_to_item(self):
        item = tweet_to_item(self.TWEET, NOW)
        assert item.id == "x:123"
        assert item.source.type is SourceType.X
        assert item.author == "OpenAI"
        assert item.time_basis is TimeBasis.PUBLISHED
        assert len(item.media) == 1

    def test_title_is_truncated_body(self):
        """推文没有标题，契约约定取正文前 80 字符。"""
        item = tweet_to_item(self.TWEET, NOW)
        assert len(item.title) == 80
        assert item.summary is not None, "正文比标题长时要留全文"

    def test_score_reflects_engagement_not_rank(self):
        """契约 §2：X 的 score 由互动量算，不是列表位置。"""
        hot = tweet_to_item({**self.TWEET, "likes": 100000}, NOW)
        cold = tweet_to_item({**self.TWEET, "likes": 1, "retweets": 0}, NOW)
        assert hot.score > cold.score
        assert 0.0 <= cold.score <= hot.score <= 1.0

    def test_missing_author_is_skipped(self):
        assert tweet_to_item({**self.TWEET, "author": {}}, NOW) is None

    def test_html_response_is_not_found(self, monkeypatch):
        """FxTwitter 对认不出的地址会回 HTML 而不是 JSON。"""
        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200, text="<html></html>", headers={"content-type": "text/html"},
                request=httpx.Request("GET", url),
            ),
        )
        with pytest.raises(Exception, match="不是 JSON"):
            FxTwitterBackend().fetch_tweet("OpenAI", "1")


class TestNitterFailover:
    RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Hello world from OpenAI</title>
        <link>https://nitter.net/OpenAI/status/999#m</link>
        <pubDate>Fri, 24 Jul 2026 16:13:35 GMT</pubDate>
        <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">@OpenAI</dc:creator>
      </item></channel></rss>"""

    def test_falls_through_to_next_instance(self, monkeypatch):
        """公共实例寿命很短，卡在第一个上等于整条路断了。"""
        seen: list[str] = []

        def fake_get(self, url, **kw):
            seen.append(url)
            if "dead" in url:
                return httpx.Response(502, request=httpx.Request("GET", url))
            return httpx.Response(
                200, text=TestNitterFailover.RSS, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        backend = NitterBackend(["https://dead.example", "https://alive.example"])
        items = backend.fetch_timeline("OpenAI", 5)
        assert len(seen) == 2 and len(items) == 1

    def test_url_points_back_to_x_not_nitter(self):
        """契约要求 url 是第三方原文——不能留 Nitter 的镜像地址。"""
        import feedparser

        from sourcepilot.channels.x.nitter import _entry_to_item

        entry = feedparser.parse(self.RSS).entries[0]
        item = _entry_to_item(entry, "OpenAI", NOW)
        assert str(item.url) == "https://x.com/OpenAI/status/999"
        assert "nitter" not in str(item.url)

    def test_no_signal_means_zero_score(self):
        """Nitter 的 RSS 不带互动数，没有热度信号就老实给 0。"""
        import feedparser

        from sourcepilot.channels.x.nitter import _entry_to_item

        entry = feedparser.parse(self.RSS).entries[0]
        assert _entry_to_item(entry, "OpenAI", NOW).score == 0.0

    def test_whitelist_placeholder_is_treated_as_unusable(self, monkeypatch):
        """xcancel 这类要 RSS 客户端备案的，对我们等同不可用。"""
        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200, text="RSS reader not yet whitelisted!", request=httpx.Request("GET", url)
            ),
        )
        with pytest.raises(UpstreamDown, match="都不可用"):
            NitterBackend(["https://x1.example"]).fetch_timeline("OpenAI", 5)


class TestGraphQLParsing:
    def _entry(self, tweet_id="555", text="hello"):
        return {
            "entryId": f"tweet-{tweet_id}",
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "rest_id": tweet_id,
                            "core": {
                                "user_results": {
                                    "result": {"legacy": {"screen_name": "OpenAI"}}
                                }
                            },
                            "legacy": {
                                "id_str": tweet_id,
                                "full_text": text,
                                "created_at": "Wed Jul 22 13:00:00 +0000 2026",
                                "favorite_count": 10,
                                "retweet_count": 2,
                                "reply_count": 1,
                                "lang": "en",
                            },
                        }
                    }
                }
            },
        }

    def _payload(self, entries):
        return {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {"timeline": {"instructions": [{"entries": entries}]}}
                }
            }
        }

    def test_extracts_tweets_and_cursor(self):
        cursor_entry = {
            "entryId": "cursor-bottom-1",
            "content": {"cursorType": "Bottom", "value": "NEXT"},
        }
        items, cursor = walk_timeline(self._payload([self._entry(), cursor_entry]), NOW)
        assert [i.id for i in items] == ["x:555"]
        assert cursor == "NEXT"

    def test_unknown_entry_types_are_skipped_not_fatal(self):
        """X 随时会加新条目类型，为此整条崩掉不值得。"""
        weird = {"entryId": "who-knows-1", "content": {"somethingNew": {}}}
        items, _ = walk_timeline(self._payload([weird, self._entry()]), NOW)
        assert len(items) == 1

    def test_long_tweet_uses_note_text_not_truncated_full_text(self):
        entry = self._entry(text="截断的正文…")
        result = entry["content"]["itemContent"]["tweet_results"]["result"]
        result["note_tweet"] = {"note_tweet_results": {"result": {"text": "完整长推正文" * 20}}}
        items, _ = walk_timeline(self._payload([entry]), NOW)
        assert "截断" not in (items[0].summary or "")

    def test_no_accounts_means_unavailable(self):
        assert GraphQLBackend(pool=AccountPool([])).available() is False


class TestRouter:
    def test_search_has_only_graphql(self):
        """实测免登录搜索已全部关闭，所以搜索没有降级路径——要说清楚而不是静默返回空。"""
        router = XRouter()
        router.graphql.pool = AccountPool([])
        with pytest.raises(Exception, match="没有可用后端"):
            router.search("test", 5)

    def test_timeline_prefers_zero_auth_backend(self, monkeypatch):
        """账号是稀缺且脆弱的资源，能不用就不用。"""
        router = XRouter()
        monkeypatch.setattr(
            NitterBackend, "fetch_timeline", lambda self, h, n: ["来自 nitter"]
        )
        monkeypatch.setattr(
            GraphQLBackend, "available", lambda self: True
        )
        monkeypatch.setattr(
            GraphQLBackend,
            "user_id",
            lambda self, h: pytest.fail("Nitter 可用时不该动用账号"),
        )
        items, _ = router.timeline("OpenAI", 5)
        assert items == ["来自 nitter"]


class TestSignatureRequirement:
    """X 对 operation 分化地强制 x-client-transaction-id（2026-07-26 实测）。

    在真实登录态浏览器里逐个对照过：
        UserByScreenName / UserTweets / UserMedia  不带签名 → 200
        SearchTimeline                             不带签名 → 404
                                                   带截获的签名重放 → 仍 404
    最后一条说明签名是一次性的，截获复用无效。
    """

    def _backend(self, signer=None):
        return GraphQLBackend(
            pool=AccountPool([Account(name="a", cookie="auth_token=x; ct0=c")]),
            transaction_signer=signer,
        )

    def test_search_without_signer_fails_fast_with_a_clear_reason(self):
        """与其发出去等一个语焉不详的 404，不如直接说清楚缺什么。"""
        with pytest.raises(AuthExpired, match="x-client-transaction-id"):
            self._backend().search("test", 5)

    def test_timeline_operations_do_not_require_a_signer(self, monkeypatch):
        """时间线不需要签名——实测可用，别因为搜索的限制把它一起挡了。"""
        captured = {}

        def fake_get(self, url, **kw):
            captured["url"] = url
            return httpx.Response(
                200,
                json={"data": {"user": {"result": {"rest_id": "42"}}}},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        assert self._backend().user_id("OpenAI") == "42"
        assert "UserByScreenName" in captured["url"]


class TestTransactionSignature:
    """签名算法本身可离线验证——它是纯函数，只有取密钥那步要联网。"""

    def _signer(self):
        from sourcepilot.channels.x.signature import XTransactionSigner

        return XTransactionSigner(vk_bytes=list(range(48)), anim_key="abc123")

    def test_signature_is_base64_without_padding(self):
        sig = self._signer().sign("GET", "/i/api/graphql/x/SearchTimeline")
        assert not sig.endswith("=")
        import base64

        base64.b64decode(sig + "=" * (-len(sig) % 4))  # 能解回来说明是合法 base64

    def test_output_varies_between_calls(self):
        """带时间戳与随机噪声——X 靠这个防重放，实测截获重放确实会 404。

        断言「多次调用会产生多个不同结果」，而不是「N 次全不同」：混淆字节只有
        256 种取值，同一秒内连发几次本来就可能撞上。要求全不同会让这条测试
        偶发失败——一条会无故变红的测试比没有测试更糟，它会让人开始无视失败。
        """
        s = self._signer()
        sigs = {s.sign("GET", "/i/api/graphql/x/SearchTimeline") for _ in range(20)}
        assert len(sigs) > 1

    def test_signature_is_bound_to_method_and_path(self):
        """签名绑方法与路径，所以截一个换个端点用也没用。"""
        s = self._signer()
        import base64

        def payload(sig):
            raw = base64.b64decode(sig + "=" * (-len(sig) % 4))
            noise = raw[0]
            return bytes(b ^ noise for b in raw[1:])

        a = payload(s.sign("GET", "/a"))
        b = payload(s.sign("GET", "/b"))
        # 前 48 字节是 vk_bytes，之后 4 字节时间戳，再往后是路径参与的哈希
        assert a[:48] == b[:48]
        assert a[52:68] != b[52:68], "路径不同，哈希段就该不同"

    def test_anonymous_load_is_rejected_with_a_clear_reason(self):
        """匿名态的 bundle 里没有签名脚本，早点说清楚好过失败在更深处。"""
        from sourcepilot.channels.x.signature import (
            SignatureUnavailable,
            XTransactionSigner,
        )

        with pytest.raises(SignatureUnavailable, match="cookie"):
            XTransactionSigner.load("")

    def test_cubic_curve_endpoints(self):
        from sourcepilot.channels.x.signature import CubicCurve

        c = CubicCurve([0.25, 0.1, 0.25, 1.0])
        assert c.value_at(0.0) == pytest.approx(0.0, abs=1e-6)
        assert 0.0 < c.value_at(0.5) < 1.5


class TestSignatureAgainstRealData:
    """用 2026-07-26 从真实页面取到的数据固化算法。

    这组输入取自登录态的 x.com，用它算出的签名打真实 SearchTimeline 拿到了
    200 / 20 条推文。算法一旦被"优化"坏，这里就会红。
    """

    #: 真实 verification key（48 字节）。它每次请求都会变，这里只作算法回归用。
    VK = [
        205, 204, 105, 236, 206, 244, 10, 67, 214, 164, 190, 240, 120, 172, 246, 101,
        119, 13, 53, 78, 93, 195, 65, 107, 249, 66, 136, 185, 208, 166, 28, 81,
        143, 69, 187, 69, 225, 86, 166, 189, 68, 154, 118, 177, 83, 33, 185, 167,
    ]
    FRAME = [81.0, 61.0, 19.0, 90.0, 160.0, 44.0, 220.0, 235.0, 156.0, 224.0, 79.0]

    def test_anim_key_matches_independent_implementation(self):
        """同一份真实输入，独立写的 JS 实现算出的也是这个值。"""
        from sourcepilot.channels.x.signature import calc_anim_key

        assert calc_anim_key(self.FRAME, 0.0) == "513d13100100"

    def test_another_real_frame(self):
        """另一次抓取的真实帧，JS 侧同样算出 72a3c100100。"""
        from sourcepilot.channels.x.signature import calc_anim_key

        frame = [114.0, 10.0, 60.0, 84.0, 219.0, 238.0, 52.0, 63.0, 64.0, 41.0, 143.0]
        assert calc_anim_key(frame, 0.0) == "72a3c100100"

    def test_signature_payload_layout(self):
        """签名体的布局必须是 vk(48) + 时间戳(4) + 哈希前 16 + 尾字节，共 69 字节。"""
        import base64

        from sourcepilot.channels.x.signature import XTransactionSigner

        sig = XTransactionSigner(self.VK, "513d13100100").sign("GET", "/i/api/graphql/x/Y")
        raw = base64.b64decode(sig + "=" * (-len(sig) % 4))
        noise, body = raw[0], bytes(b ^ raw[0] for b in raw[1:])
        assert len(body) == 48 + 4 + 16 + 1
        assert list(body[:48]) == self.VK
        assert body[-1] == 3
        assert 0 <= noise <= 255
