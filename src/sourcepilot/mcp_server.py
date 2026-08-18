"""MCP 出口。与 `api.py` 平级——**同一套服务，换个协议壳**。

CLAUDE.md 的铁律：「补 MCP 出口时改 api.py 的同级新文件，不许把逻辑抄一份
——服务层才是那『一套核心』」。所以这个文件里没有任何业务判断：降级、缓存、
分源健康、限流都在服务层，这里只负责

    MCP 的 tool schema  ←→  契约的 params 模型
    MCP 的 text 返回     ←→  契约的 Envelope

两件事。工具清单直接由 `contracts.TOOL_REGISTRY` 驱动，新增工具时不用动这里。

**与 REST 的唯一实质差别**：MCP 没有 HTTP 状态码。契约 §1 早就写死了这一点
——「MCP 侧只看信封的 ok 与 error.code，语义完全一致」——所以这边反而更简单，
不用做状态码映射，失败照样是一个 ok=false 的信封。

跑起来：
    python -m sourcepilot.mcp_server        # stdio，给 Claude Desktop 这类客户端
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .article import ArticleService
from .collector import Collector
from .contracts import (
    TOOL_REGISTRY,
    BadRequest,
    Envelope,
    SourcePilotError,
)
from .services import FeedService, HotlistService, WechatFeedService
from .sources import SourceConfig, load_sources
from .store import Store
from .x_service import XSearchService, XTimelineService

log = logging.getLogger("sourcepilot.mcp")


def build_handlers(
    store: Store | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> dict[str, Callable[[Any], Envelope]]:
    """工具名 → 服务方法。这是本文件唯一的"接线"，其余都是协议转换。

    签名与 `api.create_app` 保持一致（都可注入 store 与 sources）——两个出口
    构造方式不一样的话，就没法拿同一组输入去比对两边的行为是否一致。
    """
    store = store or Store()
    sources = sources if sources is not None else load_sources()
    collector = Collector(store, sources)
    return {
        "search_x": XSearchService(store).search,
        "get_x_timeline": XTimelineService(store).get,
        "get_hotlist": HotlistService(collector).get,
        "get_wechat_feed": WechatFeedService(store).get,
        "read_article": ArticleService().get,
        "get_feed": FeedService(store, sources).get,
    }


def tool_schemas() -> list[dict[str, Any]]:
    """把契约里的 params 模型转成 MCP 的 inputSchema。

    pydantic 自带 JSON Schema 生成，所以这里是纯机械转换——**参数定义只有一份**，
    改契约就同时改了 REST 校验和 MCP schema，不可能对不上。
    """
    schemas = []
    for name, spec in TOOL_REGISTRY.items():
        schema = spec.params.model_json_schema()
        # MCP 客户端不认 $defs 引用之外的东西，pydantic 的默认输出已经够用；
        # 只需把标题换成工具名，免得客户端显示成 "SearchXParams"。
        schema["title"] = name
        schemas.append(
            {"name": name, "description": spec.description, "inputSchema": schema}
        )
    return schemas


def call_tool(
    name: str, arguments: dict[str, Any], handlers: dict[str, Callable[[Any], Envelope]]
) -> str:
    """执行一个工具，返回信封的 JSON 文本。

    异常一律翻成 `ok=false` 的信封，而不是让它冒到协议层——MCP 客户端拿到
    协议级错误只会说"工具调用失败"，拿到信封里的 error.code 才能分支决策
    （限流就退避、验证码就如实说、参数错就改正重试）。
    """
    spec = TOOL_REGISTRY.get(name)
    handler = handlers.get(name)
    if spec is None or handler is None:
        env = Envelope.failure(BadRequest(f"没有名为 {name} 的工具").code, f"未知工具 {name}")
        return env.model_dump_json()

    try:
        params = spec.params(**arguments)
    except Exception as exc:
        # 参数校验失败：把 pydantic 的报错原样带出去，客户端才知道该改哪个字段。
        return Envelope.failure(BadRequest("").code, f"参数错误：{exc}").model_dump_json()

    try:
        envelope = handler(params)
    except SourcePilotError as exc:
        envelope = Envelope.from_exception(exc)
    except Exception as exc:  # 兜底：绝不让未知异常穿透协议层
        log.exception("工具 %s 执行出错", name)
        envelope = Envelope.failure(
            BadRequest("").code, f"内部错误：{type(exc).__name__}"
        )
    return envelope.model_dump_json()


def create_server(
    store: Store | None = None, sources: dict[str, SourceConfig] | None = None
):
    """构造 MCP Server。延迟导入 SDK，让不装 mcp 的环境也能 import 本模块。

    **兼容 mcp 1.x 与 2.x 两套低层 API**。2.0 把装饰器（`@server.list_tools()`）
    换成了构造参数回调（`on_list_tools=`），两套不共存——1.x 的写法在 2.0 上直接
    `AttributeError`。这不是能靠钉版本躲过去的事：`pip install "sourcepilot[mcp]"`
    今天装到的就是 2.x，而钉住上界等于让新装的人拿不到 SDK 的安全修复。

    分流按**能力探测**而不是读版本号：探的正是我们真正依赖的那个方法在不在，
    所以 SDK 哪天把装饰器加回来、或者再改一次版本号规则，这里都不用跟着动。

    两条路产出的服务器行为一致，工具定义仍然只有一份（`tool_schemas()`）——
    协议壳换了，那「一套核心」没动。
    """
    import mcp.types as types
    from mcp.server import Server

    handlers = build_handlers(store, sources)

    if hasattr(Server, "list_tools"):  # mcp 1.x：装饰器注册
        server = Server("sourcepilot")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [types.Tool(**s) for s in tool_schemas()]

        @server.call_tool()
        async def handle(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            return [
                types.TextContent(type="text", text=call_tool(name, arguments or {}, handlers))
            ]

        return server

    # mcp >= 2.0：构造参数回调，返回值也从「内容列表」变成了完整的 Result 对象。
    async def on_list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(tools=[types.Tool(**s) for s in tool_schemas()])

    async def on_call_tool(_ctx: Any, params: Any) -> Any:
        text = call_tool(params.name, params.arguments or {}, handlers)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return Server("sourcepilot", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _run() -> None:
    from mcp.server.stdio import stdio_server

    server = create_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = ["build_handlers", "call_tool", "create_server", "main", "tool_schemas"]
