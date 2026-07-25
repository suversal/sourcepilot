"""声明式抓取引擎：配置 → HTTP → 归一化 Item。

所有源共用这一条路径，源之间的差异全在 YAML 里。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

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
from .config import SourceConfig
from .extract import extract_field, resolve_path

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


def _classify_http_error(response: httpx.Response) -> SourcePilotError:
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

        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamDown(f"{config.name} 返回的不是合法 JSON") from exc
    finally:
        if owns_client:
            client.close()


def normalize(config: SourceConfig, payload: Any, *, now: datetime | None = None) -> list[Item]:
    """把原始 JSON 转成统一 Item。单条坏了就跳过，不拖垮整源。"""
    now = now or datetime.now(UTC)
    rows = resolve_path(payload, config.extract.list)
    if not isinstance(rows, list):
        raise UpstreamDown(
            f"{config.name} 的 extract.list={config.extract.list!r} 没取到列表"
            f"（拿到 {type(rows).__name__}）——多半是对方改版了"
        )

    source = Source(type=config.type, name=config.display_name, platform=config.platform)
    categorizer = get_categorizer()
    total = len(rows)
    items: list[Item] = []

    for rank, row in enumerate(rows):
        try:
            fields = {
                key: extract_field(row, spec) for key, spec in config.extract.fields.items()
            }
            native_id, title, url = fields.get("native_id"), fields.get("title"), fields.get("url")
            if not native_id or not title or not url:
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
                    score=rank_to_score(rank, total),
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

    契约明确：该值不保证跨源可比。
    """
    if total <= 0:
        return 0.0
    return round((total - rank) / total, 4)


def collect(config: SourceConfig, client: httpx.Client | None = None) -> list[Item]:
    return normalize(config, fetch_raw(config, client))
