"""MCP 出口测试。

这一层的价值不在功能——功能都在服务层，早就测过了。它的价值在于
**REST 与 MCP 的行为必须一致**：同一个问题走两个出口应该得到同一个信封。
不一致是最难查的一类 bug，因为两边单独测都是绿的。
"""

from __future__ import annotations

import json

import pytest
from conftest import FAKE_CONFIG_DICT, FAKE_PAYLOAD
from fastapi.testclient import TestClient

from sourcepilot.api import create_app
from sourcepilot.contracts import CONTRACT_VERSION, TOOL_REGISTRY
from sourcepilot.mcp_server import build_handlers, call_tool, tool_schemas
from sourcepilot.sources import SourceConfig, engine


@pytest.fixture
def sources():
    return {"fake": SourceConfig(**FAKE_CONFIG_DICT)}


@pytest.fixture(autouse=True)
def stub_fetch(monkeypatch):
    monkeypatch.setattr(engine, "fetch_raw", lambda config, client=None, *a, **kw: FAKE_PAYLOAD)


class TestToolSchemas:
    def test_exposes_all_contract_tools(self):
        """工具清单由契约驱动——新增工具时不该需要改 MCP 出口。"""
        assert {s["name"] for s in tool_schemas()} == set(TOOL_REGISTRY)

    def test_every_tool_has_a_description(self):
        """MCP 客户端靠这段文字决定调不调，空描述等于这个工具不存在。"""
        for s in tool_schemas():
            assert len(s["description"]) > 20, s["name"]

    def test_schema_comes_from_the_contract_model(self):
        """参数定义只有一份：改契约就同时改了 REST 校验和 MCP schema。"""
        schema = next(s for s in tool_schemas() if s["name"] == "search_x")["inputSchema"]
        assert set(schema["properties"]) == set(TOOL_REGISTRY["search_x"].params.model_fields)
        assert "q" in schema["required"]

    def test_title_is_the_tool_name_not_the_class_name(self):
        schema = next(s for s in tool_schemas() if s["name"] == "get_feed")["inputSchema"]
        assert schema["title"] == "get_feed"


class TestCallTool:
    def test_returns_a_contract_envelope(self, store, sources):
        out = json.loads(call_tool("get_hotlist", {"limit": 2}, build_handlers(store, sources)))
        assert out["ok"] is True
        assert out["meta"]["contract_version"] == CONTRACT_VERSION
        assert "items" in out["data"]

    def test_unknown_tool_is_a_bad_request_envelope(self, store, sources):
        """协议级报错客户端只会说"调用失败"；信封里的 code 才能让它分支决策。"""
        out = json.loads(call_tool("nope", {}, build_handlers(store, sources)))
        assert out["ok"] is False
        assert out["error"]["code"] == "BAD_REQUEST"

    def test_invalid_params_report_which_field(self, store, sources):
        out = json.loads(call_tool("get_feed", {"limit": 9999}, build_handlers(store, sources)))
        assert out["ok"] is False
        assert "limit" in out["error"]["message"]

    def test_unexpected_exception_does_not_escape(self, store, sources, monkeypatch):
        """未知异常绝不能穿透协议层——那会让客户端拿到一个没有 error.code 的失败。"""
        handlers = build_handlers(store, sources)
        handlers["get_feed"] = lambda p: (_ for _ in ()).throw(RuntimeError("炸了"))
        out = json.loads(call_tool("get_feed", {}, handlers))
        assert out["ok"] is False
        assert out["error"]["code"] == "BAD_REQUEST"


class TestRestAndMcpAgree:
    """同一个问题走两个出口，信封必须一致。这是"三出口一套核心"的实际含义。"""

    def _both(self, store, sources, tool: str, args: dict, rest_path: str):
        mcp = json.loads(call_tool(tool, args, build_handlers(store, sources)))
        client = TestClient(create_app(store=store, sources=sources, scheduler=False))
        rest = client.get(f"/api/v1{rest_path}", params=args).json()
        return mcp, rest

    def test_same_items_and_meta_shape(self, store, sources):
        mcp, rest = self._both(store, sources, "get_hotlist", {"limit": 2}, "/hotlist")
        assert mcp["ok"] == rest["ok"]
        assert [i["id"] for i in mcp["data"]["items"]] == [
            i["id"] for i in rest["data"]["items"]
        ]
        assert mcp["meta"]["mode"] == rest["meta"]["mode"]
        assert mcp["meta"]["stale"] == rest["meta"]["stale"]

    def test_same_error_code_on_bad_params(self, store, sources):
        """REST 会额外映射一个 HTTP 状态码，但信封里的 error.code 必须相同。"""
        mcp, rest = self._both(
            store, sources, "get_hotlist", {"platform": "不存在"}, "/hotlist"
        )
        assert mcp["ok"] is False and rest["ok"] is False
        assert mcp["error"]["code"] == rest["error"]["code"] == "BAD_REQUEST"

    def test_contract_version_matches(self, store, sources):
        mcp, rest = self._both(store, sources, "get_feed", {"limit": 1}, "/items")
        assert mcp["meta"]["contract_version"] == rest["meta"]["contract_version"]


mcp_sdk = pytest.importorskip("mcp", reason="没装 mcp SDK（它是可选依赖）")


class TestCreateServer:
    """构造真正的 MCP Server。

    这组用例是补上来的——**CI 第一次跑就抓到了这里的空白**：mcp 2.0 把低层
    Server 的装饰器 API（`@server.list_tools()`）换成了构造参数回调，1.x 的写法
    在 2.0 上直接 `AttributeError`。而当时所有 MCP 测试都只覆盖协议无关的那半
    （`tool_schemas` / `call_tool`），构造服务器这一步没人碰，于是在本机（1.x）
    全绿、在干净环境（装到 2.x）整个出口起不来。
    """

    @staticmethod
    def _handler(server, method: str, request_type):
        """跨 SDK 大版本取回注册好的 handler。

        2.0 按 method 字符串注册并返回 HandlerEntry；1.x 用请求类型做键。
        """
        if hasattr(server, "get_request_handler"):  # mcp >= 2.0
            entry = server.get_request_handler(method)
            return entry.handler if entry is not None else None
        return server.request_handlers.get(request_type)

    def test_server_constructs(self, store, sources):
        from sourcepilot.mcp_server import create_server

        server = create_server(store=store, sources=sources)
        assert server is not None

    def test_both_tool_methods_are_registered(self, store, sources):
        """tools/list 与 tools/call 都得挂上——少一个，客户端就看不到工具或调不动。"""
        import mcp.types as types

        from sourcepilot.mcp_server import create_server

        server = create_server(store=store, sources=sources)
        assert self._handler(server, "tools/list", types.ListToolsRequest) is not None
        assert self._handler(server, "tools/call", types.CallToolRequest) is not None

    def test_tool_definitions_come_from_the_contract(self, store, sources):
        """六个工具、schema 由契约的 pydantic 模型生成——**定义只有一份**。"""
        import mcp.types as types

        from sourcepilot.mcp_server import create_server

        create_server(store=store, sources=sources)  # 构造不抛就够，定义本身在下面对
        tools = [types.Tool(**schema) for schema in tool_schemas()]
        assert len(tools) == len(TOOL_REGISTRY)
        # 字段在 1.x 叫 inputSchema、2.0 叫 input_schema（camelCase 仍作 alias 接受）
        schema = getattr(tools[0], "input_schema", None) or tools[0].inputSchema
        assert schema["type"] == "object"
