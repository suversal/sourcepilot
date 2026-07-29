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
from .tweet import TweetRecord

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
            # Nitter 走 RSS，拿不到互动数与引用链，所以给不出 TweetRecord。
            # 空列表在这里是**诚实的**：宁可推文表少一条，也不写一条互动数
            # 全为 0 的假记录——那会让下游以为这条推文没人理。
            return backend.fetch_timeline(handle, limit), [], None

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

    def fill_articles(self, records, store=None, limit: int = 10) -> int:
        """给带长文的推文补上正文。

        长文正文**每篇要单独一次请求**，所以这里只补「确实有长文、且还没取过
        正文」的那些，并设上限——一次搜索若撞上十几篇长文，全补会打出一串请求，
        比抓推文本身还重。补不到的下次再来（`missing_article_text` 查得到）。
        """
        pending = [
            r for r in records
            if r.has_article and not r.article_markdown
        ][:limit]
        if not pending or not self.graphql.available():
            return 0

        filled = 0
        for record in pending:
            try:
                article = self.graphql.fetch_article(record.tweet_id)
            except SourcePilotError as exc:
                # 正文补不到不该影响推文本身——它已经入库了。
                log.warning("取长文正文失败（%s）：%s", record.tweet_id, exc.code.value)
                if exc.code in BACKEND_LEVEL_FAILURES:
                    break  # 被限流了就别接着捅
                continue
            if not article:
                continue
            for key, value in article.items():
                setattr(record, key, value)
            filled += 1
        if filled and store is not None:
            store.upsert_tweets(pending)
        return filled

    def tweet(self, handle: str, tweet_id: str) -> Item | None:
        return self._run(
            [self.fxtwitter], lambda b: b.fetch_tweet(handle, tweet_id), f"单推 {tweet_id}"
        )


class _TweetSink:
    """推文全貌的落库出口。

    `collect_x` 是 channel 入口，签名固定为「配置进、Item 列表出」，拿不到
    Store。和 COOLDOWNS 同一个模式：服务启动时绑定一次，没绑定就静默丢弃
    ——**丢的只是推文表这份附加视图，Item 照常入库**，所以测试和一次性脚本
    不绑定也能正常跑。
    """

    def __init__(self) -> None:
        self._store = None

    @property
    def store(self):
        return self._store

    def bind(self, store) -> None:
        self._store = store

    def write(self, records) -> int:
        if self._store is None or not records:
            return 0
        try:
            return self._store.upsert_tweets(records)
        except Exception:
            # 推文表写失败不该让整轮采集失败——Item 已经落库了，那是主线。
            log.warning("推文全貌落库失败，本轮跳过", exc_info=True)
            return 0


TWEET_SINK = _TweetSink()


def collect_x(config: SourceConfig) -> list[Item]:
    """定时采集入口：把配置里关注的账号的时间线抓进库，供缓存兜底用。

    契约规定 `search_x` 现查失败要能降级回缓存——库里得先有东西，那个降级才有意义。
    """
    # accounts 是 ChannelAccount（公众号那边要 fakeid 才加的）。X 只认 handle，
    # 取 name 即可——但不能直接传对象，下游要对它做字符串操作。
    handles = [getattr(a, "name", a) for a in (config.accounts or [])]
    if not handles:
        return []

    router = XRouter(nitter_instances=config.nitter_instances or None)
    items: list[Item] = []
    tweet_records: list[TweetRecord] = []
    for handle in handles:
        try:
            fetched, records, _ = router.timeline(handle, config.per_account_limit)
            items.extend(fetched)
            tweet_records.extend(records)
        except SourcePilotError as exc:
            # 单个账号取不到不该拖垮整批
            log.warning("X 取 @%s 时间线失败：%s", handle, exc.code.value)
            continue

    # 长文正文单独补。放在写库之后：推文先落地，正文补不到也不影响主线。
    router.fill_articles(tweet_records, TWEET_SINK.store)
    TWEET_SINK.write(tweet_records)
    return items


register_channel("x", collect_x)

__all__ = [
    "TWEET_SINK",
    "Account",
    "AccountPool",
    "FxTwitterBackend",
    "GraphQLBackend",
    "NitterBackend",
    "XRouter",
    "collect_x",
]
