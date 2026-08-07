"""channel 的批次轮转。

有些 channel 对**单轮请求总量**敏感——微信读书实测 24 个号一次打完会弹人机
验证，而把间隔从 3 秒放到 8 秒并不管用（第 1 个号就被弹），说明它看的是单轮
总量而不是瞬时密度。
"""

from __future__ import annotations

import pytest

from sourcepilot.channels.rotation import _Rotation

ITEMS = list("abcdefghij")  # 10 个


@pytest.fixture
def rot(store):
    r = _Rotation()
    r.bind(store)
    return r


class TestCoverage:
    def test_consecutive_rounds_cover_everything_once(self, rot):
        """轮转的意义就在这：几轮下来不重不漏。"""
        seen = []
        for _ in range(5):
            seen += rot.take("k", ITEMS, 2)
        assert seen == ITEMS

    def test_wraps_around(self, rot):
        for _ in range(5):
            rot.take("k", ITEMS, 2)
        assert rot.take("k", ITEMS, 2) == ["a", "b"]

    def test_wrap_keeps_batches_full(self, rot):
        """绕回时从头接着取。剩一两个就只抓一两个的话，那轮的请求预算就浪费了。"""
        rot.take("k", ITEMS, 4)   # a-d
        rot.take("k", ITEMS, 4)   # e-h
        assert rot.take("k", ITEMS, 4) == ["i", "j", "a", "b"]

    def test_independent_keys(self, rot):
        rot.take("one", ITEMS, 3)
        assert rot.take("two", ITEMS, 3) == ["a", "b", "c"]


class TestDegradesSafely:
    def test_no_size_takes_everything(self, rot):
        assert rot.take("k", ITEMS, None) == ITEMS

    def test_size_larger_than_list(self, rot):
        assert rot.take("k", ITEMS, 99) == ITEMS

    def test_empty_list(self, rot):
        assert rot.take("k", [], 3) == []

    def test_unbound_starts_from_the_beginning(self):
        """没绑定 store 时退化成每轮从头——测试和一次性脚本本来就只跑一轮。"""
        r = _Rotation()
        assert r.take("k", ITEMS, 3) == ["a", "b", "c"]
        assert r.take("k", ITEMS, 3) == ["a", "b", "c"]

    def test_corrupt_cursor_does_not_crash(self, rot, store):
        """游标被写坏时从头开始，而不是让整个采集炸掉。"""
        store.set_channel_state("rotation:k", "不是数字")
        assert rot.take("k", ITEMS, 3) == ["a", "b", "c"]


class TestCursorSurvivesRestart:
    def test_cursor_is_persisted(self, store):
        """进程重启很频繁。内存游标会让每次重启都从头，前几个号被反复抓、
        后面的永远轮不到。"""
        first = _Rotation()
        first.bind(store)
        first.take("k", ITEMS, 3)

        after_restart = _Rotation()
        after_restart.bind(store)
        assert after_restart.take("k", ITEMS, 3) == ["d", "e", "f"]


class TestCursorAdvancesEvenOnFailure:
    def test_advance_happens_at_take_time(self, rot):
        """整批失败通常是账号级问题（限流、验证码），重试同一批没有意义，
        反而会让后面的号永远轮不到——所以取的时候就推进，不等成功。"""
        rot.take("k", ITEMS, 3)          # 假设这批全失败了
        assert rot.take("k", ITEMS, 3) == ["d", "e", "f"], "下一轮该换一批"
