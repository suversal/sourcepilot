"""微信读书后端测试。全部离线——这条线需要登录态，CI 里不可能有真凭据。

响应形状照 2026-08-06 核对的真实结构：
`reviews[].subReviews[].review.mpInfo{title, originalId, cover, digest}`。
"""

from __future__ import annotations

import pytest

from sourcepilot.channels.wechat import COOLDOWNS, WereadBackend, collect_wechat
from sourcepilot.channels.wechat.weread import (
    ERR_CONTEXT_REQUIRED,
    ERR_NO_SUCH_USER,
    WereadClient,
    WereadCredentials,
    _flatten_reviews,
    book_id_for,
)
from sourcepilot.contracts import (
    AuthExpired,
    RateLimited,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from sourcepilot.sources import SourceConfig

QBITAI_FAKEID = "MzIzNjc1NzUzMw=="
QBITAI_BOOK_ID = "MP_WXS_3236757533"
READER_URL = "https://weread.qq.com/web/mp/reader/abc123hash"

CONFIG = {
    "name": "wechat",
    "display_name": "微信公众号",
    "type": "wechat",
    "platform": "wechat",
    "channel": "wechat",
    "min_interval": 21600,
    "accounts": [{"name": "量子位", "fakeid": QBITAI_FAKEID}],
    "per_account_limit": 5,
    "account_interval": 0,
    "backends": ["weread"],
}

SHELF_PAYLOAD = {
    "books": [
        {
            "bookId": QBITAI_BOOK_ID,
            "title": "量子位",
            "deepLink": "weread://book-detail?type=1&v=abc123hash",
        },
        # 书架里还有真的书，必须被滤掉
        {"bookId": "3000342875", "title": "某本物理书", "deepLink": "weread://x?v=zzz"},
    ]
}


def _articles_payload(*groups):
    return {"reviews": list(groups)}


def _group(*articles, created=1754400000):
    return {
        "createTime": created,
        "subReviews": [
            {
                "review": {
                    "reviewId": f"r{i}",
                    "createTime": a.get("t", created),
                    "mpInfo": {
                        "title": a["title"],
                        "originalId": a["oid"],
                        "cover": a.get("cover"),
                        "digest": a.get("digest"),
                    },
                }
            }
            for i, a in enumerate(articles)
        ],
    }


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    COOLDOWNS.reset()
    yield
    COOLDOWNS.reset()


@pytest.fixture
def creds(monkeypatch, tmp_path):
    path = tmp_path / "weread.yaml"
    path.write_text("cookie: 'wr_vid=1; wr_skey=abc'\n", encoding="utf-8")
    monkeypatch.setattr("sourcepilot.channels.wechat.weread.CREDENTIALS_FILE", path)
    return path


def _stub_http(monkeypatch, router):
    """按 URL 分流的假响应。router: url 片段 -> payload 或 callable。"""
    import httpx

    seen: list[tuple[str, dict]] = []

    def fake_get(self, url, **kw):
        seen.append((url, dict(kw.get("headers") or {})))
        for fragment, payload in router.items():
            if fragment in url:
                if callable(payload):
                    payload = payload()
                if isinstance(payload, str):  # 非 JSON = 风控白屏
                    return httpx.Response(
                        200, text=payload, request=httpx.Request("GET", url)
                    )
                return httpx.Response(
                    200, json=payload, request=httpx.Request("GET", url)
                )
        raise AssertionError(f"未预期的请求：{url}")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    return seen


class TestBookId:
    def test_derives_book_id_from_fakeid(self):
        """fakeid 就是 __biz，bookId = MP_WXS_ + base64 解码。"""
        assert book_id_for(QBITAI_FAKEID) == QBITAI_BOOK_ID

    def test_rejects_garbage_instead_of_guessing(self):
        """解不出来宁可跳过，也不要拿乱拼的 bookId 去请求。"""
        assert book_id_for("not-base64!!") is None
        assert book_id_for("aGVsbG8=") is None  # 解出来是 "hello"，不是数字


class TestCredentials:
    def test_missing_file_returns_none(self, tmp_path):
        assert WereadCredentials.load(tmp_path / "nope.yaml") is None

    def test_empty_cookie_returns_none(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("cookie: ''\n", encoding="utf-8")
        assert WereadCredentials.load(path) is None

    def test_repr_hides_cookie(self):
        assert "secret" not in repr(WereadCredentials("wr_skey=secret"))

    def test_detects_cookie_without_the_auth_keys(self):
        """只有埋点键的 cookie 要当场认出来。

        2026-08-07 踩过：复制到的整串里只有 `_qimei_*`/`_clck` 这些腾讯通用埋点，
        `wr_*` 一个没有。这种 cookie 能过书架那道浅校验，却在拉文章时回
        -2010/-2041——看着像账号被风控，实际只是复制得不全。
        """
        junk = "_qimei_q36=x; _clck=y; pgv_pvid=z"
        assert WereadCredentials(junk).missing_keys() == ["wr_vid", "wr_skey"]

    def test_complete_cookie_reports_nothing_missing(self):
        assert WereadCredentials("wr_vid=1; wr_skey=abc; wr_rt=z").missing_keys() == []

    def test_empty_value_counts_as_missing(self):
        assert "wr_skey" in WereadCredentials("wr_vid=1; wr_skey=").missing_keys()


class TestOriginalId:
    """微信读书把文章 id 里的下划线换成了 `~`，不换回来链接打不开。"""

    def test_tilde_becomes_underscore(self):
        from sourcepilot.channels.wechat.weread import normalize_original_id

        # 实测 2026-08-06：带 ~ 的直接请求回「参数错误」，换成 _ 后正常打开
        assert normalize_original_id("XK6ymJL7y0vo~GQXxmpuBA") == "XK6ymJL7y0vo_GQXxmpuBA"
        assert normalize_original_id("U5fnTRW4cGvXYJER~~YBiw") == "U5fnTRW4cGvXYJER__YBiw"

    def test_hyphens_are_left_alone(self):
        """`-` 在真实 id 里本来就有，跟着一起替换会把好 id 改坏。"""
        from sourcepilot.channels.wechat.weread import normalize_original_id

        assert normalize_original_id("nL--rVri3qAy~6Recsg~4g") == "nL--rVri3qAy_6Recsg_4g"

    def test_applied_when_building_items(self):
        payload = _articles_payload(_group({"title": "t", "oid": "aa~bb"}))
        assert _flatten_reviews(payload)[0]["original_id"] == "aa_bb"


class TestFlatten:
    def test_expands_every_sub_review(self):
        """一次群发 = 一个 reviews 条目，subReviews 才是一篇篇文章。

        只读 subReviews[0] 会丢掉同一次群发的其余文章——有的号一天发 3-4 篇。
        """
        payload = _articles_payload(
            _group(
                {"title": "第一篇", "oid": "aaa"},
                {"title": "第二篇", "oid": "bbb"},
                {"title": "第三篇", "oid": "ccc"},
            )
        )
        assert len(_flatten_reviews(payload)) == 3

    def test_sorts_newest_first(self):
        payload = _articles_payload(
            _group({"title": "旧", "oid": "a", "t": 1000}),
            _group({"title": "新", "oid": "b", "t": 2000}),
        )
        assert [a["title"] for a in _flatten_reviews(payload)] == ["新", "旧"]

    def test_skips_entries_without_title_or_id(self):
        payload = {
            "reviews": [
                {"subReviews": [{"review": {"mpInfo": {"title": "有标题没 id"}}}]},
                {"subReviews": [{"review": {"mpInfo": {"originalId": "x"}}}]},
                {"subReviews": [{"review": {}}]},
            ]
        }
        assert _flatten_reviews(payload) == []


class TestClient:
    def test_shelf_keeps_only_official_accounts(self, monkeypatch):
        _stub_http(monkeypatch, {"/web/shelf/sync": SHELF_PAYLOAD})
        shelf = WereadClient(WereadCredentials("c")).shelf()
        assert shelf == {QBITAI_BOOK_ID: READER_URL}

    def test_shelf_is_fetched_once_per_client(self, monkeypatch):
        """整轮采集只同步一次书架——每个请求都在消耗反爬额度。"""
        seen = _stub_http(monkeypatch, {"/web/shelf/sync": SHELF_PAYLOAD})
        client = WereadClient(WereadCredentials("c"))
        client.shelf()
        client.shelf()
        assert len(seen) == 1

    def test_articles_request_carries_reader_referer(self, monkeypatch):
        """没有阅读器页 Referer 一定拿 -2041，这是上下文校验不是限流。"""
        seen = _stub_http(monkeypatch, {"/web/mp/articles": _articles_payload()})
        WereadClient(WereadCredentials("c")).articles(QBITAI_BOOK_ID, READER_URL)
        _, headers = seen[0]
        assert headers["Referer"] == READER_URL

    def test_login_expired_does_not_leak_account_details(self, monkeypatch):
        """契约 §5：对外只说平台侧不可用。"""
        _stub_http(monkeypatch, {"/web/shelf/sync": {"errCode": ERR_NO_SUCH_USER}})
        with pytest.raises(AuthExpired) as exc:
            WereadClient(WereadCredentials("c")).shelf()
        assert "cookie" not in exc.value.message.lower()

    def test_login_timeout_is_auth_expired_not_upstream_down(self, monkeypatch):
        """-2012「登录超时」= cookie 过期，必须报 AUTH_EXPIRED。

        报成 UPSTREAM_DOWN 有两个后果：运维以为是微信读书挂了而不是自己该重新
        登录；更要命的是 UPSTREAM_DOWN 不属于 BACKEND_LEVEL_FAILURES，不会冷却
        后端——于是 23 个号各自重试一次书架，把有反爬的接口打 23 遍。
        """
        from sourcepilot.channels.wechat.weread import ERR_LOGIN_TIMEOUT

        _stub_http(
            monkeypatch,
            {"/web/shelf/sync": {"errCode": ERR_LOGIN_TIMEOUT, "errMsg": "登录超时"}},
        )
        with pytest.raises(AuthExpired):
            WereadClient(WereadCredentials("c")).shelf()

    def test_shelf_failure_is_remembered_not_retried_per_account(self, monkeypatch):
        """书架失败要记住。否则一次抖动会被 23 个号放大成 23 次请求。"""
        from sourcepilot.channels.wechat.weread import ERR_LOGIN_TIMEOUT

        seen = _stub_http(
            monkeypatch, {"/web/shelf/sync": {"errCode": ERR_LOGIN_TIMEOUT}}
        )
        client = WereadClient(WereadCredentials("c"))
        for _ in range(3):
            with pytest.raises(AuthExpired):
                client.shelf()
        assert len(seen) == 1

    def test_context_error_is_upstream_not_auth(self, monkeypatch):
        """-2041 是阅读器地址过期，重同步书架就能好——不该报成凭据失效让人去重扫码。"""
        _stub_http(
            monkeypatch, {"/web/mp/articles": {"errCode": ERR_CONTEXT_REQUIRED}}
        )
        with pytest.raises(UpstreamDown) as exc:
            WereadClient(WereadCredentials("c")).articles(QBITAI_BOOK_ID, READER_URL)
        assert "-2041" in exc.value.message

    def test_html_response_means_rate_limited_not_broken_parser(self, monkeypatch):
        """触发风控时返回的是 HTML 白屏页。报 RateLimited 让冷却退避，
        而不是报「多半是对方改版了」把人引去改解析规则。"""
        _stub_http(monkeypatch, {"/web/shelf/sync": "<html>验证</html>"})
        with pytest.raises(RateLimited) as exc:
            WereadClient(WereadCredentials("c")).shelf()
        assert "风控" in exc.value.message


class TestBackend:
    def test_normalizes_articles(self, creds, monkeypatch):
        _stub_http(
            monkeypatch,
            {
                "/web/shelf/sync": SHELF_PAYLOAD,
                "/web/mp/articles": _articles_payload(
                    _group({"title": "GLM 发布", "oid": "AbC123", "digest": "摘要"})
                ),
            },
        )
        items = collect_wechat(SourceConfig(**CONFIG))
        assert len(items) == 1
        item = items[0]
        assert item.source.type is SourceType.WECHAT
        assert item.source.platform == "量子位"
        assert item.title == "GLM 发布"
        assert item.summary == "摘要"
        assert str(item.url).startswith("https://mp.weixin.qq.com/s/AbC123")
        assert item.time_basis is TimeBasis.PUBLISHED
        # 公众号是时间流不是排行榜，没有热度信号（契约 §2）
        assert item.score == 0.0

    def test_id_uses_original_id_so_backends_agree(self, creds, monkeypatch):
        """同一篇文章无论从 weread 还是 mp 进来，native_id 都该是永久链接那一段，
        否则换后端会把库里的东西全变成「新条目」重来一遍。"""
        _stub_http(
            monkeypatch,
            {
                "/web/shelf/sync": SHELF_PAYLOAD,
                "/web/mp/articles": _articles_payload(
                    _group({"title": "t", "oid": "PermaId"})
                ),
            },
        )
        items = collect_wechat(SourceConfig(**CONFIG))
        assert items[0].id == "wechat:量子位_PermaId"

    def test_fetches_accounts_that_are_not_on_the_shelf(self, creds, monkeypatch):
        """要订阅的号**不必**加进书架。

        -2041 那道校验只认「Referer 是不是合法阅读器页」，不比对 bookId——实测
        拿书架里某个 Kimi 号的阅读器页去拉根本不在书架里的量子位，返回 77 篇。
        所以书架只用来换一张通行证，不是每个号的准入名单。
        """
        other_book = {
            "books": [
                {
                    "bookId": "MP_WXS_9999999999",  # 跟配置里的号完全无关
                    "title": "随便什么号",
                    "deepLink": "weread://book-detail?type=1&v=ticket_hash",
                }
            ]
        }
        seen = _stub_http(
            monkeypatch,
            {
                "/web/shelf/sync": other_book,
                "/web/mp/articles": _articles_payload(
                    _group({"title": "拉到了", "oid": "x1"})
                ),
            },
        )
        items = collect_wechat(SourceConfig(**CONFIG))
        assert len(items) == 1
        articles_call = next(h for u, h in seen if "/web/mp/articles" in u)
        assert articles_call["Referer"].endswith("ticket_hash")

    def test_empty_shelf_says_how_to_get_a_ticket(self, creds, monkeypatch):
        """一个公众号都没有就换不到通行证——要说清楚怎么补，别静默返回空。"""
        _stub_http(monkeypatch, {"/web/shelf/sync": {"books": []}})
        # channel 会把后端错误吞成「所有后端都失败」，这里直接验客户端层
        from sourcepilot.channels.wechat.weread import WereadClient, WereadCredentials

        with pytest.raises(UpstreamDown) as exc:
            WereadClient(WereadCredentials("c")).reader_ticket()
        assert "在微信读书中阅读" in exc.value.message

    def test_account_without_fakeid_is_skipped(self, creds, monkeypatch):
        """微信读书搜不到公众号名，没 fakeid 就定位不了。"""
        _stub_http(monkeypatch, {"/web/shelf/sync": SHELF_PAYLOAD})
        config = SourceConfig(**{**CONFIG, "accounts": ["某个没配 fakeid 的号"]})
        assert collect_wechat(config) == []

    def test_respects_per_account_limit(self, creds, monkeypatch):
        _stub_http(
            monkeypatch,
            {
                "/web/shelf/sync": SHELF_PAYLOAD,
                "/web/mp/articles": _articles_payload(
                    _group(*[{"title": f"t{i}", "oid": f"o{i}"} for i in range(20)])
                ),
            },
        )
        items = collect_wechat(SourceConfig(**{**CONFIG, "per_account_limit": 5}))
        assert len(items) == 5

    def test_account_interval_comes_from_config(self):
        """weread 有反爬，间隔必须能从配置调——之前 account_interval 这个字段
        声明了却没有任何后端读它。"""
        from sourcepilot.channels.wechat import _build

        (backend,) = _build(["weread"], account_interval=7.5)
        assert isinstance(backend, WereadBackend)
        assert backend.account_interval == 7.5

    def test_without_credentials_is_a_config_state_not_a_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "sourcepilot.channels.wechat.weread.CREDENTIALS_FILE",
            tmp_path / "absent.yaml",
        )
        assert collect_wechat(SourceConfig(**CONFIG)) == []


class TestWiring:
    def test_weread_is_the_default_primary_backend(self):
        """mp 的跨号列表被微信关了，默认顺序必须把 weread 放前面，
        否则每轮先白撞一次 200013。"""
        import inspect

        from sourcepilot.channels import wechat

        source = inspect.getsource(wechat.collect_wechat)
        assert '["weread", "mp"]' in source

    def test_config_uses_weread_and_a_conservative_interval(self):
        import yaml

        from sourcepilot.settings import PROJECT_ROOT

        config = yaml.safe_load(
            (PROJECT_ROOT / "config" / "sources" / "wechat.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert config["enabled"] is True
        assert config["backends"] == ["weread"]
        # 一轮 24 个请求，一小时一轮会把反爬额度打爆
        assert config["min_interval"] >= 21600

    def test_credentials_file_is_gitignored(self):
        from sourcepilot.settings import PROJECT_ROOT

        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "config/weread_credentials.yaml" in ignored
