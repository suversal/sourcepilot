"""X 内部 GraphQL 后端——**搜索的唯一可行路径**，需要登录态。

实测（2026-07-26）确认过：免登录搜索已经没有路了——Nitter 各实例的搜索一律返回 0 条，
xcancel 要 RSS 白名单，X 自己的 guest token 虽然还能激活，但旧的
`/2/search/adaptive.json` 已下线。所以「现场搜 X」这个差异点必须走登录 GraphQL。

三层反爬按需叠加，不一次全上（能少用就少用，每一层都是维护成本）：

  第一层  cookie + ct0 csrf + 公开 Bearer      —— 必需
  第二层  TLS/JA3 指纹伪装（curl_cffi）        —— 撞 Cloudflare 时才开
  第三层  x-client-transaction-id 动态签名     —— **搜索必需，时间线不需要**

第三层原本打算「先不带它试，被拒了再挂」。这个假设已经在真实登录态浏览器里
验证过了（2026-07-26），结论是**按 operation 分化的**：

    UserByScreenName / UserTweets / UserMedia   不带签名 → 200
    SearchTimeline                              不带签名 → 404
                                                带截获的签名重放 → 仍 404

最后那条说明签名带时间戳或 nonce、**一次性**，截获复用无效——必须能现场生成。
所以时间线可以立刻用，搜索则绕不开复刻签名算法（见 SIGNED_OPERATIONS）。
`transaction_signer` 传 None 就是关闭该层。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ...contracts import (
    AuthExpired,
    Item,
    Media,
    MediaType,
    Source,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from .accounts import Account, AccountPool
from .article import parse as parse_article
from .config import (
    ARTICLE_FIELD_TOGGLES,
    DEFAULT_FEATURES,
    GRAPHQL_BASE,
    OPERATIONS,
    PUBLIC_BEARER,
    SIGNED_OPERATIONS,
    USER_FEATURES,
)
from .tweet import TweetRecord, from_graphql

log = logging.getLogger("sourcepilot.channels.x")

#: 密钥缓存时长。X 发版没有固定节奏，所以既靠 404 触发重取（快），
#: 也靠这个上限兜底（防止某些静默失效不表现为 404）。
SIGNER_TTL = 30 * 60
#: 解析失败后的重试间隔。解析一次要拉好几 MB，失败也不是一秒内能好的。
SIGNER_RETRY_AFTER = 5 * 60


class TransactionSigner(Protocol):
    """x-client-transaction-id 的生成器。可插拔——X 不强制时就不挂。"""

    def sign(self, method: str, path: str) -> str: ...


def _score(legacy: dict[str, Any]) -> float:
    """互动量归一化，口径与 FxTwitter 后端一致，免得同一条推两个后端给出不同分。"""
    stats = (
        int(legacy.get("favorite_count") or 0)
        + int(legacy.get("retweet_count") or 0) * 2
        + int(legacy.get("reply_count") or 0)
    )
    return round(min(stats / (stats + 1000.0), 1.0), 4) if stats else 0.0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # X 的格式：Wed Jul 22 13:00:00 +0000 2026
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)
    except ValueError:
        return None


def _media(legacy: dict[str, Any]) -> list[Media]:
    out: list[Media] = []
    entities = (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []
    for entry in entities:
        url = entry.get("media_url_https")
        if not url:
            continue
        is_video = entry.get("type") in ("video", "animated_gif")
        kind = MediaType.VIDEO if is_video else MediaType.IMAGE
        out.append(Media(type=kind, url=url))
    return out


def tweet_result_to_item(result: dict[str, Any], now: datetime) -> Item | None:
    """把 GraphQL 的 TweetResult 拍平成 Item。

    X 的响应嵌套很深且形状不稳（推文可能被包在 `tweet` 里，用户在
    `core.user_results.result.legacy`），所以每一层都用 get 兜住。
    """
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}

    legacy = result.get("legacy") or {}
    tweet_id = legacy.get("id_str") or result.get("rest_id")
    if not tweet_id:
        return None

    user = (
        ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    )
    handle = (user.get("legacy") or {}).get("screen_name") or (user.get("core") or {}).get(
        "screen_name"
    )
    if not handle:
        return None

    # 长推文的全文在 note_tweet 里，legacy.full_text 是被截断的。
    note = (
        ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    )
    text = (note.get("text") or legacy.get("full_text") or "").strip()
    if not text:
        return None

    published = _parse_time(legacy.get("created_at"))
    return Item(
        id=f"x:{tweet_id}",
        source=Source(type=SourceType.X, name="X / Twitter", platform="x"),
        title=text[:80],
        # summary 恒为完整正文，**即使短于 title 的截断长度**。
        # title 是 80 字截断版（契约要求 title 非空，而推文没有标题），
        # 让 summary 只在「超过 80 字」时才有值的话，下游取正文就得写
        # `summary or title`——一个字段的语义不该随长度变化。
        summary=text,
        url=f"https://x.com/{handle}/status/{tweet_id}",
        author=handle,
        published_at=published,
        discovered_at=now,
        time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
        score=_score(legacy),
        categories=[],
        lang=legacy.get("lang"),
        media=_media(legacy),
        raw={
            "backend": "graphql",
            "likes": legacy.get("favorite_count"),
            "retweets": legacy.get("retweet_count"),
            "replies": legacy.get("reply_count"),
            "views": (result.get("views") or {}).get("count"),
        },
    )


def walk_timeline(
    payload: dict[str, Any], now: datetime
) -> tuple[list[Item], list[TweetRecord], str | None]:
    """遍历 timeline 指令，抽出推文、推文全貌与下一页游标。

    同时产出两种形状是刻意的：Item 进信息流参与跨源检索，TweetRecord 保留
    推文原貌（互动数、引用链、展开外链）供需要它的消费方使用。两者从**同一份
    响应**解析，不会出现「信息流里有、推文表里没有」的偏差。

    X 把结果放在 instructions[].entries[] 里，条目类型混杂（推文、游标、模块），
    所以按 entryId 前缀分派，遇到不认识的类型就跳过而不是报错——
    X 随时会加新类型，为此整条崩掉不值得。
    """
    items: list[Item] = []
    records: list[TweetRecord] = []
    cursor: str | None = None

    def take(result: dict[str, Any]) -> None:
        """一份响应同时产出两种形状，保证它们不会各自解析出不同的集合。"""
        parsed = tweet_result_to_item(result, now)
        if parsed is None:
            return
        items.append(parsed)
        record = from_graphql(result, now, parsed.published_at)
        if record is not None:
            records.append(record)

    def visit_entry(entry: dict[str, Any]) -> None:
        nonlocal cursor
        entry_id = entry.get("entryId") or ""
        content = entry.get("content") or {}

        if entry_id.startswith("cursor-bottom") or content.get("cursorType") == "Bottom":
            cursor = content.get("value") or cursor
            return

        # 单条推文
        item_content = content.get("itemContent") or {}
        result = ((item_content.get("tweet_results") or {}).get("result")) or {}
        if result:
            take(result)
            return

        # 模块（会话串等）里还有一层
        for sub in content.get("items") or []:
            sub_content = (sub.get("item") or {}).get("itemContent") or {}
            sub_result = ((sub_content.get("tweet_results") or {}).get("result")) or {}
            if sub_result:
                take(sub_result)

    data = payload.get("data") or {}
    timeline = (
        (data.get("search_by_raw_query") or {}).get("search_timeline")
        or ((data.get("user") or {}).get("result") or {}).get("timeline_v2")
        or ((data.get("user") or {}).get("result") or {}).get("timeline")
        or {}
    ).get("timeline") or {}

    for instruction in timeline.get("instructions") or []:
        for entry in instruction.get("entries") or []:
            visit_entry(entry)
        if instruction.get("entry"):
            visit_entry(instruction["entry"])

    return items, records, cursor


class GraphQLBackend:
    name = "graphql"
    supports = frozenset({"search", "timeline"})

    def __init__(
        self,
        pool: AccountPool | None = None,
        timeout: float = 10.0,
        impersonate: str | None = None,
        transaction_signer: TransactionSigner | None = None,
    ) -> None:
        self.pool = pool if pool is not None else AccountPool.load()
        self.timeout = timeout
        self.impersonate = impersonate
        self.signer = transaction_signer
        #: 外部注入的签名器由调用方负责生命周期，我们不去动它。
        self._signer_is_managed = transaction_signer is None
        self._signer_loaded_at = 0.0
        self._signer_failed_at = 0.0

    def _ensure_signer(self, account: Account, *, force: bool = False):
        """按需解析签名密钥，**并在过期或被拒后重取**。

        只在真要用签名的 operation 上才解析——那要拉页面和若干 MB 的 chunk，
        时间线那类不需要签名的请求不该为此付代价。

        为什么必须能重取：密钥是从 X 某一次前端构建的页面里算出来的，
        **X 一发版它就失效**。只解析一次的话，搜索会从某一刻起一直 404，
        而且不重启进程就永远好不了——这种「今天好好的、明天突然全坏且不自愈」
        的故障最难排查。
        """
        if not self._signer_is_managed:
            return self.signer

        now = time.time()
        if not force and self.signer is not None and now - self._signer_loaded_at < SIGNER_TTL:
            return self.signer
        # 刚失败过就别连着重试——解析一次要拉好几 MB，失败通常也不是一秒内能好的。
        if not force and self.signer is None and now - self._signer_failed_at < SIGNER_RETRY_AFTER:
            return None

        try:
            from .signature import XTransactionSigner

            self.signer = XTransactionSigner.load(account.cookie, account.user_agent)
            self._signer_loaded_at = now
            log.info("X 签名密钥已%s", "重新解析" if force else "解析")
        except Exception as exc:
            log.warning("X 签名密钥解析失败：%s", exc)
            self.signer = None
            self._signer_failed_at = now
        return self.signer

    def refresh_signer(self, account: Account | None = None):
        """强制重取密钥。模块文档承诺过这个方法，现在它真的存在了。"""
        account = account or (self.pool.accounts[0] if self.pool.accounts else None)
        if account is None:
            return None
        return self._ensure_signer(account, force=True)

    def available(self) -> bool:
        return bool(self.pool.accounts)

    def _request(
        self,
        account: Account,
        operation: str,
        variables: dict[str, Any],
        *,
        features: dict[str, bool] | None = None,
        field_toggles: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict:
        import json

        query_id = OPERATIONS.get(operation)
        if not query_id:
            raise UpstreamDown(f"没有配置 {operation} 的 operation id")
        if operation in SIGNED_OPERATIONS and self._ensure_signer(account) is None:
            # 与其发出去等一个语焉不详的 404，不如直接说清楚缺什么。
            raise AuthExpired(
                f"{operation} 需要 x-client-transaction-id 签名，但密钥解析失败。"
                f"该签名是一次性的，不能截获复用——见 channels/x/config.py 的实测记录"
            )

        path = f"/{query_id}/{operation}"
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features or DEFAULT_FEATURES, separators=(",", ":")),
        }
        if field_toggles is not None:
            params["fieldToggles"] = json.dumps(field_toggles, separators=(",", ":"))
        headers = account.headers(PUBLIC_BEARER)
        if self.signer is not None:
            headers["x-client-transaction-id"] = self.signer.sign("GET", f"/i/api/graphql{path}")

        url = f"{GRAPHQL_BASE}{path}"
        try:
            if self.impersonate:
                from curl_cffi import requests as curl_requests

                response = curl_requests.get(
                    url, params=params, headers=headers,
                    timeout=self.timeout, impersonate=self.impersonate,
                )
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get(url, params=params, headers=headers)
        except Exception as exc:
            raise UpstreamDown(f"X GraphQL 连接失败：{type(exc).__name__}") from exc

        # 先过状态机：它负责区分「限流」与「账号废了」，并在必要时抛错。
        self.pool.classify(account, operation, response)

        if response.status_code == 404:
            # 404 有两种可能：签名过期，或 operation id 过期。前者能自愈，先试它。
            if operation in SIGNED_OPERATIONS and not _retried and self._signer_is_managed:
                log.info("%s 返回 404，重取签名密钥后重试一次", operation)
                self._ensure_signer(account, force=True)
                if self.signer is not None:
                    return self._request(
                        account,
                        operation,
                        variables,
                        features=features,
                        field_toggles=field_toggles,
                        _retried=True,
                    )
            # 重取过还是 404，那多半就是 operation id 过期了——那个只能靠人改配置。
            raise UpstreamDown(
                f"{operation} 返回 404（已尝试重取签名）——operation id 多半过期了，"
                f"更新 channels/x/config.py 里的 OPERATIONS"
            )
        if response.status_code != 200:
            raise UpstreamDown(f"X GraphQL 返回 {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamDown("X GraphQL 返回的不是 JSON") from exc

    def search(
        self, query: str, limit: int, cursor: str | None = None
    ) -> tuple[list[Item], str | None]:
        account = self.pool.acquire("SearchTimeline")
        # 参数照抄浏览器里的真实请求（2026-07-26 抓取）。
        variables = {
            "rawQuery": query,
            "count": min(limit, 20),
            "querySource": "",
            "product": "Latest",  # 按时间倒序，不是「热门」——资讯要的是最新
            "withGrokTranslatedBio": False,
            "withQuickPromoteEligibilityTweetFields": False,
        }
        if cursor:
            variables["cursor"] = cursor
        payload = self._request(account, "SearchTimeline", variables)
        return walk_timeline(payload, datetime.now(UTC))

    def timeline(
        self, user_id: str, limit: int, cursor: str | None = None
    ) -> tuple[list[Item], str | None]:
        account = self.pool.acquire("UserTweets")
        variables = {
            "userId": user_id,
            "count": min(limit, 20),
            # 真实请求里这三个都是 true；改成 false 属于「自作聪明」，
            # 参数组合与前端不一致本身就可能被当成异常流量。
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor
        payload = self._request(
            account, "UserTweets", variables, field_toggles={"withArticlePlainText": False}
        )
        return walk_timeline(payload, datetime.now(UTC))

    def fetch_article(self, tweet_id: str) -> dict[str, Any] | None:
        """取一条推文挂载的长文正文。

        **必须单独请求**：搜索与时间线返回的 article 只有 preview_text，
        正文要靠 ARTICLE_FIELD_TOGGLES 打开。没有长文的推文返回 None，
        调用方据此跳过——所以调用前先看 `has_article`，别对每条推文都问一遍。
        """
        account = self.pool.acquire("TweetResultByRestId")
        payload = self._request(
            account,
            "TweetResultByRestId",
            {
                "tweetId": tweet_id,
                "includePromotedContent": True,
                "withBirdwatchNotes": True,
                "withVoice": True,
                "withCommunity": True,
            },
            field_toggles=ARTICLE_FIELD_TOGGLES,
        )
        result = ((payload.get("data") or {}).get("tweetResult") or {}).get("result") or {}
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet") or {}
        article = ((result.get("article") or {}).get("article_results") or {}).get("result")
        return parse_article(article or {})

    def user_id(self, handle: str) -> str | None:
        account = self.pool.acquire("UserByScreenName")
        payload = self._request(
            account,
            "UserByScreenName",
            {"screen_name": handle.lstrip("@"), "withGrokTranslatedBio": True},
            # 这个 operation 用的是另一套更短的 features，给错会被拒。
            features=USER_FEATURES,
            field_toggles={"withPayments": False, "withAuxiliaryUserLabels": True},
        )
        return (((payload.get("data") or {}).get("user") or {}).get("result") or {}).get("rest_id")
