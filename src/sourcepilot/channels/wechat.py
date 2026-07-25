"""公众号 channel。

**整块隔离**：这是全平台最可能被官方封掉的一条线（走的是公众平台后台接口，
不是官方开放 API）。所以它自成一个模块，凭据、客户端、归一化都在这里；
坏了就整块换掉，不牵动其它信源。

**必须有登录态**：`mp.weixin.qq.com` 的两个接口匿名请求一律回
`{"ret": 200003, "err_msg": "invalid session"}`。凭据由使用者自己扫码取得
（见 `login.py`），本模块只负责读取和使用，从不索要、不记录明文到日志。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml

from ..contracts import (
    AuthExpired,
    Item,
    RateLimited,
    Source,
    SourceType,
    TimeBasis,
    UpstreamDown,
)
from ..settings import PROJECT_ROOT
from ..sources.config import SourceConfig
from ..sources.engine import normalize_url

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
        raw={"aid": aid},
    )


def collect_wechat(config: SourceConfig) -> list[Item]:
    """channel 入口。由 sources.engine.collect 按 `channel: wechat` 分派进来。"""
    credentials = Credentials.load()
    if credentials is None:
        raise AuthExpired("公众号采集未配置")

    accounts = list(config.accounts or [])
    if not accounts:
        return []

    client = WechatClient(credentials)
    now = datetime.now(UTC)
    items: list[Item] = []

    for name in accounts:
        try:
            found = client.search_account(name)
            if not found or not found.get("fakeid"):
                log.warning("公众号 %s 搜不到", name)
                continue
            for article in client.list_articles(found["fakeid"], config.per_account_limit):
                item = _to_item(article, name, now)
                if item is not None:
                    items.append(item)
        except AuthExpired:
            raise  # 凭据失效是整块的事，不是单个账号的事
        except RateLimited:
            raise  # 被限流就停手，别继续捅
        except Exception as exc:
            # 单个公众号出问题不该拖垮整个 channel
            log.warning("公众号 %s 采集失败：%s", name, type(exc).__name__)
            continue
        # 公众平台对连续请求很敏感，账号之间留间隔。
        time.sleep(config.account_interval)

    return items
