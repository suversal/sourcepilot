"""公众平台后端——**主力路线**，数据最全。

- ✅ 能按公众号精确拉文章列表，链接是永久的 `mp.weixin.qq.com/s/...`
- ⚠️ 必须有登录态：两个接口匿名请求一律回
  `{"ret": 200003, "err_msg": "invalid session"}`（实测 2026-07-26）
- ⚠️ 有账号风险：这是后台接口不是开放 API，抓太狠会被封。所以账号之间留间隔，
  凭据一失效立刻停手交给降级链。

凭据由使用者自己取得（浏览器里手动复制，或跑 login.py 扫码），
本模块只负责读取和使用，从不索要、不记录明文到日志。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml

from ...contracts import (
    AuthExpired,
    Item,
    Media,
    MediaType,
    RateLimited,
    Source,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from ...settings import PROJECT_ROOT
from ...sources.engine import normalize_url

log = logging.getLogger("sourcepilot.channels.wechat")

CREDENTIALS_FILE = PROJECT_ROOT / "config" / "wechat_credentials.yaml"
SEARCH_BIZ = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
APPMSG_LIST = "https://mp.weixin.qq.com/cgi-bin/appmsg"

#: 公众平台的业务错误码。它自己永远回 HTTP 200，真正的状态在响应体里。
RET_OK = 0
RET_INVALID_SESSION = 200003
RET_FREQ_LIMIT = 200013


class Credentials:
    """公众平台登录态。明文只在内存里，不进日志、不进响应。"""

    __slots__ = ("token", "cookie")

    def __init__(self, token: str, cookie: str) -> None:
        self.token = token
        self.cookie = cookie

    @classmethod
    def load(cls, path=None) -> Credentials | None:
        path = path or CREDENTIALS_FILE
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        token, cookie = str(data.get("token", "")), str(data.get("cookie", ""))
        if not token or not cookie:
            return None
        return cls(token, cookie)

    def __repr__(self) -> str:  # 防止 token 被日志或异常栈带出去
        return "<Credentials token=*** cookie=***>"


class WechatClient:
    def __init__(self, credentials: Credentials, timeout: float = 15.0) -> None:
        self._creds = credentials
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://mp.weixin.qq.com/",
            "Cookie": self._creds.cookie,
        }

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "token": self._creds.token, "lang": "zh_CN", "f": "json", "ajax": 1}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise UpstreamDown("公众平台请求超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamDown(f"公众平台连接失败：{type(exc).__name__}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamDown("公众平台返回的不是合法 JSON") from exc

        ret = (payload.get("base_resp") or {}).get("ret", RET_OK)
        if ret == RET_INVALID_SESSION:
            # 对外只说「平台侧不可用」，不暴露是哪个账号、为什么失效（契约 §5）。
            raise AuthExpired("公众号采集暂不可用")
        if ret == RET_FREQ_LIMIT:
            raise RateLimited("公众平台触发频率限制")
        if ret != RET_OK:
            raise UpstreamDown(f"公众平台返回业务错误 {ret}")
        return payload

    def search_account(self, keyword: str) -> dict[str, Any] | None:
        """搜公众号，拿它的 fakeid。fakeid 才是拉文章列表的钥匙。

        匹配优先级是有讲究的：**微信号（alias）> 昵称 > 第一条结果**。
        微信号是全平台唯一且不可改的，昵称既会改也会重名——搜「智谱AI」
        命中过一个 2022 年就停更的同名号。所以拿微信号搜时要让它精确命中，
        而不是被某个昵称更像的结果抢先。
        """
        payload = self._get(
            SEARCH_BIZ, {"action": "search_biz", "begin": 0, "count": 5, "query": keyword}
        )
        candidates = payload.get("list") or []
        for item in candidates:
            if item.get("alias") == keyword:
                return item
        for item in candidates:
            if item.get("nickname") == keyword:
                return item
        return candidates[0] if candidates else None

    def list_articles(self, fakeid: str, count: int = 20) -> list[dict[str, Any]]:
        """拉某个公众号的文章列表。

        用 `appmsg?action=list_ex` 而不是 `appmsgpublish`：后者返回的是被转义两层的
        publish_page 字符串（publish_list → publish_info → appmsgex），解析链长且脆；
        前者直接给扁平的 app_msg_list，字段一目了然。实测同一个号，list_ex 一次给
        20 条、字段齐全（aid/title/digest/link/update_time）。
        """
        payload = self._get(
            APPMSG_LIST,
            {
                "action": "list_ex",
                "begin": 0,
                "count": min(count, 20),  # 对方单页上限就是 20
                "fakeid": fakeid,
                "type": 9,
                "query": "",
            },
        )
        articles = payload.get("app_msg_list")
        if articles is None:
            raise UpstreamDown("响应里没有 app_msg_list——多半是对方改版了")
        return articles


def _to_item(article: dict[str, Any], account: str, now: datetime) -> Item | None:
    aid, title, link = article.get("aid"), article.get("title"), article.get("link")
    if not aid or not title or not link:
        return None

    stamp = article.get("update_time") or article.get("create_time")
    published = datetime.fromtimestamp(int(stamp), tz=UTC) if stamp else None

    return Item(
        id=f"wechat:{account}_{aid}",
        source=Source(type=SourceType.WECHAT, name=f"公众号 · {account}", platform=account),
        title=str(title)[:500],
        summary=(article.get("digest") or None),
        url=normalize_url(str(link)),
        author=account,
        published_at=published,
        discovered_at=now,
        time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
        # 公众号是按时间倒序的订阅流，不是排行榜——没有热度信号可言。
        score=0.0,
        categories=[],
        lang="zh",
        media=(
            [Media(type=MediaType.IMAGE, url=article["cover"])] if article.get("cover") else []
        ),
        raw={"aid": aid, "backend": "mp", "appmsgid": article.get("appmsgid")},
    )


class MpBackend:
    name = "mp"
    needs_credentials = True

    def available(self) -> bool:
        return Credentials.load() is not None

    def fetch(self, account, limit: int) -> list[Item]:
        credentials = Credentials.load()
        if credentials is None:
            raise AuthExpired("公众号采集未配置")

        name = getattr(account, "name", account)
        fakeid = getattr(account, "fakeid", None)

        client = WechatClient(credentials)
        now = datetime.now(UTC)
        if fakeid is None:
            # 没配 fakeid 才回退到按名字搜。这条路有两个已知代价，所以只是兜底：
            # 搜索是公众平台上最容易触发风控的动作，而且同名号/停更旧号很常见
            # ——实测搜「智谱AI」命中的是个 2022 年就停更的号。
            found = client.search_account(name)
            if not found or not found.get("fakeid"):
                log.warning("公众号 %s 搜不到", name)
                return []
            fakeid = found["fakeid"]

        items = []
        for article in client.list_articles(fakeid, limit):
            item = _to_item(article, name, now)
            if item is not None:
                items.append(item)
        return items
