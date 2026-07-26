"""Canary 自检：让「悄悄坏掉」变成「看得见地坏掉」。

**为什么需要它**：24 个源里任何一个改版、被封、或返回空，调度器只会在日志里
留一行 warning，然后继续跑。库里那个源的条目慢慢变陈旧，但 `/health` 只显示
「上次成功于某时」——没人会去逐个对时间。等到发现时，可能已经断了好几天。

Canary 做三件事：

1. **按源判定健康度**，不是只看最后一次成功。连续失败次数、距上次成功多久、
   本次产出条目数，三者一起看。
2. **区分「该报警」和「正常波动」**。一个源本来就 1 小时抓一次，那它 40 分钟
   没更新是正常的；超过它自己间隔的若干倍才算异常。用固定阈值会把慢源全报红。
3. **把判定结果暴露在 `/health` 里**，让人和监控都能一眼看到。

它不自己去抓——那是调度器的事。Canary 只读状态、下判断，职责分开。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .sources.config import SourceConfig
from .store import Store

log = logging.getLogger("sourcepilot.canary")


class Health(StrEnum):
    OK = "ok"
    #: 还能用，但有迹象不对（落后了、条目数掉了）
    DEGRADED = "degraded"
    #: 已经不产出数据了
    DOWN = "down"
    #: 没启用或从没跑过，不算故障
    IDLE = "idle"


#: 距上次成功超过「自己间隔 × 这个倍数」才算落后。
#: 用倍数而不是固定时长：1 小时抓一次的源和 5 分钟抓一次的源，落后的定义不同。
STALE_FACTOR = 3
#: 连续失败到这个次数就判 down。一两次多半是网络抖动，不值得报警。
DOWN_AFTER_FAILURES = 3
#: 条目数掉到历史正常值的这个比例以下，判 degraded——
#: 「还能抓到但只剩两条」往往是选择器半坏，比整个挂掉更隐蔽。
YIELD_DROP_RATIO = 0.5


@dataclass
class SourceHealthReport:
    name: str
    status: Health
    reason: str
    last_success_at: datetime | None
    consecutive_failures: int
    last_item_count: int
    stale_seconds: float | None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "last_success_at": (
                self.last_success_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self.last_success_at
                else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "last_item_count": self.last_item_count,
            "stale_seconds": round(self.stale_seconds) if self.stale_seconds else None,
        }


class Canary:
    def __init__(self, store: Store, sources: dict[str, SourceConfig]) -> None:
        self.store = store
        self.sources = sources

    def check(self, name: str, now: datetime | None = None) -> SourceHealthReport:
        now = now or datetime.now(UTC)
        config = self.sources[name]
        state = self.store.get_state(name) or {}

        last_success = state.get("last_success_at")
        failures = int(state.get("consecutive_failures") or 0)
        count = int(state.get("last_item_count") or 0)
        error = state.get("last_error_code")
        stale = (now - last_success).total_seconds() if last_success else None

        def report(status: Health, reason: str) -> SourceHealthReport:
            return SourceHealthReport(
                name=name,
                status=status,
                reason=reason,
                last_success_at=last_success,
                consecutive_failures=failures,
                last_item_count=count,
                stale_seconds=stale,
            )

        if not config.enabled:
            return report(Health.IDLE, "未启用")
        if last_success is None:
            # 从没成功过：如果还没失败过，就是刚启动还没轮到它，不算故障。
            if failures == 0:
                return report(Health.IDLE, "尚未采集过")
            return report(Health.DOWN, f"从未成功，已连续失败 {failures} 次（{error}）")

        if failures >= DOWN_AFTER_FAILURES:
            return report(Health.DOWN, f"连续失败 {failures} 次（最后一次：{error}）")

        limit = config.min_interval * STALE_FACTOR
        if stale is not None and stale > limit:
            return report(
                Health.DEGRADED,
                f"距上次成功 {stale / 60:.0f} 分钟，超过自身间隔的 {STALE_FACTOR} 倍",
            )

        if failures > 0:
            return report(Health.DEGRADED, f"最近失败 {failures} 次（{error}），但仍有成功记录")

        if count == 0:
            # 采集"成功"但一条都没有，通常是选择器还在、内容没了。
            return report(Health.DEGRADED, "上次采集成功但零条目——多半是提取规则半坏")

        return report(Health.OK, "正常")

    def check_all(self, now: datetime | None = None) -> list[SourceHealthReport]:
        return [self.check(name, now) for name in sorted(self.sources)]

    def summary(self, now: datetime | None = None) -> dict:
        reports = self.check_all(now)
        counts: dict[str, int] = {}
        for r in reports:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        problems = [r for r in reports if r.status in (Health.DOWN, Health.DEGRADED)]
        return {
            # 只要还有源在正常产出，平台整体就是可用的——单源故障不该让整体报红。
            "ok": not any(r.status is Health.DOWN for r in reports),
            "counts": counts,
            "problems": [r.to_dict() for r in problems],
            "sources": [r.to_dict() for r in reports],
        }
