"""微信读书后端——公众平台被关掉之后的**主力路线**。

2026-07-30 前后微信收紧了公众平台后台的跨公众号文章列表接口（见 mp.py 的
`CROSS_ACCOUNT_LIST_CLOSED`），一批依赖它的开源项目同时失效。微信读书是**另一套
系统**，不受这次变更影响——实测 2026-08-06，公开的 Wechat2RSS 服务（其部署文档
写明「服务通过读书获取公众号信息」）仍在输出当天的文章。

核心事实：**微信读书把公众号当成「书」**。

    bookId = "MP_WXS_" + base64解码(fakeid)

而 `fakeid` 就是公众号的 `__biz`，我们配置里 23 个号全都有——所以不需要像其它
实现那样为每个号手工提供一篇文章链接来反查身份。

三个必须知道的坑（都实测/文档确认过，不是猜的）：

1. **`/web/mp/articles` 必须带阅读器页的 Referer。** 在微信读书首页发同样的请求
   一律回 `errCode: -2041`——那是上下文校验，不是限流。阅读器页地址形如
   `/web/mp/reader/<hash>`，而**那串 hash 不能自己拼**：它每个号各不相同，
   只能从书架接口的 `deepLink` 里取（`?v=<hash>`）。

   **但那个校验只看「Referer 是不是一个合法的阅读器页」，不看它跟 bookId 配不配**
   （实测 2026-08-06：拿书架里某个 Kimi 号的阅读器页当 Referer，去拉根本不在书架里
   的量子位，返回 77 篇；机器之心 53 篇，最新都是当天的）。所以书架只用来换一张
   **通行证**，用户不必把要订阅的号一个个加进微信读书书架——参考实现里那条
   「每个号都要先加书架」的前提是多余的。书架里有任意一个公众号即可。
2. **一次群发 = 一个 `reviews` 条目，里面的 `subReviews` 才是一篇篇文章。**
   只读 `subReviews[0]` 会丢掉同一次群发的其余文章，有的号一天群发 3–4 篇。
3. **这条路有风控。** 参考实现的作者一天请求 30 多次就触发反爬（页面白屏几小时）。
   所以账号间隔别调小，`min_interval` 也别按小时级设——配置见 wechat.yaml。

固有局限（平台侧的，技术上无解，别当 bug 修）：

- **收录滞后**：部分公众号在微信读书侧收录慢，实测遇到过滞后半个多月的。
- **不实时**：通常比公众号发布晚几个小时。
- **书架里至少要有一个公众号**：用来换阅读器页通行证（见上）。一次性的，
  加哪个号都行。一个都没有时本模块会明确说怎么加。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml

from ...contracts import (
    AuthExpired,
    Item,
    RateLimited,
    Source,
    SourcePilotError,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from ...settings import PROJECT_ROOT
from ...sources.engine import normalize_url

log = logging.getLogger("sourcepilot.channels.wechat")

CREDENTIALS_FILE = PROJECT_ROOT / "config" / "weread_credentials.yaml"
WEREAD_BASE = "https://weread.qq.com"
SHELF_URL = f"{WEREAD_BASE}/web/shelf/sync"
ARTICLES_URL = f"{WEREAD_BASE}/web/mp/articles"
READER_PREFIX = f"{WEREAD_BASE}/web/mp/reader/"
BOOK_ID_PREFIX = "MP_WXS_"

#: 微信读书的业务错误码。它和公众平台一样：HTTP 200，真状态在响应体里。
ERR_CONTEXT_REQUIRED = -2041  # 请求没发在阅读器页上下文里
ERR_NO_SUCH_USER = -2010  # 用户不存在
ERR_LOGIN_TIMEOUT = -2012  # 「登录超时」——cookie 过期，要重新登录 weread.qq.com
#: 这两个都是登录态问题，报 AUTH_EXPIRED 让冷却状态机按「要人介入」退避。
#: 关键在于 AUTH_EXPIRED 属于 BACKEND_LEVEL_FAILURES：第一个号失败就冷却整个后端，
#: 后面 22 个号直接跳过。否则每个号都会重试一次书架——23 次无谓请求打在一个
#: 有反爬的接口上，正是把额度耗光的那种打法。
_AUTH_ERRORS = frozenset({ERR_NO_SUCH_USER, ERR_LOGIN_TIMEOUT})

_DEEP_LINK_V = re.compile(r"[?&]v=([^&]+)")


def normalize_original_id(original_id: str) -> str:
    """微信读书回的 `originalId` 把下划线换成了 `~`，拼 URL 前必须换回来。

    微信文章 id 用的是 base64url 字符集（`A-Za-z0-9_-`），**不含 `~`**。
    实测 2026-08-06：`XK6ymJL7y0vo~GQXxmpuBA` 直接请求回「参数错误」，
    把 `~` 换成 `_` 后正常打开（DeepSeek-V3 那篇）；换成 `-` 仍是参数错误。
    `~~` 连着两个的也一样（`U5fnTRW4cGvXYJER~~YBiw` → `__`）。

    只动 `~`，其余字符原样保留——`-` 在真实 id 里本来就出现（如
    `nL--rVri3qAy~6Recsg~4g`），乱替换会把好的 id 改坏。
    """
    return original_id.replace("~", "_")


class WereadCredentials:
    """微信读书登录态。只有 cookie，没有 token——与公众平台是两套凭据。

    明文只在内存里，不进日志、不进响应（同 mp.Credentials 的约定）。
    """

    __slots__ = ("cookie",)

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie

    @classmethod
    def load(cls, path=None) -> WereadCredentials | None:
        path = path or CREDENTIALS_FILE
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cookie = str(data.get("cookie", ""))
        if not cookie:
            return None
        return cls(cookie)

    def __repr__(self) -> str:  # 防止 cookie 被日志或异常栈带出去
        return "<WereadCredentials cookie=***>"


def book_id_for(fakeid: str) -> str | None:
    """fakeid（= 公众号的 __biz）→ 微信读书的 bookId。

    `MzIzNjc1NzUzMw==` → `MP_WXS_3236757533`。解不出来就返回 None——
    宁可跳过这个号，也不要拿一个乱拼的 bookId 去请求。
    """
    try:
        decoded = base64.b64decode(fakeid, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not decoded.isdigit():
        return None
    return f"{BOOK_ID_PREFIX}{decoded}"


class WereadClient:
    def __init__(self, credentials: WereadCredentials, timeout: float = 20.0) -> None:
        self._creds = credentials
        self.timeout = timeout
        self._shelf: dict[str, str] | None = None
        self._shelf_error: SourcePilotError | None = None

    def _headers(self, referer: str = WEREAD_BASE) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "Cookie": self._creds.cookie,
        }

    def _get(self, url: str, params: dict[str, Any], referer: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params, headers=self._headers(referer))
        except httpx.TimeoutException as exc:
            raise UpstreamDown("微信读书请求超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamDown(f"微信读书连接失败：{type(exc).__name__}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            # 触发风控时返回的是 HTML（页面白屏），不是 JSON。这不是「改版了」，
            # 是被限了——报 RateLimited 让冷却状态机退避，而不是催人去改解析规则。
            raise RateLimited(
                "微信读书返回的不是 JSON——多半触发了风控，停手等几小时"
            ) from exc

        err = payload.get("errCode")
        if err in _AUTH_ERRORS:
            # 日志里说清楚怎么修（运维看得到），对外只说平台侧不可用（契约 §5：
            # 不暴露账号细节）。
            log.warning(
                "微信读书登录态失效（errCode=%s %s）：重新登录 weread.qq.com 后"
                "更新 config/weread_credentials.yaml 的 cookie",
                err,
                payload.get("errMsg") or "",
            )
            raise AuthExpired("公众号采集暂不可用")
        if err == ERR_CONTEXT_REQUIRED:
            raise UpstreamDown(
                "微信读书拒绝了请求上下文（-2041）：阅读器页地址可能已失效，"
                "下一轮会重新同步书架"
            )
        if err:
            raise UpstreamDown(f"微信读书返回业务错误 {err}")
        return payload

    def reader_ticket(self) -> str:
        """换一张阅读器页「通行证」，用作 `/web/mp/articles` 的 Referer。

        `-2041` 那道校验只认「Referer 是不是一个合法的阅读器页」，**不认它跟
        请求的 bookId 配不配**（实测见模块文档）。所以随便哪个号的阅读器页都行，
        书架里有一个就够——要订阅的号不必逐个加进书架。
        """
        shelf = self.shelf()
        if not shelf:
            raise UpstreamDown(
                "微信读书书架里一个公众号都没有，换不到阅读器页通行证。"
                "在微信里打开任意一个公众号的文章 → 分享 → 在微信读书中阅读，加一个即可"
            )
        return next(iter(shelf.values()))

    def shelf(self) -> dict[str, str]:
        """书架里的公众号 → 它的阅读器页地址。

        书架接口在首页上下文就能调，不需要 Referer。整轮采集只同步一次，
        结果缓存在实例里——每个请求都在消耗反爬额度。
        """
        if self._shelf is not None:
            return self._shelf
        if self._shelf_error is not None:
            # 失败也要记住。不记的话，非后端级的错误（比如一次网络抖动）会让
            # 23 个号各自重试一次书架——23 次无谓请求打在有反爬的接口上。
            raise self._shelf_error
        try:
            payload = self._get(
                SHELF_URL, {"synckey": 0, "teenmode": 0, "album": 1}, WEREAD_BASE
            )
        except SourcePilotError as exc:
            self._shelf_error = exc
            raise
        shelf: dict[str, str] = {}
        for book in payload.get("books") or []:
            book_id = str(book.get("bookId") or "")
            if not book_id.startswith(BOOK_ID_PREFIX):
                continue  # 书架里还有真的书，只要公众号
            match = _DEEP_LINK_V.search(str(book.get("deepLink") or ""))
            if match:
                shelf[book_id] = f"{READER_PREFIX}{match.group(1)}"
        self._shelf = shelf
        return shelf

    def articles(self, book_id: str, reader_url: str) -> list[dict[str, Any]]:
        payload = self._get(ARTICLES_URL, {"bookId": book_id, "offset": 0}, reader_url)
        return _flatten_reviews(payload)


def _flatten_reviews(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`reviews[].subReviews[].review.mpInfo` → 扁平的文章列表，按时间倒序。

    **一次群发是一个 `reviews` 条目，里面的 `subReviews` 才是一篇篇文章**——
    只取 `subReviews[0]` 会丢掉同一次群发的其余文章（有的号一天发 3–4 篇）。
    """
    out: list[dict[str, Any]] = []
    for group in payload.get("reviews") or []:
        for sub in group.get("subReviews") or []:
            review = sub.get("review") or {}
            info = review.get("mpInfo") or {}
            title = info.get("title")
            original_id = info.get("originalId")
            if not title or not original_id:
                continue
            out.append(
                {
                    "title": str(title),
                    "original_id": normalize_original_id(str(original_id)),
                    "created_at": review.get("createTime") or group.get("createTime") or 0,
                    "cover": info.get("cover"),
                    "digest": info.get("digest"),
                }
            )
    out.sort(key=lambda a: a["created_at"], reverse=True)
    return out


def _to_item(article: dict[str, Any], account: str, now: datetime) -> Item | None:
    original_id = article.get("original_id")
    title = article.get("title")
    if not original_id or not title:
        return None

    stamp = article.get("created_at")
    published = datetime.fromtimestamp(int(stamp), tz=UTC) if stamp else None

    return Item(
        # 与 mp 后端的 id 规则保持一致的前缀，但用 originalId 做 native_id——
        # 它就是永久链接 /s/<id> 里那一段，同一篇文章无论从哪个后端进来都同一个 id。
        id=f"wechat:{account}_{original_id}",
        source=Source(type=SourceType.WECHAT, name=f"公众号 · {account}", platform=account),
        title=str(title)[:500],
        summary=(article.get("digest") or None),
        url=normalize_url(f"https://mp.weixin.qq.com/s/{original_id}"),
        author=account,
        published_at=published,
        discovered_at=now,
        time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
        # 公众号是按时间倒序的订阅流，不是排行榜——没有热度信号可言。
        score=0.0,
        categories=[],
        lang="zh",
        media=[],
        raw={"original_id": original_id, "backend": "weread"},
    )


class WereadBackend:
    name = "weread"
    needs_credentials = True

    def __init__(self, account_interval: float = 3.0) -> None:
        #: 号与号之间的间隔。参考实现用 3 秒，作者实测一天 30 多次快速请求
        #: 就触发了风控——这个值别往小调。
        self.account_interval = account_interval
        self._client: WereadClient | None = None

    def available(self) -> bool:
        return WereadCredentials.load() is not None

    def _get_client(self) -> WereadClient:
        if self._client is None:
            credentials = WereadCredentials.load()
            if credentials is None:
                raise AuthExpired("公众号采集未配置")
            self._client = WereadClient(credentials)
        return self._client

    def fetch(self, account, limit: int) -> list[Item]:
        name = getattr(account, "name", account)
        fakeid = getattr(account, "fakeid", None)
        if not fakeid:
            # 微信读书搜不到公众号名（实测多种参数组合全返回图书），
            # 没有 fakeid 就没有 bookId，这个号只能跳过。
            log.warning("公众号 %s 没配 fakeid，微信读书后端无法定位（它搜不了名字）", name)
            return []

        book_id = book_id_for(fakeid)
        if book_id is None:
            log.warning("公众号 %s 的 fakeid 不是合法 __biz，解不出 bookId", name)
            return []

        client = self._get_client()
        # 通行证与这个号无关，随便哪个阅读器页都能过 -2041 那道校验（见模块文档）。
        reader_url = client.reader_ticket()

        if self.account_interval:
            time.sleep(self.account_interval)

        now = datetime.now(UTC)
        items: list[Item] = []
        for article in client.articles(book_id, reader_url)[:limit]:
            item = _to_item(article, name, now)
            if item is not None:
                items.append(item)
        return items
