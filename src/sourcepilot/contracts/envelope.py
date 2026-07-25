"""统一响应信封。REST 与 MCP 完全一致，见 docs/contract.md §1。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .errors import ErrorBody, ErrorCode, SourcePilotError
from .item import Item, Utc, to_utc
from .version import CONTRACT_VERSION

DataT = TypeVar("DataT")


class Mode(StrEnum):
    """实际走了什么取数路径（与入参 `live` 是「请求」与「结果」的关系）。"""

    LIVE = "live"
    CACHE = "cache"
    MIXED = "mixed"


class SourceHealth(BaseModel):
    """分源结果。一个源崩了不许拖垮全局——失败详情落在这里，而不是整体报错。"""

    name: str
    ok: bool
    from_cache: bool = False
    item_count: int = 0
    error_code: ErrorCode | None = None


class Meta(BaseModel):
    contract_version: str = CONTRACT_VERSION
    mode: Mode | None = None
    stale: bool = Field(
        default=False, description="true = 降级得到的近似结果，非实时"
    )
    collected_at: Utc | None = Field(
        default=None, description="数据快照时间；现查时约等于请求时间"
    )
    next_cursor: str | None = Field(
        default=None, description="opaque，消费方不得解析其内容"
    )
    has_more: bool = False
    elapsed_ms: int | None = None
    sources: list[SourceHealth] = Field(default_factory=list)

    @field_validator("collected_at")
    @classmethod
    def _normalize_time(cls, v: datetime | None) -> datetime | None:
        return None if v is None else to_utc(v)

    @field_serializer("collected_at")
    def _ser_time(self, v: datetime | None) -> str | None:
        return None if v is None else v.strftime("%Y-%m-%dT%H:%M:%SZ")


class Envelope(BaseModel, Generic[DataT]):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: DataT | None = None
    meta: Meta = Field(default_factory=Meta)
    error: ErrorBody | None = None

    @classmethod
    def success(cls, data: DataT, meta: Meta | None = None) -> Envelope[DataT]:
        return cls(ok=True, data=data, meta=meta or Meta(), error=None)

    @classmethod
    def degraded(
        cls,
        data: DataT,
        *,
        meta: Meta | None = None,
        collected_at: datetime | None = None,
    ) -> Envelope[DataT]:
        """现查失败但缓存兜住了。**这不是错误**——ok 仍为 True，只标 stale。"""
        m = (meta or Meta()).model_copy(
            update={
                "mode": Mode.CACHE,
                "stale": True,
                "collected_at": collected_at
                or (meta.collected_at if meta else None)
                or datetime.now(UTC),
            }
        )
        return cls(ok=True, data=data, meta=m, error=None)

    @classmethod
    def failure(
        cls,
        code: ErrorCode,
        message: str,
        meta: Meta | None = None,
    ) -> Envelope[DataT]:
        return cls(
            ok=False,
            data=None,
            meta=meta or Meta(),
            error=ErrorBody(code=code, message=message),
        )

    @classmethod
    def from_exception(
        cls, exc: SourcePilotError, meta: Meta | None = None
    ) -> Envelope[DataT]:
        return cls(ok=False, data=None, meta=meta or Meta(), error=exc.to_body())


class ItemsPayload(BaseModel):
    """列表类工具的统一 data 形状。分页信息在 meta，不在这里。"""

    model_config = ConfigDict(extra="forbid")

    items: list[Item] = Field(default_factory=list)
