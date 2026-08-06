"""MCP 客户端测试：握手、工具列表、工具调用、资源读取、失败容忍。"""
import sys
from pathlib import Path

import pytest

from agent.config import MCPClientConfig
from agent.mcp.client import MCPClient
from agent.mcp.manager import MCPManager

SERVER = str(Path(__file__).parent / "mcp_test_server.py")


def server_cfg(**kw):
    return MCPClientConfig(
        name="test", transport="stdio",
        command=sys.executable, args=[SERVER], **kw,
    )


@pytest.mark.asyncio
async def test_connect_handshake_and_tools():
    client = MCPClient(server_cfg(), tool_timeout=10.0)
    assert await client.connect() is True
    tools = await client.list_tools()
    names = [t["name"] for t in tools]
    assert "add" in names and "echo" in names
    add = next(t for t in tools if t["name"] == "add")
    assert "properties" in add["parameters"]
    await client.close()


@pytest.mark.asyncio
async def test_call_tool():
    client = MCPClient(server_cfg(), tool_timeout=10.0)
    await client.connect()
    res = await client.call_tool("add", {"a": 2, "b": 3})
    assert res.success
    assert "5" in res.output
    assert res.metadata["mcp_server"] == "test"
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_before_connect():
    client = MCPClient(server_cfg(), tool_timeout=10.0)
    res = await client.call_tool("add", {"a": 1, "b": 1})
    assert res.success is False
    assert "未连接" in res.error
    await client.close()


@pytest.mark.asyncio
async def test_resources():
    client = MCPClient(server_cfg(), tool_timeout=10.0)
    await client.connect()
    resources = await client.list_resources()
    assert resources
    assert any("facts" in r["name"] for r in resources)
    text = await client.read_resource("memory://facts")
    assert "pytest" in text
    await client.close()


@pytest.mark.asyncio
async def test_connect_failure_tolerant():
    bad = MCPClientConfig(name="bad", transport="stdio",
                          command="nonexistent-cmd-xyz", args=["--nope"])
    client = MCPClient(bad, tool_timeout=5.0)
    assert await client.connect() is False
    await client.close()


@pytest.mark.asyncio
async def test_manager_connect_tools_and_call():
    manager = MCPManager(servers=[server_cfg()],
                         connect_timeout=10.0, tool_timeout=10.0)
    assert await manager.connect_all() == 1
    tools = await manager.build_tools()
    assert any(t.name == "add" for t in tools)
    res = await manager.call_tool("test", "add", {"a": 1, "b": 1})
    assert res.success and "2" in res.output
    assert manager.connected is True
    await manager.disconnect_all()
    assert manager.connected is False


@pytest.mark.asyncio
async def test_manager_tolerates_bad_server():
    manager = MCPManager(
        servers=[server_cfg(), MCPClientConfig(name="bad", transport="stdio",
                                               command="no-such-cmd")],
        connect_timeout=10.0, tool_timeout=10.0,
    )
    assert await manager.connect_all() == 1  # 好服务器连上，坏服务器被忽略
    await manager.disconnect_all()