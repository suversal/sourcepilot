"""公众号 channel：多后端降级链。

**整块隔离**：这是全平台最可能被官方封掉的一条线（走的都不是官方开放 API）。
凭据、后端、冷却判断全在这个包里，坏了整块换掉，不牵动其它信源。

**为什么是链而不是单一后端**（照参考项目 x-tweet-fetcher 的多后端路由）：
两条路的失效方式互不相关——公众平台会因为凭据过期或限流而断，搜狗不需要凭据、
不受此影响；反过来搜狗撞验证码时，有凭据的公众平台照常工作。串起来之后，
任何一条单独挂掉都不会让公众号这块彻底没数据。

| 后端 | 凭据 | 数据 | 链接 | 账号风险 |
|---|---|---|---|---|
| `mp` 公众平台 | 需要 | 按号精确、最全 | 永久 | 有（抓狠了会封） |
| `sogou` 搜狗 | 不需要 | 搜索结果，需筛 | **限时，会过期** | 无 |

顺序在配置里（`backends`），默认先主力后应急。
"""

from __future__ import annotations

import logging

from ...contracts import AuthExpired, ErrorCode, Item, SourcePilotError
from ...sources.config import SourceConfig
from ...sources.engine import register_channel
from .cooldown import COOLDOWNS
from .mp import Credentials, MpBackend, WechatClient
from .sogou import SogouBackend

log = logging.getLogger("sourcepilot.channels.wechat")

BACKENDS = {"mp": MpBackend, "sogou": SogouBackend}

#: 只有这几种故障值得冷却整个后端——它们都在说「再捅就要出事」。
#: 其余错误（改版、内容没了、网络抖动）是单个账号的事，冷却后端会饿死其它账号。
BACKEND_LEVEL_FAILURES = frozenset(
    {ErrorCode.AUTH_EXPIRED, ErrorCode.RATE_LIMITED, ErrorCode.CAPTCHA}
)


def _build(names: list[str]):
    backends = []
    for name in names:
        factory = BACKENDS.get(name)
        if factory is None:
            log.warning("未知的公众号后端：%s，跳过", name)
            continue
        backends.append(factory())
    return backends


def collect_wechat(config: SourceConfig) -> list[Item]:
    """channel 入口。由 sources.engine.collect 按 `channel: wechat` 分派进来。"""
    accounts = list(config.accounts or [])
    if not accounts:
        return []

    backends = _build(config.backends or ["mp", "sogou"])
    if not backends:
        raise AuthExpired("公众号没有可用后端")

    items: list[Item] = []
    first_error: SourcePilotError | None = None
    served_any = False
    attempted_any = False

    for account in accounts:
        for backend in backends:
            if COOLDOWNS.blocked(backend.name):
                continue
            if not backend.available():
                # 没凭据不算故障，只是这条路现在走不了，别记进冷却。
                continue

            attempted_any = True
            try:
                fetched = backend.fetch(account, config.per_account_limit)
            except SourcePilotError as exc:
                if exc.code in BACKEND_LEVEL_FAILURES:
                    # 只有「再捅就要被封」的信号才冷却整个后端。
                    COOLDOWNS.penalize(backend.name, exc.code)
                else:
                    # 单个号抓不到（改版、内容没了、网络抖动）是这一个号的事，
                    # 冷却整个后端会把后面的号一起饿死。
                    log.warning(
                        "后端 %s 抓 %s 失败：%s", backend.name, account, exc.code.value
                    )
                first_error = first_error or exc
                continue
            except Exception as exc:
                log.warning("后端 %s 抓 %s 出错：%s", backend.name, account, type(exc).__name__)
                continue

            COOLDOWNS.clear(backend.name)
            served_any = True
            items.extend(fetched)
            if backend is not backends[0]:
                log.info("公众号 %s 由降级后端 %s 提供", account, backend.name)
            break  # 这个号已经拿到了，不必再问后面的后端

    if not attempted_any:
        blocked = [b.name for b in backends if COOLDOWNS.blocked(b.name)]
        if blocked:
            # 全在冷却里是**故障**：说明刚刚被限流或凭据被拒了，得让人看见。
            raise AuthExpired(f"公众号后端全在冷却中：{', '.join(blocked)}")
        # 一个凭据都没配过是**配置状态**，不是故障。这样仓库默认开着也不会
        # 让别人克隆下来就满屏报错；/health 里的 last_item_count=0 已经说明问题。
        log.info("公众号未配置凭据，本轮跳过（配置见 config/sources/wechat.yaml）")
        return []
    if not served_any and first_error is not None:
        # 所有后端都试过且都失败——把最先遇到的错误报上去，那是运维要修的东西。
        raise first_error
    return items


register_channel("wechat", collect_wechat)

__all__ = [
    "COOLDOWNS",
    "Credentials",
    "MpBackend",
    "SogouBackend",
    "WechatClient",
    "collect_wechat",
]
