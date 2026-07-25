"""X channel：分层后端路由。

**每种能力走能力最匹配的后端**，不是简单地「一条链从头试到尾」——三个后端的
能力集不重叠：

| 能力 | 后端顺序 | 认证 |
|---|---|---|
| 搜索 `search` | graphql | **必须登录** |
| 时间线 `timeline` | nitter → graphql | nitter 零认证 |
| 单推/资料 `tweet` | fxtwitter | 零认证 |

搜索为什么只有一条路：实测（2026-07-26）Nitter 各实例的搜索一律返回 0 条、
xcancel 要 RSS 白名单、X guest token 的旧搜索端点已下线。**免登录搜 X 已经没有路了。**

时间线为什么把零认证的 Nitter 排在前面：它不消耗账号配额、不承担封号风险。
账号是稀缺且脆弱的资源，能不用就不用——这也是参考项目 x-tweet-fetcher 的思路。
"""

from __future__ import annotations

import logging

from ...contracts import AuthExpired, Item, NotFound, SourcePilotError
from ...sources.config import SourceConfig
from ...sources.engine import register_channel
from ..cooldown import BACKEND_LEVEL_FAILURES, COOLDOWNS
from .accounts import Account, AccountPool
from .fxtwitter import FxTwitterBackend
from .graphql import GraphQLBackend
from .nitter import NitterBackend

log = logging.getLogger("sourcepilot.channels.x")


class XRouter:
    """按能力挑后端，并在后端之间做故障转移。"""

    def __init__(
        self,
        nitter_instances: list[str] | None = None,
        impersonate: str | None = None,
    ) -> None:
        self.fxtwitter = FxTwitterBackend()
        self.nitter = NitterBackend(nitter_instances)
        self.graphql = GraphQLBackend(impersonate=impersonate)

    def _usable(self, backends):
        return [b for b in backends if b.available() and not COOLDOWNS.blocked(f"x:{b.name}")]

    def _run(self, backends, call, what: str):
        """顺次试各后端。失败的按「是不是该停手」决定要不要冷却。"""
        first_error: SourcePilotError | None = None
        candidates = self._usable(backends)
        if not candidates:
            names = [b.name for b in backends]
            # 用 AUTH_EXPIRED 而不是 INTERNAL：对 Agent 来说这是可操作的信息
            # （「平台侧要配账号」），而 INTERNAL 只会让它以为是 bug。
            raise AuthExpired(f"X 的{what}没有可用后端（{', '.join(names)}）")

        for backend in candidates:
            try:
                result = call(backend)
            except SourcePilotError as exc:
                if exc.code in BACKEND_LEVEL_FAILURES:
                    COOLDOWNS.penalize(f"x:{backend.name}", exc.code)
                log.warning("X 后端 %s 处理 %s 失败：%s", backend.name, what, exc.code.value)
                first_error = first_error or exc
                continue
            COOLDOWNS.clear(f"x:{backend.name}")
            if backend is not candidates[0]:
                log.info("X 的 %s 由降级后端 %s 提供", what, backend.name)
            return result

        raise first_error or AuthExpired(f"X 的{what}全部后端都失败了")

    # ---------- 对外能力 ----------

    def search(self, query: str, limit: int, cursor: str | None = None):
        """搜索只有 GraphQL 一条路——免登录路径实测已全部关闭。"""
        return self._run(
            [self.graphql], lambda b: b.search(query, limit, cursor), f"搜索 {query!r}"
        )

    def timeline(self, handle: str, limit: int, cursor: str | None = None):
        """时间线优先走零认证的 Nitter，省下账号配额和封号风险。"""
        handle = handle.lstrip("@")

        def via_nitter(backend):
            return backend.fetch_timeline(handle, limit), None

        def via_graphql(backend):
            user_id = backend.user_id(handle)
            if not user_id:
                raise NotFound(f"找不到用户 @{handle}")
            return backend.timeline(user_id, limit, cursor)

        candidates = self._usable([self.nitter, self.graphql])
        if not candidates:
            raise AuthExpired("X 时间线没有可用后端")

        first_error: SourcePilotError | None = None
        for backend in candidates:
            try:
                runner = via_nitter if backend is self.nitter else via_graphql
                result = runner(backend)
            except SourcePilotError as exc:
                if exc.code in BACKEND_LEVEL_FAILURES:
                    COOLDOWNS.penalize(f"x:{backend.name}", exc.code)
                first_error = first_error or exc
                log.warning("X 后端 %s 取时间线失败：%s", backend.name, exc.code.value)
                continue
            COOLDOWNS.clear(f"x:{backend.name}")
            return result
        raise first_error or AuthExpired("X 时间线全部后端都失败了")

    def tweet(self, handle: str, tweet_id: str) -> Item | None:
        return self._run(
            [self.fxtwitter], lambda b: b.fetch_tweet(handle, tweet_id), f"单推 {tweet_id}"
        )


def collect_x(config: SourceConfig) -> list[Item]:
    """定时采集入口：把配置里关注的账号的时间线抓进库，供缓存兜底用。

    契约规定 `search_x` 现查失败要能降级回缓存——库里得先有东西，那个降级才有意义。
    """
    handles = list(config.accounts or [])
    if not handles:
        return []

    router = XRouter(nitter_instances=config.nitter_instances or None)
    items: list[Item] = []
    for handle in handles:
        try:
            fetched, _ = router.timeline(handle, config.per_account_limit)
            items.extend(fetched)
        except SourcePilotError as exc:
            # 单个账号取不到不该拖垮整批
            log.warning("X 取 @%s 时间线失败：%s", handle, exc.code.value)
            continue
    return items


register_channel("x", collect_x)

__all__ = [
    "Account",
    "AccountPool",
    "FxTwitterBackend",
    "GraphQLBackend",
    "NitterBackend",
    "XRouter",
    "collect_x",
]
