"""统一条目 schema。跨源一致，见 docs/contract.md §2。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class SourceType(StrEnum):
    X = "x"
    HOTLIST = "hotlist"
    WECHAT = "wechat"
    RSS = "rss"
    WEB = "web"
    #: 厂商官方发布（OpenAI / Anthropic / DeepSeek 等的官网新闻与发布说明）。
    #: 按「谁发的」而不是「怎么抓的」分类——同一家可能今天有 RSS、明天只剩 HTML，
    #: 下游不该因为传输方式变了就得改查询。
    VENDOR = "vendor"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"
    AUDIO = "audio"


class TimeBasis(StrEnum):
    """`published` = published_at 可信；`discovered` = 只有收录时间。

    下游展示时间必须据此标注，不得把收录时间伪称为原文发布时间。
    """

    PUBLISHED = "published"
    DISCOVERED = "discovered"


class Category(StrEnum):
    """确定性规则打标，规则表在 config/categories.yaml。采集侧不做 LLM 分析。"""

    MODEL = "model"
    PRODUCT = "product"
    PAPER = "paper"
    INDUSTRY = "industry"
    TIP = "tip"


ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:.+$")

Utc = Annotated[datetime, Field(description="ISO8601 UTC")]


def to_utc(value: datetime) -> datetime:
    """归一到 UTC。naive datetime 一律拒绝——时区靠猜是 bug 的温床。"""
    if value.tzinfo is None:
        raise ValueError("时间必须带时区信息（naive datetime 不接受）")
    return value.astimezone(UTC)


class Source(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: SourceType
    name: str = Field(min_length=1, description="人类可读源名，如 'X / Twitter'")
    platform: str | None = Field(
        default=None, description="子平台标识，热榜专用：weibo / zhihu / douyin …"
    )


class Media(BaseModel):
    type: MediaType
    url: AnyHttpUrl
    width: int | None = None
    height: int | None = None

    @field_serializer("url")
    def _ser_url(self, url: AnyHttpUrl) -> str:
        return str(url)


class Item(BaseModel):
    """跨源统一条目。

    采集侧只负责「看见·抓取·归一化」——不做面向用户的排序，
    `score` 仅为源内热度信号。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="{source_type}:{native_id}，全局唯一")
    source: Source
    title: str = Field(min_length=1)
    summary: str | None = Field(
        default=None, description="客观摘要，抽取式，不做生成式改写、不带观点"
    )
    url: AnyHttpUrl = Field(description="第三方原文链接")
    author: str | None = None
    published_at: Utc | None = Field(
        default=None, description="原文发布时间。取不到就是 null，绝不回填"
    )
    discovered_at: Utc = Field(description="本平台收录时间")
    time_basis: TimeBasis
    score: float = Field(
        ge=0.0, le=1.0, description="源内相对热度，不保证跨源可比"
    )
    categories: list[Category] = Field(default_factory=list)
    lang: str | None = Field(default=None, description="ISO639-1，如 zh / en")
    media: list[Media] = Field(default_factory=list)
    raw: dict[str, Any] = Field(
        default_factory=dict, description="原始响应片段。结构不稳定，消费方不得依赖"
    )

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not ID_PATTERN.match(v):
            raise ValueError(f"id 必须形如 'source_type:native_id'，收到 {v!r}")
        return v

    @field_validator("published_at", "discovered_at")
    @classmethod
    def _normalize_time(cls, v: datetime | None) -> datetime | None:
        return None if v is None else to_utc(v)

    @field_validator("author")
    @classmethod
    def _strip_at(cls, v: str | None) -> str | None:
        return v.lstrip("@") if v else v

    @model_validator(mode="after")
    def _check_time_basis(self) -> Self:
        if self.time_basis is TimeBasis.PUBLISHED and self.published_at is None:
            raise ValueError("time_basis=published 但 published_at 为空")
        if self.id.split(":", 1)[0] != self.source.type.value:
            raise ValueError(
                f"id 前缀 {self.id.split(':', 1)[0]!r} 与 source.type "
                f"{self.source.type.value!r} 不一致"
            )
        return self

    @field_serializer("published_at", "discovered_at")
    def _ser_time(self, v: datetime | None) -> str | None:
        return None if v is None else v.strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_serializer("url")
    def _ser_url(self, url: AnyHttpUrl) -> str:
        return str(url)

    @property
    def effective_time(self) -> datetime:
        """可排序时间。展示层仍须按 time_basis 区分说法。"""
        return self.published_at or self.discovered_at

    @classmethod
    def build(
        cls,
        *,
        source_type: SourceType,
        native_id: str,
        published_at: datetime | None,
        discovered_at: datetime | None = None,
        **fields: Any,
    ) -> Item:
        """便利构造：自动拼 id、推导 time_basis、补 discovered_at。"""
        return cls(
            id=f"{source_type.value}:{native_id}",
            published_at=published_at,
            discovered_at=discovered_at or datetime.now(UTC),
            time_basis=(
                TimeBasis.PUBLISHED if published_at else TimeBasis.DISCOVERED
            ),
            **fields,
        )


class Article(BaseModel):
    """`read_article` 的出参。正文形态套不进 Item，故独立建模。"""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    title: str
    author: str | None = None
    published_at: Utc | None = None
    content_markdown: str
    char_count: int = Field(ge=0)
    truncated: bool = False
    lang: str | None = None
    fetched_at: Utc

    @field_validator("published_at", "fetched_at")
    @classmethod
    def _normalize_time(cls, v: datetime | None) -> datetime | None:
        return None if v is None else to_utc(v)

    @field_serializer("published_at", "fetched_at")
    def _ser_time(self, v: datetime | None) -> str | None:
        return None if v is None else v.strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_serializer("url")
    def _ser_url(self, url: AnyHttpUrl) -> str:
        return str(url)
