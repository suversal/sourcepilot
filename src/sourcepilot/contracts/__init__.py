"""工具契约：采集平台与一切消费方之间的唯一合同。

三个出口（REST / MCP / SKILL.md）共用本包的定义，只是协议壳不同。
文字版见 docs/contract.md；两者冲突时以本包为准。
"""

from .envelope import Envelope, ItemsPayload, Meta, Mode, SourceHealth
from .errors import (
    DEGRADABLE,
    HTTP_STATUS,
    AuthExpired,
    BadRequest,
    Captcha,
    ErrorBody,
    ErrorCode,
    NotFound,
    RateLimited,
    SourcePilotError,
    Timeout,
    UpstreamDown,
)
from .item import (
    Article,
    Category,
    Item,
    Media,
    MediaType,
    Source,
    SourceType,
    TimeBasis,
    to_utc,
)
from .tools import (
    TOOL_REGISTRY,
    WINDOW_SECONDS,
    GetFeedParams,
    GetHotlistParams,
    GetWechatFeedParams,
    GetXTimelineParams,
    ReadArticleParams,
    SearchXParams,
    ToolSpec,
    Window,
    split_platforms,
)
from .version import API_PREFIX, CONTRACT_VERSION

__all__ = [
    "API_PREFIX",
    "CONTRACT_VERSION",
    "DEGRADABLE",
    "HTTP_STATUS",
    "TOOL_REGISTRY",
    "WINDOW_SECONDS",
    "Article",
    "AuthExpired",
    "BadRequest",
    "Captcha",
    "Category",
    "Envelope",
    "ErrorBody",
    "ErrorCode",
    "GetFeedParams",
    "GetHotlistParams",
    "GetWechatFeedParams",
    "GetXTimelineParams",
    "Item",
    "ItemsPayload",
    "Media",
    "MediaType",
    "Meta",
    "Mode",
    "NotFound",
    "RateLimited",
    "ReadArticleParams",
    "SearchXParams",
    "ToolSpec",
    "Source",
    "SourceHealth",
    "SourcePilotError",
    "SourceType",
    "TimeBasis",
    "Timeout",
    "UpstreamDown",
    "Window",
    "split_platforms",
    "to_utc",
]
