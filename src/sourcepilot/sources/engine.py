"""声明式抓取引擎：配置 → HTTP → 归一化 Item。

所有源共用这一条路径，源之间的差异全在 YAML 里。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..categorize import get_categorizer
from ..contracts import (
    AuthExpired,
    Captcha,
    Item,
    Media,
    MediaType,
    RateLimited,
    Source,
    SourcePilotError,
    TimeBasis,
    Timeout,
    UpstreamDown,
)
from .config import FieldSpec, SourceConfig
from .extract import extract_row, resolve_path

log = logging.getLogger("sourcepilot.engine")

#: 追踪参数。不清掉的话，同一条内容会因为埋点串不同而躲过跨源去重。
TRACKING_PARAMS = frozenset(
    {
        "log_pb", "impr_id", "category_name", "event_type", "style_id", "rank",
        "topic_id", "spm_id_from", "from_spmid", "vd_source", "seid",
        "ref", "ref_src", "from_source",
    }
)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS
        and not k.startswith(("utm_", "share_"))
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _fetch_impersonated(config: SourceConfig) -> Any:
    """走 curl_cffi，在 TLS 层伪装成真实浏览器。

    对付 Cloudflare 那种「Just a moment...」挑战——它拦的是 TLS 握手指纹，
    改 UA 或补请求头都没用。代价是这条路不共用 httpx 连接池。
    """
    from curl_cffi import requests as curl_requests

    try:
        return curl_requests.request(
            config.request.method,
            config.request.url,
            headers=config.request.merged_headers(),
            json=config.request.json_body,
            timeout=config.request.timeout,
            impersonate=config.request.impersonate,
        )
    except Exception as exc:  # curl_cffi 的异常类型随版本变，统一按上游不可达处理
        if "timed out" in str(exc).lower():
            raise Timeout(f"{config.name} 请求超时") from exc
        raise UpstreamDown(f"{config.name} 连接失败：{type(exc).__name__}") from exc


def _classify_http_error(response: Any) -> SourcePilotError:
    """把 HTTP 现象翻成结构化错误码，让上层能分支决策。"""
    status = response.status_code
    body = response.text[:2000].lower()

    if status == 429:
        return RateLimited("上游限流")
    if "cf-ray" in response.headers or "captcha" in body or "验证码" in body:
        return Captcha("触发人机校验，不硬刚")
    if status in (401, 403):
        # 公开热榜返回 401/403 通常是缺访客 cookie 或被拦，属源侧问题。
        # 对外不区分具体原因，避免泄露内部账号细节。
        return AuthExpired("上游拒绝访问")
    if status >= 500:
        return UpstreamDown(f"上游 {status}")
    return UpstreamDown(f"上游返回意外状态 {status}")


def check_business_status(config: SourceConfig, payload: Any) -> None:
    """检查响应体里的业务状态码。

    HTTP 200 不等于成功——很多站点用体内的码表示拒绝。不看这个的话，
    「被风控挡了一下」会一路走到提取层，表现为「取不到列表」，然后被报成
    「多半是对方改版了」。那句话会把人带到完全错误的排查方向上。
    """
    spec = config.status
    if spec is None or not isinstance(payload, dict):
        return

    code = resolve_path(payload, spec.path)
    if code is None or code in spec.ok:
        return
    try:
        code = int(code)
    except (TypeError, ValueError):
        raise UpstreamDown(f"{config.name} 的业务状态码不是数字：{code!r}") from None

    message = resolve_path(payload, spec.message_path) if spec.message_path else None
    detail = f"{config.name} 业务码 {code}" + (f"：{message}" if message else "")

    if code in spec.rate_limited:
        raise RateLimited(detail)
    if code in spec.auth_expired:
        raise AuthExpired(detail)
    if code in spec.captcha:
        raise Captcha(detail)
    raise UpstreamDown(detail)


def fetch_raw(config: SourceConfig, client: httpx.Client | None = None) -> Any:
    """执行（可选的 pre_request +）正式请求，返回解析后的 JSON。"""
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        if config.pre_request is not None:
            # 领访客 cookie；失败不致命，正式请求可能照样能过。
            try:
                client.request(
                    config.pre_request.method,
                    config.pre_request.url,
                    headers=config.request.merged_headers(),
                    timeout=config.pre_request.timeout,
                )
            except httpx.HTTPError:
                pass

        if config.request.impersonate:
            response = _fetch_impersonated(config)
        else:
            try:
                response = client.request(
                    config.request.method,
                    config.request.url,
                    headers=config.request.merged_headers(),
                    json=config.request.json_body,
                    timeout=config.request.timeout,
                )
            except httpx.TimeoutException as exc:
                raise Timeout(f"{config.name} 请求超时") from exc
            except httpx.HTTPError as exc:
                raise UpstreamDown(f"{config.name} 连接失败：{type(exc).__name__}") from exc

        if response.status_code != 200:
            raise _classify_http_error(response)

        if config.extract.format == "json":
            try:
                payload = response.json()
            except ValueError as exc:
                raise UpstreamDown(f"{config.name} 返回的不是合法 JSON") from exc
            # HTTP 200 不代表业务上成功，先过一遍体内状态码。
            check_business_status(config, payload)
            return payload
        return response.text
    finally:
        if owns_client:
            client.close()


def _rss_entries(text: str, config: SourceConfig) -> list[dict[str, Any]]:
    """把 RSS/Atom 条目压成普通 dict，之后就能走 JSON 那套取值逻辑。"""
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise UpstreamDown(f"{config.name} 的 RSS 解析失败：{feed.bozo_exception}")

    rows: list[dict[str, Any]] = []
    for entry in feed.entries:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            parsed = entry.get(key)
            if parsed:
                published = datetime(*parsed[:6], tzinfo=UTC).isoformat()
                break
        summary = entry.get("summary")
        if summary:
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
        rows.append(
            {
                "id": entry.get("id") or entry.get("link"),
                "title": entry.get("title"),
                "link": entry.get("link"),
                "summary": (summary or None),
                "author": entry.get("author"),
                "published": published,
            }
        )
    return rows


#: RSS 条目形状稳定，配置不写 fields 就用这套默认映射。
RSS_DEFAULT_FIELDS: dict[str, FieldSpec] = {
    "native_id": FieldSpec(path="id"),
    "title": FieldSpec(path="title"),
    "url": FieldSpec(path="link"),
    "summary": FieldSpec(path="summary"),
    "author": FieldSpec(path="author"),
    "published_at": FieldSpec(path="published", type="iso"),
}


def _rows_and_fields(
    config: SourceConfig, payload: Any
) -> tuple[list[Any], dict[str, FieldSpec]]:
    """按格式取出「行」的列表与本次要用的字段定义。"""
    spec = config.extract

    if spec.format == "rss":
        return _rss_entries(payload, config), (spec.fields or RSS_DEFAULT_FIELDS)

    if spec.format == "html":
        soup = BeautifulSoup(payload, "lxml")
        rows = soup.select(spec.rows)
        if not rows:
            raise UpstreamDown(
                f"{config.name} 的选择器 {spec.rows!r} 一个元素都没选中"
                f"——多半是对方改版了"
            )
        return rows, spec.fields

    rows = resolve_path(payload, spec.rows)
    if not isinstance(rows, list):
        raise UpstreamDown(
            f"{config.name} 的 extract.list={spec.rows!r} 没取到列表"
            f"（拿到 {type(rows).__name__}）——多半是对方改版了"
        )
    return rows, spec.fields


def _is_excluded(config: SourceConfig, fields: dict[str, Any]) -> bool:
    """按配置的关键词丢弃条目（IT之家那种混在列表里的广告）。"""
    for key, words in config.extract.exclude_if.items():
        value = fields.get(key)
        if isinstance(value, str) and any(w in value for w in words):
            return True
    return False


def normalize(config: SourceConfig, payload: Any, *, now: datetime | None = None) -> list[Item]:
    """把原始响应转成统一 Item。单条坏了就跳过，不拖垮整源。"""
    now = now or datetime.now(UTC)
    rows, field_specs = _rows_and_fields(config, payload)
    is_html = config.extract.format == "html"

    source = Source(type=config.type, name=config.display_name, platform=config.platform)
    categorizer = get_categorizer()
    total = len(rows)
    items: list[Item] = []

    for rank, row in enumerate(rows):
        try:
            fields = extract_row(
                row, field_specs, is_html=is_html, base_url=config.base_url
            )
            native_id, title, url = fields.get("native_id"), fields.get("title"), fields.get("url")
            if not native_id or not title or not url:
                continue
            if _is_excluded(config, fields):
                continue

            published_at = fields.get("published_at")
            summary = fields.get("summary")
            media = (
                [Media(type=MediaType.IMAGE, url=fields["image"])]
                if fields.get("image")
                else []
            )
            categories = categorizer.classify(
                title=str(title),
                summary=summary,
                source_keys=(config.name, config.platform, config.type.value),
            )
            categories = list(dict.fromkeys([*config.categories, *categories]))

            items.append(
                Item(
                    id=f"{config.type.value}:{config.platform or config.name}_{native_id}",
                    source=source,
                    title=str(title)[:500],
                    summary=summary,
                    url=normalize_url(str(url)),
                    author=fields.get("author"),
                    published_at=published_at,
                    discovered_at=now,
                    time_basis=(
                        TimeBasis.PUBLISHED if published_at else TimeBasis.DISCOVERED
                    ),
                    score=rank_to_score(rank, total) if config.ranked else 0.0,
                    categories=categories,
                    lang=config.lang,
                    media=media,
                    raw={"rank": rank + 1, "score_raw": fields.get("score_raw")},
                )
            )
        except Exception:
            # 单条脏数据不该让整个榜挂掉；源级失败才走错误码。
            continue

    if not items and total:
        raise UpstreamDown(f"{config.name} 取到 {total} 行但没有一条能归一化——字段配置该改了")
    return items


def rank_to_score(rank: int, total: int) -> float:
    """榜内排名 → [0,1] 源内相对热度。第 1 名 1.0，末位 > 0。

    只对真正是排行榜的源有意义（config.ranked）。按时间倒序的 RSS/快讯
    没有名次，那种源固定 0.0，原始热度值仍在 raw 里。
    契约明确：该值不保证跨源可比。
    """
    if total <= 0:
        return 0.0
    return round((total - rank) / total, 4)


def verify_urls(
    config: SourceConfig, items: list[Item], client: httpx.Client | None = None
) -> list[Item]:
    """逐条确认 URL 真的存在，404 的丢掉。

    只给 URL 是推导出来的源用。推导规则（比如「文章地址 = 标题 slug 化」）是对
    站点的假设，站点一改就会悄悄产出一堆死链——开着这个，假设失效会表现为
    条目数下降，在 /health 里看得见。
    """
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True)
    alive: list[Item] = []
    try:
        for item in items:
            try:
                response = client.head(
                    str(item.url),
                    headers=config.request.merged_headers(),
                    timeout=config.request.timeout,
                )
                if response.status_code == 405:  # 有些站点不认 HEAD
                    response = client.get(
                        str(item.url),
                        headers=config.request.merged_headers(),
                        timeout=config.request.timeout,
                    )
                if response.status_code < 400:
                    alive.append(item)
                else:
                    log.warning(
                        "%s 推导出的 URL 无效（%s）：%s",
                        config.name,
                        response.status_code,
                        item.url,
                    )
            except httpx.HTTPError:
                # 校验本身失败不等于条目无效，放行，别因为网络抖动丢数据。
                alive.append(item)
    finally:
        if owns_client:
            client.close()
    return alive


#: 重逻辑 channel 注册表。声明式引擎搞不定的源在这里登记自己的采集函数。
CHANNELS: dict[str, Any] = {}


def register_channel(name: str, collector) -> None:
    CHANNELS[name] = collector


def collect(config: SourceConfig, client: httpx.Client | None = None) -> list[Item]:
    if config.channel is not None:
        handler = CHANNELS.get(config.channel)
        if handler is None:
            raise UpstreamDown(f"未注册的 channel：{config.channel}")
        return handler(config)

    items = normalize(config, fetch_raw(config, client))
    if config.verify_urls:
        items = verify_urls(config, items, client)
    return items
