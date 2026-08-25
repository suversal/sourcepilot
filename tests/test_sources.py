"""声明式引擎测试。全部离线——网络连通性不是这里要验证的东西。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FAKE_CONFIG_DICT, FAKE_PAYLOAD
from pydantic import ValidationError

from sourcepilot.contracts import Category, TimeBasis, UpstreamDown
from sourcepilot.settings import SOURCES_DIR
from sourcepilot.sources import SourceConfig, load_sources, normalize, rank_to_score
from sourcepilot.sources.engine import normalize_url
from sourcepilot.sources.extract import coerce, render_template, resolve_path


class TestExtract:
    def test_dotted_path(self):
        assert resolve_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_array_index(self):
        assert resolve_path({"a": [{"b": 2}]}, "a.0.b") == 2

    def test_empty_path_returns_root(self):
        assert resolve_path([1, 2], "") == [1, 2]

    def test_missing_path_is_none_not_raise(self):
        assert resolve_path({"a": 1}, "a.b.c") is None
        assert resolve_path({"a": 1}, "zzz") is None

    def test_template_fills_from_row(self):
        assert render_template("x/{id}", row={"id": 7}) == "x/7"

    def test_template_urlencodes(self):
        assert render_template("q={w|urlencode}", row={"w": "a b&c"}) == "q=a%20b%26c"

    def test_template_missing_slot_yields_none(self):
        """拼不出完整 URL 时返回 None，好过给出一个半截的坏链接。"""
        assert render_template("x/{id}", row={"other": 1}) is None

    def test_coerce_unix_seconds_and_millis(self):
        assert coerce(1784711789, "unix") == datetime(2026, 7, 22, 9, 16, 29, tzinfo=UTC)
        assert coerce(1784711789000, "unix") == datetime(2026, 7, 22, 9, 16, 29, tzinfo=UTC)

    def test_coerce_bad_value_is_none(self):
        assert coerce("不是数字", "int") is None
        assert coerce("", "str") is None


class TestNormalizeUrl:
    def test_strips_tracking_params(self):
        url = "https://www.toutiao.com/trending/123/?log_pb=%7B%7D&rank=&style_id=40132"
        assert normalize_url(url) == "https://www.toutiao.com/trending/123/"

    def test_keeps_meaningful_params(self):
        url = "https://example.com/s?q=AI&utm_source=x"
        assert normalize_url(url) == "https://example.com/s?q=AI"

    def test_untouched_when_no_query(self):
        assert normalize_url("https://example.com/a") == "https://example.com/a"


class TestRankToScore:
    def test_first_is_one_last_is_positive(self):
        assert rank_to_score(0, 10) == 1.0
        assert 0 < rank_to_score(9, 10) < 0.2

    def test_within_contract_bounds(self):
        assert all(0.0 <= rank_to_score(i, 50) <= 1.0 for i in range(50))

    def test_empty_list(self):
        assert rank_to_score(0, 0) == 0.0


class TestNormalize:
    def test_produces_items(self, fake_config):
        items = normalize(fake_config, FAKE_PAYLOAD)
        assert [i.id for i in items] == ["hotlist:fake_AAA111", "hotlist:fake_BBB222"]

    def test_dirty_row_skipped_not_fatal(self, fake_config):
        """单条脏数据不该让整个源挂掉——源级失败才走错误码。"""
        assert len(normalize(fake_config, FAKE_PAYLOAD)) == 2

    def test_template_url_and_nested_author(self, fake_config):
        first = normalize(fake_config, FAKE_PAYLOAD)[0]
        assert str(first.url) == "https://example.com/v/AAA111"
        assert first.author == "作者甲"

    def test_time_basis_follows_available_timestamp(self, fake_config):
        first, second = normalize(fake_config, FAKE_PAYLOAD)
        assert first.time_basis is TimeBasis.PUBLISHED and first.published_at is not None
        assert second.time_basis is TimeBasis.DISCOVERED and second.published_at is None

    def test_score_descends_with_rank(self, fake_config):
        items = normalize(fake_config, FAKE_PAYLOAD)
        assert items[0].score > items[1].score

    def test_unranked_source_gets_zero_score(self):
        """契约 §2：没有热度信号的源固定 0.0。

        按时间倒序的 RSS/快讯没有名次可言，硬套排名等于把「第几个被列出来」
        伪装成「有多热」，下游会当真。
        """
        cfg = SourceConfig(**{**FAKE_CONFIG_DICT, "ranked": False})
        items = normalize(cfg, FAKE_PAYLOAD)
        assert [i.score for i in items] == [0.0, 0.0]

    def test_unranked_source_still_keeps_raw_signal(self):
        """score 归零不等于把原始热度扔掉——下游想自己加权还得取得到。"""
        cfg = SourceConfig(**{**FAKE_CONFIG_DICT, "ranked": False})
        assert normalize(cfg, FAKE_PAYLOAD)[0].raw["score_raw"] == 5000

    def test_raw_keeps_original_hotness(self, fake_config):
        """契约 §2：原始热度值留在 raw 里，给下游自行加权。"""
        assert normalize(fake_config, FAKE_PAYLOAD)[0].raw == {"rank": 1, "score_raw": 5000}

    def test_keyword_categories_off_by_default(self, fake_config):
        """关键词分类默认关闭——错标签比空标签更有害，理由见 config/categories.yaml。"""
        assert normalize(fake_config, FAKE_PAYLOAD)[0].categories == []

    def test_source_level_categories_applied(self):
        cfg = SourceConfig(**{**FAKE_CONFIG_DICT, "categories": ["tip"]})
        assert all(Category.TIP in i.categories for i in normalize(cfg, FAKE_PAYLOAD))

    def test_wrong_list_path_raises_upstream_down(self, fake_config):
        """对方改版把列表挪走了——这是源级故障，要报出来而不是静悄悄返回空。"""
        with pytest.raises(UpstreamDown, match="没取到列表"):
            normalize(fake_config, {"data": {"items": []}})

    def test_all_rows_unusable_raises(self, fake_config):
        payload = {"data": {"list": [{"nope": 1}, {"nope": 2}]}}
        with pytest.raises(UpstreamDown, match="字段配置该改了"):
            normalize(fake_config, payload)

    def test_empty_list_is_not_an_error(self, fake_config):
        assert normalize(fake_config, {"data": {"list": []}}) == []


class TestConfigValidation:
    def test_field_needs_exactly_one_of_path_or_template(self):
        bad = {**FAKE_CONFIG_DICT}
        bad["extract"] = {
            **bad["extract"],
            "fields": {**bad["extract"]["fields"], "title": {}},
        }
        with pytest.raises(ValidationError, match="path / select / template"):
            SourceConfig(**bad)

    def test_required_fields_enforced(self):
        bad = {**FAKE_CONFIG_DICT}
        fields = {k: v for k, v in bad["extract"]["fields"].items() if k != "url"}
        bad["extract"] = {**bad["extract"], "fields": fields}
        with pytest.raises(ValidationError, match="缺必填项"):
            SourceConfig(**bad)

    def test_min_interval_floor(self):
        """自适应间隔最短 2 分钟——别把源抓崩了。"""
        with pytest.raises(ValidationError):
            SourceConfig(**{**FAKE_CONFIG_DICT, "min_interval": 30})

    def test_typo_in_config_is_rejected(self):
        with pytest.raises(ValidationError):
            SourceConfig(**{**FAKE_CONFIG_DICT, "min_intervall": 300})

    def test_hotlist_platform_defaults_to_name(self):
        cfg = SourceConfig(**{k: v for k, v in FAKE_CONFIG_DICT.items() if k != "platform"})
        assert cfg.platform == "fake"


class TestShippedConfigs:
    """仓库里的真实源配置必须能解析——挡住 YAML 手滑，不需要联网。"""

    def test_all_shipped_configs_parse(self):
        configs = load_sources(SOURCES_DIR)
        assert configs, "config/sources 下没有任何源配置"

    def test_platforms_are_unique(self):
        platforms = [c.platform for c in load_sources(SOURCES_DIR).values()]
        assert len(platforms) == len(set(platforms))

    def test_x_topic_names_are_the_persisted_chinese_identifiers(self):
        x = load_sources(SOURCES_DIR)["x"]
        assert [topic.name for topic in x.topics] == ["AI 热点", "U卡推荐", "eSIM推荐"]


class TestCategorizer:
    """分类只做确定性打标，且宁可不标也不错标。"""

    def test_keyword_rules_off_by_default(self, tmp_path):
        from sourcepilot.categorize import Categorizer

        c = Categorizer({"keyword_rules": {"model": {"keywords": ["gpt"]}}})
        assert c.classify(title="Launching Health in ChatGPT") == []

    def test_keyword_rules_apply_when_enabled(self):
        from sourcepilot.categorize import Categorizer

        c = Categorizer(
            {"keyword_rules_enabled": True, "keyword_rules": {"model": {"keywords": ["大模型"]}}}
        )
        assert [x.value for x in c.classify(title="某公司发布新大模型")] == ["model"]

    def test_source_rules_always_apply(self):
        """源级映射不受开关影响——那是「这个源只发这类内容」的事实，不是猜测。"""
        from sourcepilot.categorize import Categorizer

        c = Categorizer({"source_rules": {"arxiv": ["paper"]}})
        assert [x.value for x in c.classify(title="随便什么", source_keys=("arxiv",))] == ["paper"]


class TestBusinessStatusCode:
    """HTTP 200 不等于成功。

    很多站点用体内的码表示拒绝（B站 `code`、公众平台 `base_resp.ret`）。
    不看这个的话，「被风控挡了一下」会一路走到提取层、表现为「取不到列表」，
    然后被报成「多半是对方改版了」——那句话会把人带到完全错误的排查方向上。
    实测 B站就这样误报过一次。
    """

    def _config(self, **over):
        base = {
            **FAKE_CONFIG_DICT,
            "status": {
                "path": "code",
                "ok": [0],
                "message_path": "message",
                "rate_limited": [-352],
                "auth_expired": [-101],
                "captcha": [-900],
            },
        }
        return SourceConfig(**{**base, **over})

    def _fetch(self, monkeypatch, payload):
        import httpx

        from sourcepilot.sources.engine import fetch_raw

        monkeypatch.setattr(
            httpx.Client,
            "request",
            lambda self, m, url, **kw: httpx.Response(
                200, json=payload, request=httpx.Request("GET", url)
            ),
        )
        return fetch_raw(self._config())

    def test_success_code_passes_through(self, monkeypatch):
        payload = {**FAKE_PAYLOAD, "code": 0}
        assert self._fetch(monkeypatch, payload)["code"] == 0

    def test_rate_limit_code_becomes_rate_limited(self, monkeypatch):
        """限流该退避重试；报成「改版了」会让人白白去改配置。"""
        from sourcepilot.contracts import RateLimited

        with pytest.raises(RateLimited, match="-352"):
            self._fetch(monkeypatch, {"code": -352, "message": "请求过于频繁"})

    def test_auth_code_becomes_auth_expired(self, monkeypatch):
        from sourcepilot.contracts import AuthExpired

        with pytest.raises(AuthExpired):
            self._fetch(monkeypatch, {"code": -101, "message": "账号未登录"})

    def test_captcha_code_becomes_captcha(self, monkeypatch):
        from sourcepilot.contracts import Captcha

        with pytest.raises(Captcha):
            self._fetch(monkeypatch, {"code": -900})

    def test_unknown_code_falls_back_to_upstream_down(self, monkeypatch):
        with pytest.raises(UpstreamDown, match="-1"):
            self._fetch(monkeypatch, {"code": -1, "message": "未知错误"})

    def test_upstream_message_is_carried_into_the_error(self, monkeypatch):
        """把上游的原话带出来——排查时那句话往往比我们的猜测更有用。"""
        from sourcepilot.contracts import RateLimited

        with pytest.raises(RateLimited, match="请求过于频繁"):
            self._fetch(monkeypatch, {"code": -352, "message": "请求过于频繁"})

    def test_sources_without_status_spec_are_unaffected(self, monkeypatch):
        """没声明 status 的源照常走，不该因为这个特性改变行为。"""
        import httpx

        from sourcepilot.sources.engine import fetch_raw

        monkeypatch.setattr(
            httpx.Client,
            "request",
            lambda self, m, url, **kw: httpx.Response(
                200, json={**FAKE_PAYLOAD, "code": -352}, request=httpx.Request("GET", url)
            ),
        )
        assert fetch_raw(SourceConfig(**FAKE_CONFIG_DICT))["code"] == -352
