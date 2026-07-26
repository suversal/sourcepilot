"""六个工具的入参定义。三出口共用：REST 解析 query string，MCP 生成 tool schema。

见 docs/contract.md §4。
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的协议无关定义。三出口都从这里派生，别各写各的。

    `description` 是给 **MCP 客户端选工具**用的——REST 那边人看 docstring 和
    OpenAPI，MCP 那边是模型读这段文字来决定调不调。所以它得说清楚
    「什么时候用它」和「它给不了什么」，而不只是复述参数。
    """

    params: type[_Params]
    rest_path: str
    description: str


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "search_x": ToolSpec(
        SearchXParams,
        "/x/search",
        "现场搜索 X（Twitter）上的任意关键词。**这是唯一的实时查询工具**，"
        "其余都是读预采集的缓存。用于「X 上现在怎么评价某个东西」这类问题。"
        "较慢（数秒），且现查失败时会降级到缓存并把 meta.stale 置 true——"
        "那种情况必须向用户说明结果非实时。",
    ),
    "get_x_timeline": ToolSpec(
        GetXTimelineParams,
        "/x/timeline",
        "取某个 X 账号最近发的推文。适合「某某最近发了什么」。"
        "走公开镜像，拿不到互动数，score 恒为 0——别据此判断哪条更热门。",
    ),
    "get_hotlist": ToolSpec(
        GetHotlistParams,
        "/hotlist",
        "多平台科技热榜（B站、头条、掘金、Hacker News、GitHub Trending 等）。"
        "回答「现在大家在讨论什么」。这是二手讨论，不是官方发布；"
        "想要厂商一手信息用 get_feed 配 source=vendor。",
    ),
    "get_wechat_feed": ToolSpec(
        GetWechatFeedParams,
        "/wechat/feed",
        "已订阅公众号的最新文章。**只覆盖已订阅的号**，不是全网搜索——"
        "用户问一个没订阅的号，如实说没订阅，别拿别家文章顶替。",
    ),
    "read_article": ToolSpec(
        ReadArticleParams,
        "/article",
        "抓取指定 URL 的正文并转成 Markdown。用于「展开讲讲这条」——"
        "先从别的工具拿到条目的 url，再传进来。只接受公网 http(s) 地址。",
    ),
    "get_feed": ToolSpec(
        GetFeedParams,
        "/items",
        "归一化的资讯流，覆盖全部信源。支持关键词检索（q，中文按字符匹配）、"
        "按信源过滤（platform）、按类型过滤（source=vendor 取 OpenAI/Anthropic "
        "等厂商官方发布）、时间窗（window）。这是最通用的入口，"
        "不确定用哪个工具时先用它。",
    ),
}
