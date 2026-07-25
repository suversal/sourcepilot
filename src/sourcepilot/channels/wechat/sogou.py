"""搜狗微信后端——**免凭据的应急路线**。

它的定位是「公众平台那条路不通时，至少还能给出点东西」，不是主力：

- ✅ 完全匿名，零账号风险。主力被限流或凭据失效时，这条路不受影响。
- ⚠️ 文章链接是**限时签名链接**（`mp.weixin.qq.com/s?src=11&timestamp=…&signature=…`），
  几小时到一天后会失效。条目的 `raw.link_expires` 标了这一点，下游别当永久链接存。
- ⚠️ 每条都要多发一次请求去还原跳转，慢；发快了会触发验证码。
- ⚠️ 搜出来的是「提到这个词的文章」，不等于「这个公众号发的文章」，
  所以要按公众号名过滤一遍。

项目文档说「当前最稳路线是微信读书，不是 Sogou 逆向」——那说的是**主力**选型。
作为降级路线它是合适的：无凭据、无账号风险，代价是数据质量差一档。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ...contracts import Captcha, Item, Source, SourceType, TimeBasis, UpstreamDown
from ...settings import DEFAULT_UA

log = logging.getLogger("sourcepilot.channels.wechat")

SEARCH_URL = "https://weixin.sogou.com/weixin"
#: 跳转页里 JS 把真实地址拼在这些片段里。
_REAL_URL = re.compile(r"url \+= '([^']*)'")
_TIMESTAMP = re.compile(r"timeConvert\('(\d+)'\)")


def _looks_like_captcha(html: str, url: str) -> bool:
    return "antispider" in url or "请输入验证码" in html or "网络世界不太平" in html


class SogouBackend:
    name = "sogou"
    needs_credentials = False

    def __init__(self, timeout: float = 15.0, resolve_interval: float = 1.0) -> None:
        self.timeout = timeout
        self.resolve_interval = resolve_interval

    def available(self) -> bool:
        return True  # 不需要凭据，永远可以一试

    def _resolve(self, client: httpx.Client, href: str, referer: str) -> str | None:
        """把 /link?url=… 还原成真实的 mp.weixin.qq.com 地址。

        搜狗不在 HTTP 层跳转，而是返回一段 JS 把地址拼起来，所以得从页面里抠。
        """
        url = href if href.startswith("http") else f"https://weixin.sogou.com{href}"
        try:
            response = client.get(url, headers={"Referer": referer})
        except httpx.HTTPError:
            return None
        if _looks_like_captcha(response.text, str(response.url)):
            raise Captcha("搜狗触发验证码，不硬刚")
        parts = _REAL_URL.findall(response.text)
        return "".join(parts) if parts else None

    def fetch(self, account: str, limit: int) -> list[Item]:
        now = datetime.now(UTC)
        with httpx.Client(
            headers={"User-Agent": DEFAULT_UA}, timeout=self.timeout, follow_redirects=True
        ) as client:
            try:
                response = client.get(SEARCH_URL, params={"type": 2, "query": account})
            except httpx.HTTPError as exc:
                raise UpstreamDown(f"搜狗连接失败：{type(exc).__name__}") from exc

            if _looks_like_captcha(response.text, str(response.url)):
                raise Captcha("搜狗触发验证码，不硬刚")

            soup = BeautifulSoup(response.text, "lxml")
            rows = soup.select("ul.news-list li")
            if not rows:
                raise UpstreamDown("搜狗结果页没有条目——多半是改版或被拦")

            items: list[Item] = []
            for row in rows[:limit]:
                item = self._to_item(client, row, account, now, str(response.url))
                if item is not None:
                    items.append(item)
                time.sleep(self.resolve_interval)  # 还原跳转发太快会撞验证码
            return items

    def _to_item(
        self, client: httpx.Client, row: Any, account: str, now: datetime, referer: str
    ) -> Item | None:
        link = row.select_one("h3 a")
        if link is None:
            return None
        title = " ".join(link.get_text(" ", strip=True).split())

        publisher = row.select_one(".s-p .all-time-y2")
        if publisher is None or publisher.get_text(strip=True) != account:
            # 搜出来的是「提到这个词的文章」，不是「这个号发的文章」，得筛掉别家的。
            return None

        stamp_match = _TIMESTAMP.search(str(row))
        published = (
            datetime.fromtimestamp(int(stamp_match.group(1)), tz=UTC) if stamp_match else None
        )

        real_url = self._resolve(client, link.get("href", ""), referer)
        if not real_url:
            log.warning("搜狗条目还原链接失败：%s", title[:30])
            return None

        summary = row.select_one("p.txt-info")
        native_id = re.sub(r"[^A-Za-z0-9]+", "", real_url)[-40:]

        return Item(
            id=f"wechat:{account}_sg{native_id}",
            source=Source(type=SourceType.WECHAT, name=f"公众号 · {account}", platform=account),
            title=title[:500],
            summary=(" ".join(summary.get_text(" ", strip=True).split()) if summary else None),
            url=real_url,
            author=account,
            published_at=published,
            discovered_at=now,
            time_basis=TimeBasis.PUBLISHED if published else TimeBasis.DISCOVERED,
            score=0.0,
            categories=[],
            lang="zh",
            media=[],
            raw={
                "backend": "sogou",
                # 搜狗给的是限时签名链接，几小时到一天后失效。下游别当永久链接。
                "link_expires": True,
            },
        )
