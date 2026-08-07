"""channel 的批次轮转。

有些 channel 对**单轮请求总量**敏感——微信读书实测 24 个号一次打完会弹人机
验证，而把间隔从 3 秒放到 8 秒并不管用（第 1 个号就被弹），说明它看的是单轮
总量而不是瞬时密度。摊成 4 轮各 6 个才落在容忍度内。

代价是单个号的更新延迟乘以批数。对公众号这类信源可以接受：收录本身就滞后
几小时，谁也不指望分钟级。

游标存在库里而不是内存：进程重启很频繁（改配置、部署），内存游标会让每次
重启都从头开始，前几个号被反复抓、后面的永远轮不到。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TypeVar

log = logging.getLogger("sourcepilot.channels.rotation")

T = TypeVar("T")


class _Rotation:
    """轮转游标。和 COOLDOWNS 一样，服务启动时绑定一次 store。

    没绑定就退化成「每轮从头开始」——测试和一次性脚本不必配这个，
    它们本来也只跑一轮。
    """

    def __init__(self) -> None:
        self._store = None

    def bind(self, store) -> None:
        self._store = store

    def take(self, key: str, items: Sequence[T], size: int | None) -> list[T]:
        """取本轮该处理的一批，并把游标推到下一批。

        **游标在取的时候就推进，不等这批成功**。整批失败通常是账号级问题
        （限流、验证码），重试同一批没有意义，反而会让后面的号永远轮不到。
        """
        if not items or not size or size >= len(items):
            return list(items)

        start = 0
        if self._store is not None:
            try:
                start = int(self._store.get_channel_state(f"rotation:{key}", "0") or 0)
            except (ValueError, TypeError):
                start = 0
        start %= len(items)

        # 绕回时从头接着取，保证每轮都是满的一批——否则最后一批只剩一两个，
        # 那一轮的请求预算就浪费了。
        batch = list(items[start : start + size])
        if len(batch) < size:
            batch += list(items[: size - len(batch)])

        if self._store is not None:
            self._store.set_channel_state(f"rotation:{key}", str((start + size) % len(items)))
        log.info(
            "%s 本轮取第 %d–%d 个（共 %d），下轮从 %d 开始",
            key, start, start + len(batch) - 1, len(items), (start + size) % len(items),
        )
        return batch


ROTATION = _Rotation()
