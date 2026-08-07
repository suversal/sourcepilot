"""公众号 channel：多后端降级链。

**整块隔离**：这是全平台最可能被官方封掉的一条线（走的都不是官方开放 API）。
凭据、后端、冷却判断全在这个包里，坏了整块换掉，不牵动其它信源。

**为什么是链而不是单一后端**（照参考项目 x-tweet-fetcher 的多后端路由）：
两条路的失效方式互不相关——公众平台会因为凭据过期或限流而断，搜狗不需要凭据、
不受此影响；反过来搜狗撞验证码时，有凭据的公众平台照常工作。串起来之后，
任何一条单独挂掉都不会让公众号这块彻底没数据。

| 后端 | 凭据 | 数据 | 链接 | 账号风险 |
|---|---|---|---|---|
| `weread` 微信读书 | 微信读书 cookie | 按号精确，书架里的号 | 永久 | 有（有反爬，别抓快） |
| `mp` 公众平台 | 公众平台 token+cookie | 按号精确、最全 | 永久 | **能力已被关**，见 mp.py |
| `sogou` 搜狗 | 不需要 | 搜索结果，需筛 | **限时，会过期** | 无 |

顺序在配置里（`backends`），默认先主力后应急。

**2026-08-06 起主力换成 `weread`**：微信在 7-30 前后关掉了公众平台后台的跨公众号
文章列表接口（mp 后端因此拿不到数据，实测换账号、换 IP 都无效）。微信读书是另一套
系统，不受影响。mp 保留在链里——它拿到的字段更全，能力若恢复就该优先用它。
"""

from __future__ import annotations

import logging

from ...contracts import AuthExpired, Item, SourcePilotError
from ...sources.config import SourceConfig
from ...sources.engine import register_channel
from ..cooldown import BACKEND_LEVEL_FAILURES, COOLDOWNS
from ..rotation import ROTATION
from .mp import Credentials, MpBackend, WechatClient
from .sogou import SogouBackend
from .weread import WereadBackend, WereadCredentials

log = logging.getLogger("sourcepilot.channels.wechat")

BACKENDS = {"weread": WereadBackend, "mp": MpBackend, "sogou": SogouBackend}


def _build(names: list[str], account_interval: float = 3.0):
    backends = []
    for name in names:
        factory = BACKENDS.get(name)
        if factory is None:
            log.warning("未知的公众号后端：%s，跳过", name)
            continue
        # `account_interval` 是 channel 级配置，但只有实现了节流的后端接受它。
        # weread 那条路有反爬（参考实现作者实测一天 30 多次快速请求就白屏），
        # 间隔必须能从配置调，不能写死在代码里。
        if factory is WereadBackend:
            backends.append(factory(account_interval=account_interval))
        else:
            backends.append(factory())
    return backends


def collect_wechat(config: SourceConfig) -> list[Item]:
    """channel 入口。由 sources.engine.collect 按 `channel: wechat` 分派进来。"""
    accounts = list(config.accounts or [])
    if not accounts:
        return []
    # 分批轮转：微信读书对单轮请求总量敏感，24 个号一次打完会弹验证码。
    accounts = ROTATION.take("wechat", accounts, config.batch_size)

    # 默认顺序 2026-08-06 起把 weread 提到最前：mp 的跨号列表能力被微信关了。
    backends = _build(config.backends or ["weread", "mp"], config.account_interval)
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
    "WereadBackend",
    "WereadCredentials",
    "collect_wechat",
]
