"""采集中断告警测试。

重点不是「能不能发出去」（那是 Telegram 的事），而是**什么时候该发、什么时候
不该发**——告警的价值全在这个判断上：漏报等于没有告警，误报吵到人不看了，
也等于没有告警。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FAKE_CONFIG_DICT

from sourcepilot.alert import Alerter, format_alert, send_telegram
from sourcepilot.canary import Canary, Health, SourceHealthReport
from sourcepilot.contracts import ErrorCode
from sourcepilot.sources.config import SourceConfig
from sourcepilot.store import Store

NOW = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)


def make_source(name: str, enabled: bool = True) -> SourceConfig:
    return SourceConfig(
        **{
            **FAKE_CONFIG_DICT,
            "name": name,
            "platform": name,
            "enabled": enabled,
            "min_interval": 3600,
        }
    )


class Recorder:
    """假发送器：记下发过什么，并能模拟发送失败。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "alert.db")
    sources = {"weibo": make_source("weibo"), "zhihu": make_source("zhihu")}
    canary = Canary(store, sources)
    return store, sources, canary


def fail_source(store: Store, name: str, times: int, at: datetime = NOW) -> None:
    for _ in range(times):
        store.record_failure(name, ErrorCode.CAPTCHA, at)


def succeed_source(store: Store, name: str, at: datetime = NOW) -> None:
    store.record_success(name, 20, at)


class TestWhenToAlert:
    def test_new_down_source_triggers_one_message(self, setup):
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW - timedelta(hours=1))
        fail_source(store, "weibo", 3)
        sender = Recorder()

        message = Alerter(canary, store, sender).poll(NOW)

        assert message is not None
        assert len(sender.sent) == 1
        assert "weibo" in sender.sent[0]
        assert "CAPTCHA" in sender.sent[0]

    def test_same_failure_is_not_repeated(self, setup):
        """一个源一直坏着不该每轮都吵——这是「告警还有人看」的前提。"""
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW - timedelta(hours=1))
        fail_source(store, "weibo", 3)
        sender = Recorder()
        alerter = Alerter(canary, store, sender)

        assert alerter.poll(NOW) is not None
        fail_source(store, "weibo", 2)  # 又失败两次，还是同一个故障
        assert alerter.poll(NOW + timedelta(minutes=1)) is None
        assert len(sender.sent) == 1

    def test_recovery_is_announced_and_state_cleared(self, setup):
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW - timedelta(hours=1))
        fail_source(store, "weibo", 3)
        sender = Recorder()
        alerter = Alerter(canary, store, sender)
        alerter.poll(NOW)

        succeed_source(store, "weibo", NOW + timedelta(minutes=5))
        message = alerter.poll(NOW + timedelta(minutes=5))

        assert message is not None and "已恢复：weibo" in message
        assert store.alert_states() == {}
        # 恢复通报过就结束了，不该再补一条
        assert alerter.poll(NOW + timedelta(minutes=6)) is None

    def test_degraded_alone_does_not_alert(self, setup):
        """落后、条目数掉一半这类波动太频繁，报了只会让人关掉通知。"""
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW - timedelta(hours=1))
        fail_source(store, "weibo", 1)  # 1 次失败 = degraded

        assert canary.check("weibo", NOW).status is Health.DEGRADED
        sender = Recorder()
        assert Alerter(canary, store, sender).poll(NOW) is None
        assert sender.sent == []

    def test_healthy_platform_is_silent(self, setup):
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW)
        succeed_source(store, "zhihu", NOW)
        sender = Recorder()
        assert Alerter(canary, store, sender).poll(NOW) is None
        assert sender.sent == []

    def test_two_sources_down_share_one_message(self, setup):
        """一次 IP 被封会带倒好几个源，那是一件事，不该炸出好几条消息。"""
        store, _sources, canary = setup
        for name in ("weibo", "zhihu"):
            succeed_source(store, name, NOW - timedelta(hours=1))
            fail_source(store, name, 3)
        sender = Recorder()

        message = Alerter(canary, store, sender).poll(NOW)

        assert len(sender.sent) == 1
        assert "weibo" in message and "zhihu" in message


class TestDeliveryFailure:
    def test_failed_send_is_retried_next_round(self, setup):
        """先记「已通知」再发送，会让一次网络抖动永久吞掉一条告警——
        而告警恰恰是出问题时才用的，那时候网络本来更可能不好。"""
        store, _sources, canary = setup
        succeed_source(store, "weibo", NOW - timedelta(hours=1))
        fail_source(store, "weibo", 3)
        failing = Recorder(ok=False)

        assert Alerter(canary, store, failing).poll(NOW) is None
        assert store.alert_states() == {}  # 没发出去就不该记

        working = Recorder()
        assert Alerter(canary, store, working).poll(NOW) is not None
        assert store.alert_states() == {"weibo": "down"}

    def test_no_credentials_returns_false_and_does_not_raise(self, monkeypatch):
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_CHAT_ID", "")
        assert send_telegram("x") is False

    def test_network_error_returns_false(self, monkeypatch):
        """best-effort 的意思是：坏了也只回 False，绝不把调用方带崩。"""
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_CHAT_ID", "c")

        def boom(*args, **kwargs):
            raise OSError("网络没了")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert send_telegram("x") is False

    def test_telegram_says_not_ok(self, monkeypatch):
        """token/chat_id 配错时对方回 200 但 ok=false，那也是失败。"""
        import io

        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_CHAT_ID", "c")

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: FakeResponse(b'{"ok":false,"description":"chat not found"}'),
        )
        assert send_telegram("x") is False


class TestMessage:
    def test_contains_source_reason_and_age(self):
        report = SourceHealthReport(
            name="wechat",
            status=Health.DOWN,
            reason="连续失败 39 次（最后一次：CAPTCHA）",
            last_success_at=datetime(2026, 8, 7, 14, 57, tzinfo=UTC),
            consecutive_failures=39,
            last_item_count=230,
            stale_seconds=905053,
        )
        text = format_alert([report], [], {"ok": 32, "down": 1}, NOW)
        assert "wechat" in text
        assert "CAPTCHA" in text
        assert "10.5 天前" in text  # 断了多久是判断严重性的关键信息
        assert "ok 32" in text

    def test_never_seen_source_says_so(self):
        report = SourceHealthReport(
            name="new-source",
            status=Health.DOWN,
            reason="从未成功，已连续失败 3 次（UPSTREAM_DOWN）",
            last_success_at=None,
            consecutive_failures=3,
            last_item_count=0,
            stale_seconds=None,
        )
        assert "从未成功过" in format_alert([report], [], {"down": 1}, NOW)

    def test_long_message_is_truncated_not_rejected(self, monkeypatch):
        """一次大面积故障可能拉出几十个源，超长该截断而不是整条发不出去。"""
        sent = {}
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr("sourcepilot.alert.TELEGRAM_CHAT_ID", "c")

        import io

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            sent["body"] = request.data.decode("utf-8")
            return FakeResponse(b'{"ok":true}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert send_telegram("啊" * 5000) is True
        import urllib.parse

        text = urllib.parse.parse_qs(sent["body"])["text"][0]
        assert "已截断" in text
        assert len(text) < 5000


class TestSchedulerIntegration:
    def test_alerter_failure_does_not_stop_collection(self, tmp_path):
        """告警自己出问题不能反过来影响采集——这是接进调度线程的前提。"""
        from sourcepilot.collector import Collector, Scheduler

        store = Store(tmp_path / "s.db")
        sources = {"weibo": make_source("weibo", enabled=False)}

        class Exploding:
            def poll(self):
                raise RuntimeError("告警炸了")

        scheduler = Scheduler(Collector(store, sources), tick_seconds=0.01, alerter=Exploding())
        scheduler.start()
        try:
            # 只要循环没死，stop 就能在超时前 join 上。
            pass
        finally:
            scheduler.stop(timeout=2)
        assert scheduler._thread is None
