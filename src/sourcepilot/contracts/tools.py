"""六个工具的入参定义。三出口共用：REST 解析 query string，MCP 生成 tool schema。

见 docs/contract.md §4。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from .item import Category, SourceType, Utc, to_utc


class Window(StrEnum):
    """时间范围过滤。与取数模式无关——那是 `live` 的事。"""

    H1 = "1h"
    H6 = "6h"
    H24 = "24h"
    D7 = "7d"
    D30 = "30d"
    #: 不限时间。检索场景需要它——问「关于 Sora 的消息」时把人锁在 30 天内，
    #: 会让库里几个月前的相关条目一条都看不到。
    ALL = "all"


WINDOW_SECONDS: dict[Window, int | None] = {
    Window.H1: 3600,
    Window.H6: 6 * 3600,
    Window.H24: 24 * 3600,
    Window.D7: 7 * 86400,
    Window.D30: 30 * 86400,
    Window.ALL: None,  # None = 不加时间下限
}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paginated(_Params):
    cursor: str | None = Field(
        default=None, description="来自上次响应的 meta.next_cursor，opaque"
    )


class SearchXParams(Paginated):
    q: str = Field(min_length=1, description="搜索词")
    limit: int = Field(default=20, ge=1, le=100)
    window: Window = Field(
        default=Window.D7, description="X 搜索超 7 天不保证可得"
    )
    live: bool = Field(default=True, description="false = 强制只读缓存")


class GetXTimelineParams(Paginated):
    handle: str = Field(min_length=1, description="用户 handle，不带 @")
    limit: int = Field(default=20, ge=1, le=100)
    window: Window = Window.D7
    live: bool = True

    @field_validator("handle")
    @classmethod
    def _strip_at(cls, v: str) -> str:
        return v.lstrip("@")


class GetHotlistParams(_Params):
    platform: str | None = Field(
        default=None, description="weibo|zhihu|douyin|bilibili…  不填 = 全部"
    )
    limit: int = Field(default=20, ge=1, le=50, description="每平台条数")


class GetWechatFeedParams(Paginated):
    account: str | None = Field(default=None, description="不填 = 全部已订阅")
    window: Window = Window.D7
    limit: int = Field(default=20, ge=1, le=100)


class ReadArticleParams(_Params):
    url: AnyHttpUrl
    max_chars: int = Field(default=50_000, ge=1000, le=500_000)


class GetFeedParams(Paginated):
    """喂 AIRADAR。`since` 是过滤条件，`cursor` 是分页位置，两者正交。"""

    q: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="关键词，在标题与摘要里做子串匹配（对中文按字符匹配，无需分词）",
    )
    platform: str | None = Field(
        default=None,
        description="按具体信源过滤，如 openai / anthropic / bilibili；取值见 /health",
    )
    window: Window = Window.H24
    category: Category | None = Field(
        default=None, description="匹配 Item.categories 中任一项"
    )
    source: SourceType | None = None
    since: Utc | None = Field(
        default=None, description="只返回 discovered_at > since 的条目"
    )
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("since")
    @classmethod
    def _normalize_time(cls, v: datetime | None) -> datetime | None:
        return None if v is None else to_utc(v)


#: 工具名 → (入参模型, REST 端点)。三出口从这里派生，别各写各的。
TOOL_REGISTRY: dict[str, tuple[type[_Params], str]] = {
    "search_x": (SearchXParams, "/x/search"),
    "get_x_timeline": (GetXTimelineParams, "/x/timeline"),
    "get_hotlist": (GetHotlistParams, "/hotlist"),
    "get_wechat_feed": (GetWechatFeedParams, "/wechat/feed"),
    "read_article": (ReadArticleParams, "/article"),
    "get_feed": (GetFeedParams, "/items"),
}
