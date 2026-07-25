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
APPMSG_PUBLISH = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"

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
        """按名称搜公众号，拿它的 fakeid。fakeid 才是拉文章列表的钥匙。"""
        payload = self._get(
            SEARCH_BIZ, {"action": "search_biz", "begin": 0, "count": 5, "query": keyword}
        )
        for item in payload.get("list") or []:
            if item.get("nickname") == keyword:
                return item
        return (payload.get("list") or [None])[0]

    def list_articles(self, fakeid: str, count: int = 20) -> list[dict[str, Any]]:
        payload = self._get(
            APPMSG_PUBLISH,
            {"sub": "list", "begin": 0, "count": count, "fakeid": fakeid, "type": 101_0325},
        )
        # publish_page 是一段被转义的 JSON 字符串，得二次解析。
        import json

        raw = payload.get("publish_page")
        if not raw:
            return []
        try:
            page = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise UpstreamDown("publish_page 解析失败——多半是对方改版了") from exc

        articles: list[dict[str, Any]] = []
        for group in page.get("publish_list") or []:
            info = group.get("publish_info")
            if not info:
                continue
            try:
                detail = json.loads(info) if isinstance(info, str) else info
            except ValueError:
                continue
            articles.extend(detail.get("appmsgex") or [])
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
        media=[],
        raw={"aid": aid, "backend": "mp"},
    )


class MpBackend:
    name = "mp"
    needs_credentials = True

    def available(self) -> bool:
        return Credentials.load() is not None

    def fetch(self, account: str, limit: int) -> list[Item]:
        credentials = Credentials.load()
        if credentials is None:
            raise AuthExpired("公众号采集未配置")

        client = WechatClient(credentials)
        now = datetime.now(UTC)
        found = client.search_account(account)
        if not found or not found.get("fakeid"):
            log.warning("公众号 %s 搜不到", account)
            return []

        items = []
        for article in client.list_articles(found["fakeid"], limit):
            item = _to_item(article, account, now)
            if item is not None:
                items.append(item)
        return items
