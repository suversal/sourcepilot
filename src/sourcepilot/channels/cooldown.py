"""后端冷却状态机（各 channel 共用）。

核心判断是**区分「临时挡了一下」和「这条路废了」**——两者的正确反应完全相反：
限流该退避几十分钟再试，凭据失效则重试多少次都没用，继续捅只会加速封号。
参考项目 twscrape 的 `_check_rep` 就是这个思路。

公众号 channel 先用上了它；X 的多后端路由与账号池是同一套需求，所以提到这里共用，
免得两边各写一份、判断迟早不一致。

**状态会落盘**。早先只放在进程内，那意味着重启一次冷却就清零——真被封号时
重启一下就又去捅了。那是账号安全问题，不是体验问题，所以现在挂上 Store 之后
会把冷却写进 SQLite，进程重启后照样生效。没挂 Store 时退化成纯内存（测试用）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..contracts import ErrorCode

log = logging.getLogger("sourcepilot.channels")

#: 各类故障的冷却时长（秒）。分档依据是「重试有没有意义」。
COOLDOWN_SECONDS: dict[ErrorCode, int] = {
    # 凭据失效／封号：重试无意义，等人来换。设长一点，免得白白暴露账号。
    ErrorCode.AUTH_EXPIRED: 6 * 3600,
    # 限流与验证码：对方在说「慢点」，退避后还能用。
    ErrorCode.RATE_LIMITED: 30 * 60,
    ErrorCode.CAPTCHA: 30 * 60,
    # 网络抖动之类：很可能下一轮就好了。
    ErrorCode.UPSTREAM_DOWN: 5 * 60,
    ErrorCode.TIMEOUT: 5 * 60,
}
DEFAULT_COOLDOWN = 5 * 60

#: 只有这几种故障值得冷却整条后端／整个账号——它们都在说「再捅就要出事」。
#: 其余错误（对方改版、单条内容没了、网络抖动）是局部问题，
#: 冷却整体会把其它账号或其它待抓对象一起饿死。
BACKEND_LEVEL_FAILURES = frozenset(
    {ErrorCode.AUTH_EXPIRED, ErrorCode.RATE_LIMITED, ErrorCode.CAPTCHA}
)


@dataclass
class _Entry:
    until: float
    code: ErrorCode


class CooldownRegistry:
    """按名字记冷却。名字可以是后端名（`fxtwitter`）或账号名（`x:acct_3`）。"""

    def __init__(self, store=None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._store = store
        if store is not None:
            self._load()

    def bind(self, store) -> None:
        """挂上持久化。服务启动时调用一次，把上次的冷却读回来。"""
        self._store = store
        self._load()

    def _load(self) -> None:
        import time as _t
        from datetime import UTC, datetime

        try:
            saved = self._store.load_cooldowns(datetime.now(UTC))
        except Exception as exc:  # 读不出来不该拖垮启动
            log.warning("读取持久化冷却失败：%s", exc)
            return
        now_wall, now_mono = datetime.now(UTC).timestamp(), _t.time()
        for key, (until, code) in saved.items():
            # 存的是墙上时间，内存里用的是 time.time()，两者换算一下。
            self._entries[key] = _Entry(
                until=now_mono + (until.timestamp() - now_wall), code=ErrorCode(code)
            )
        if self._entries:
            log.info("从库里恢复了 %d 条仍在生效的冷却", len(self._entries))

    def penalize(
        self, key: str, code: ErrorCode, now: float | None = None, seconds: int | None = None
    ) -> int:
        """记一次惩罚。`seconds` 可显式指定——X 的限流响应会直接给出 reset 时间。"""
        span = seconds if seconds is not None else COOLDOWN_SECONDS.get(code, DEFAULT_COOLDOWN)
        now = now if now is not None else time.time()
        self._entries[key] = _Entry(until=now + span, code=code)
        log.warning("%s 因 %s 冷却 %d 秒", key, code.value, span)
        if self._store is not None:
            from datetime import UTC, datetime, timedelta

            try:
                self._store.save_cooldown(
                    key, datetime.now(UTC) + timedelta(seconds=span), code.value
                )
            except Exception as exc:
                log.warning("冷却落盘失败（内存里仍生效）：%s", exc)
        return span

    def blocked(self, key: str, now: float | None = None) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return False
        now = now if now is not None else time.time()
        if now >= entry.until:
            del self._entries[key]
            return False
        return True

    def reason(self, key: str) -> ErrorCode | None:
        entry = self._entries.get(key)
        return entry.code if entry else None

    def remaining(self, key: str, now: float | None = None) -> float:
        entry = self._entries.get(key)
        if entry is None:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, entry.until - now)

    def clear(self, key: str) -> None:
        """成功一次就解除冷却——上一次的故障已经过去了。"""
        self._entries.pop(key, None)
        if self._store is not None:
            try:
                self._store.clear_cooldown(key)
            except Exception as exc:
                log.warning("清除持久化冷却失败：%s", exc)

    def reset(self) -> None:
        """只清内存，不动库——测试用。"""
        self._entries.clear()


#: 全局单例。调度器与出口层共用同一份判断，不各记各的。
COOLDOWNS = CooldownRegistry()
