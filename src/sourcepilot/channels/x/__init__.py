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
from .topic_filter import limit_topic_authors, topic_record_matches
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

    @staticmethod
    def _cooldown_key(backend, scope: str | None = None) -> str:
        base = f"x:{backend.name}"
        return f"{base}:{scope}" if scope else base

    def _usable(self, backends, scope: str | None = None):
        return [
            b
            for b in backends
            if b.available() and not COOLDOWNS.blocked(self._cooldown_key(b, scope))
        ]

    def _run(self, backends, call, what: str, *, cooldown_scope: str | None = None):
        """顺次试各后端。失败的按「是不是该停手」决定要不要冷却。"""
        first_error: SourcePilotError | None = None
        candidates = self._usable(backends, cooldown_scope)
        if not candidates:
            names = [b.name for b in backends]
            # 用 AUTH_EXPIRED 而不是 INTERNAL：对 Agent 来说这是可操作的信息
            # （「平台侧要配账号」），而 INTERNAL 只会让它以为是 bug。
            raise AuthExpired(f"X 的{what}没有可用后端（{', '.join(names)}）")

        for backend in candidates:
            cooldown_key = self._cooldown_key(backend, cooldown_scope)
            try:
                result = call(backend)
            except SourcePilotError as exc:
                if exc.code in BACKEND_LEVEL_FAILURES:
                    COOLDOWNS.penalize(cooldown_key, exc.code)
                log.warning("X 后端 %s 处理 %s 失败：%s", backend.name, what, exc.code.value)
                first_error = first_error or exc
                continue
            COOLDOWNS.clear(cooldown_key)
            if backend is not candidates[0]:
                log.info("X 的 %s 由降级后端 %s 提供", what, backend.name)
            return result

        raise first_error or AuthExpired(f"X 的{what}全部后端都失败了")

    # ---------- 对外能力 ----------

    def search(
        self,
        query: str,
        limit: int,
        cursor: str | None = None,
        product: str = "Latest",
    ):
        """搜索只有 GraphQL 一条路——免登录路径实测已全部关闭。"""
        return self._run(
            [self.graphql],
            lambda b: b.search(query, limit, cursor, product),
            f"搜索 {query!r}",
            cooldown_scope="SearchTimeline",
        )

    def timeline(self, handle: str, limit: int, cursor: str | None = None):
        """时间线优先走 GraphQL，Nitter 作降级。

        **这个顺序被调过一次，值得记下来为什么。**

        原本是 Nitter 优先，理由是「账号是稀缺且脆弱的资源，能不动用就不动用」
        ——那时只有 items 表，而 Nitter 的 RSS 给的 title/正文/时间/链接刚好
        够填满它。后来加了 x_tweets（互动数、引用链、线程、长文正文），
        **RSS 里根本没有这些字段**，于是订阅采集的推文一条全貌都没有。

        当初的权衡没错，错在需求变了之后没回头看它的前提还成不成立。

        配额上也确实付得起：订阅 2 个账号、每 15 分钟一轮 = 192 次/天，
        远低于 UserTweets 的限流阈值。Nitter 留作降级——账号被限流或失效时，
        有内容总比没有强，只是那一轮拿不到全貌。
        """
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

        candidates = self._usable([self.graphql, self.nitter])
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
    """定时采集入口：账号时间线 + 话题搜索，两条订阅腿走同一轮。

    账号订阅盖「官方说了什么」，话题订阅盖「某个事件下大家在说什么」。
    契约规定 `search_x` 现查失败要能降级回缓存——库里得先有东西，那个降级才有意义。
    """
    # accounts 是 ChannelAccount（公众号那边要 fakeid 才加的）。X 只认 handle，
    # 取 name 即可——但不能直接传对象，下游要对它做字符串操作。
    handles = [getattr(a, "name", a) for a in (config.accounts or [])]
    if not handles and not config.topics:
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

    # ── 话题订阅（事件追踪）───────────────────────────────
    # 结果不并入返回值：引擎会把返回的 items 统一按 collected 落库，而话题
    # 内容要标 origin=topic（升级链 collected > topic > searched），只能在
    # 这里用 TWEET_SINK 的 store 单独入库。没绑定 store（测试/一次性脚本）
    # 时跳过话题采集——丢的是附加视图，账号主线不受影响。
    topic_tags: list[tuple[str, list[str]]] = []
    if config.topics and TWEET_SINK.store is not None:
        for topic in config.topics:
            try:
                fetched, records, _ = router.search(
                    topic.query,
                    topic.limit,
                    product="Top" if topic.sort == "top" else "Latest",
                )
            except SourcePilotError as exc:
                # 单个话题失败不拖垮别的话题，更不拖垮账号主线
                log.warning("X 话题「%s」搜索失败：%s", topic.name, exc.code.value)
                continue
            # X 只保证查询词在整段文本里出现，不保证它们相关。先做点赞和主要
            # 内容位置校验，再按作者限额保留 X 原排序；全是确定性规则，不引入 LLM。
            upstream_count = len(records)
            relevant = [r for r in records if topic_record_matches(topic, r)]
            relevant_ids = {r.tweet_id for r in relevant}
            irrelevant_ids = [
                r.tweet_id for r in records if r.tweet_id not in relevant_ids
            ]
            if irrelevant_ids:
                # 规则收紧后，同一条推文若曾被旧规则误打过标签，要撤销标签；
                # 原始推文仍保留，只有 topic-only 条目会降级为 searched、退出信息流。
                TWEET_SINK.store.untag_tweet_topics(irrelevant_ids, topic.name)
            records = limit_topic_authors(topic, relevant)
            kept_ids = {r.tweet_id for r in records}
            fetched = [it for it in fetched if it.id.split(":", 1)[-1] in kept_ids]
            log.info(
                "X 话题「%s」质量过滤：上游 %d，相关 %d，作者限额后 %d",
                topic.name,
                upstream_count,
                len(relevant),
                len(records),
            )
            if not records:
                continue
            TWEET_SINK.store.upsert_items(fetched, origin="topic")
            tweet_records.extend(records)
            topic_tags.append((topic.name, [r.tweet_id for r in records]))

    # 长文正文单独补。放在写库之后：推文先落地，正文补不到也不影响主线。
    router.fill_articles(tweet_records, TWEET_SINK.store)
    TWEET_SINK.write(tweet_records)
    # 打标必须在 write 之后——tag 是对已存在行的合并更新
    if TWEET_SINK.store is not None:
        for name, ids in topic_tags:
            TWEET_SINK.store.tag_tweet_topics(ids, name)
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
