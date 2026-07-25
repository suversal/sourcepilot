"""后端冷却状态机。

核心判断是**区分「临时挡了一下」和「这条路废了」**——两者的正确反应完全相反：
限流该退避几十分钟再试，凭据失效则重试多少次都没用，继续捅只会加速封号。
参考项目 twscrape 的 `_check_rep` 就是这个思路，这里先在公众号上落地，
第 3 步的 X 账号池可以直接复用。

状态放在进程内：调度器是常驻的，进程重启本来就该给每条路一次重试机会。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ...contracts import ErrorCode

log = logging.getLogger("sourcepilot.channels.wechat")

#: 各类故障的冷却时长（秒）。分档的依据是「重试有没有意义」。
COOLDOWN_SECONDS: dict[ErrorCode, int] = {
    # 凭据失效：重试无意义，等人来换。设长一点，免得白白暴露账号。
    ErrorCode.AUTH_EXPIRED: 6 * 3600,
    # 限流与验证码：对方在说「慢点」，退避后还能用。
    ErrorCode.RATE_LIMITED: 30 * 60,
    ErrorCode.CAPTCHA: 30 * 60,
    # 网络抖动之类：很可能下一轮就好了。
    ErrorCode.UPSTREAM_DOWN: 5 * 60,
    ErrorCode.TIMEOUT: 5 * 60,
}
DEFAULT_COOLDOWN = 5 * 60


@dataclass
class _Entry:
    until: float
    code: ErrorCode


class CooldownRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def penalize(self, backend: str, code: ErrorCode, now: float | None = None) -> int:
        seconds = COOLDOWN_SECONDS.get(code, DEFAULT_COOLDOWN)
        now = now if now is not None else time.time()
        self._entries[backend] = _Entry(until=now + seconds, code=code)
        log.warning("后端 %s 因 %s 冷却 %d 秒", backend, code.value, seconds)
        return seconds

    def blocked(self, backend: str, now: float | None = None) -> bool:
        entry = self._entries.get(backend)
        if entry is None:
            return False
        now = now if now is not None else time.time()
        if now >= entry.until:
            del self._entries[backend]
            return False
        return True

    def reason(self, backend: str) -> ErrorCode | None:
        entry = self._entries.get(backend)
        return entry.code if entry else None

    def clear(self, backend: str) -> None:
        """成功一次就解除冷却——上一次的故障已经过去了。"""
        self._entries.pop(backend, None)

    def reset(self) -> None:
        self._entries.clear()


#: 全局单例。调度器与出口层共用同一份判断，不各记各的。
COOLDOWNS = CooldownRegistry()
