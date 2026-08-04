"""`read_article`：读单篇已知 URL 的正文，转 Markdown。

这是唯一一个「按调用方给的地址去请求」的工具，所以它是本平台的 SSRF 面。
出网之前必须把地址钉死在公网 http(s) 上，见 `assert_public_url`。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import trafilatura

from . import htmlmd
from .contracts import (
    Article,
    BadRequest,
    Envelope,
    Meta,
    Mode,
    NotFound,
    ReadArticleParams,
    Timeout,
    UpstreamDown,
)
from .settings import DEFAULT_UA

log = logging.getLogger("sourcepilot.article")

#: 只允许普通网页端口。挡住 redis(6379)、mysql(3306) 这类「用 HTTP 探内网服务」的玩法。
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


def _is_public_ip(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False
    return bool(infos)


def assert_public_url(url: str) -> None:
    """确认这个地址指向公网上的普通网页，否则拒绝出网。

    本工具会去请求调用方给的任意地址，不设防的话，外部就能拿它当跳板探测
    内网——127.0.0.1、192.168.x.x、云厂商的 169.254.169.254 元数据接口都在
    射程内。所以在发请求之前把协议、端口、解析出的 IP 全部校验一遍。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BadRequest(f"只支持 http/https，收到 {parts.scheme!r}")
    if not parts.hostname:
        raise BadRequest("URL 里没有主机名")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BadRequest(f"端口 {port} 不在允许范围内")

    if not _is_public_ip(parts.hostname):
        # 不告诉调用方「这是内网地址」——那本身就是一条内网探测的反馈信号。
        raise BadRequest("目标地址不可达或不被允许")


@dataclass(frozen=True)
class SiteProfile:
    """正文容器已知的站点。

    只在通用提取器明显做不好时才加一条——每一条都是要跟着对方改版维护的。
    """

    hosts: tuple[str, ...]
    #: 容器候选，按顺序试第一个命中的。备一个是因为站点改版常常只是换类名。
    roots: tuple[str, ...]
    #: 容器内部要删掉的节点：文末推广、赞赏码、浮动目录——在正文里但不是正文。
    exclude: tuple[str, ...] = ()


SITE_PROFILES: tuple[SiteProfile, ...] = (
    SiteProfile(
        hosts=("mp.weixin.qq.com",),
        # 这个 id 多年没变；rich_media_content 是它的类名，作备胎。
        roots=("#js_content", ".rich_media_content"),
        exclude=(
            "#js_pc_qr_code",          # 公众号二维码
            ".rich_media_tool",        # 底部工具条
            ".reward_area",            # 赞赏
            ".qr_code_pc",
            "#js_sponsor_ad_area",     # 广告位
            "mpprofile",               # 内嵌的「公众号名片」卡片
            ".js_uneditable",          # 小程序/视频号卡片，抽出来是一串占位字
        ),
    ),
)


def _profile_for(url: str) -> SiteProfile | None:
    host = (urlsplit(url).hostname or "").lower()
    for profile in SITE_PROFILES:
        if any(host == h or host.endswith(f".{h}") for h in profile.hosts):
            return profile
    return None


class ArticleService:
    """现查工具：每次都真去取，不做缓存。

    正文体积大、复用率低，缓存它性价比不高；而且用户要读某篇文章时，
    要的就是当下的内容。
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def _fetch_impersonated(self, url: str):
        """普通请求被挡时，换 TLS 指纹再试一次。

        OpenAI 官网这类站点对裸 httpx 直接 403，但对伪装成浏览器的握手放行。
        只在被挡之后才走这条路——它不共用连接池，也更慢。
        """
        from curl_cffi import requests as curl_requests

        return curl_requests.get(
            url, headers={"User-Agent": DEFAULT_UA}, timeout=self.timeout, impersonate="safari"
        )

    def _fetch(self, url: str) -> tuple[str, str]:
        try:
            with httpx.Client(follow_redirects=True, timeout=self.timeout) as client:
                response = client.get(url, headers={"User-Agent": DEFAULT_UA})
        except httpx.TimeoutException as exc:
            raise Timeout("读取超时") from exc
        except httpx.HTTPError as exc:
            raise UpstreamDown(f"无法访问该地址：{type(exc).__name__}") from exc

        if response.status_code in (403, 429):
            try:
                response = self._fetch_impersonated(url)
            except Exception:
                pass  # 降级失败就按原状态码报错

        if response.status_code == 404:
            raise NotFound("该地址不存在")
        if response.status_code >= 400:
            raise UpstreamDown(f"目标返回 {response.status_code}")

        # 跟随重定向后要重新校验——不然「公网地址 302 到内网」就绕过了前面的检查。
        assert_public_url(str(response.url))
        return response.text, str(response.url)

    def get(self, params: ReadArticleParams) -> Envelope[Article]:
        started = time.perf_counter()
        url = str(params.url)
        assert_public_url(url)

        html, final_url = self._fetch(url)

        # 正文容器已知的站点走专用提取。trafilatura 擅长「猜正文在哪」，
        # 代价是激进降噪——实测公众号那篇 5360 字正文提得很干净，但 6 张配图
        # 和 4 个小标题全被当噪音丢了。容器已知就不必承担猜错的代价。
        markdown = None
        profile = _profile_for(final_url)
        if profile:
            markdown = htmlmd.extract(html, profile.roots, profile.exclude)
            if not markdown:
                # 站点改版把容器全改名了。回落到通用提取，宁可少格式也别整个失败。
                log.warning("%s 的正文容器都没选中，回落 trafilatura", final_url)

        markdown = markdown or trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            with_metadata=False,
        )
        if not markdown or not markdown.strip():
            raise UpstreamDown("这个页面抽不出正文——可能是纯前端渲染或纯图页面")

        meta = trafilatura.extract_metadata(html)
        published = None
        if meta and meta.date:
            try:
                published = datetime.fromisoformat(meta.date).replace(tzinfo=UTC)
            except ValueError:
                published = None

        truncated = len(markdown) > params.max_chars
        content = markdown[: params.max_chars]

        article = Article(
            url=final_url,
            title=(meta.title if meta and meta.title else "（无标题）"),
            author=(meta.author if meta and meta.author else None),
            published_at=published,
            content_markdown=content,
            char_count=len(content),
            truncated=truncated,
            lang=None,
            fetched_at=datetime.now(UTC),
        )
        return Envelope[Article].success(
            article,
            Meta(
                mode=Mode.LIVE,
                collected_at=article.fetched_at,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
