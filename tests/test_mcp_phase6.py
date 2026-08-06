"""阶段六 MCP 测试：断连重连/降级隐藏、资源缓存 TTL、调用路由。

使用可注入的 FakeMCPClient 离线验证重连与降级逻辑；
资源缓存用真实 mcp_test_server 验证（含 SDK 实际路径）。
"""
import sys
from pathlib import Path

import pytest

from agent.config import MCPClientConfig
from agent.core.decision_logger import DecisionLogger
from agent.mcp.manager import MCPManager
from agent.tools.base import ToolResult

SERVER = str(Path(__file__).parent / "mcp_test_server.py")


class FakeMCPClient:
    """可编程的 MCP 客户端替身：fail_first / always_fail 控制连接行为。"""

    def __init__(self, cfg, tool_timeout=30.0, fail_first=False,
                 always_fail=False):
        self.config = cfg
        self.connected = False
        self._fail_first = fail_first
        self._always_fail = always_fail
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if self._always_fail or (self._fail_first and self.connect_calls == 1):
            return False
        self.connected = True
        return True

    async def close(self):
        self.connected = False

    async def list_tools(self):
        return [{"name": "fake_tool", "title": "", "description": "fake 工具",
                 "parameters": {"type": "object", "properties": {}}}]

    async def list_resources(self):
        return [{"name": "r1", "uri": "mem://r1", "description": "resource one"}]

    async def read_resource(self, uri):
        return f"content of {uri}"

    async def call_tool(self, name, arguments):
        return ToolResult(success=True, output="fake-ok")


def make_fake_manager(dl=None, fail_first=False, always_fail=False, ttl=60.0):
    servers = [MCPClientConfig(name="test", transport="stdio", command="x")]
    return MCPManager(
        servers=servers,
        connect_timeout=2.0,
        tool_timeout=5.0,
        reconnect_attempts=2,
        reconnect_delay=0.01,
        resource_cache_ttl=ttl,
        decision_logger=dl,
        client_factory=lambda cfg: FakeMCPClient(
            cfg, tool_timeout=5.0, fail_first=fail_first,
            always_fail=always_fail),
    )


# ---- 6.1 断连重连与降级 ----
@pytest.mark.asyncio
async def test_ensure_connected_retries_failed_server():
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, fail_first=True)
    ok = await mgr.ensure_connected()
    assert ok == 1
    assert mgr.failed_servers == []
    client = list(mgr._clients.values())[0]
    assert client.connect_calls == 2  # 首次失败 + 重连成功
    assert any(dp.name == "mcp.reconnect" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_failed_server_degrades_tools_away():
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, always_fail=True)
    ok = await mgr.ensure_connected()
    assert ok == 0
    assert mgr.failed_servers == ["test"]
    tools = await mgr.build_tools()
    assert tools == []  # 不可用服务器的工具被隐藏（降级）
    assert any(dp.name == "mcp.degrade" for dp in dl.decisions)


@pytest.mark.asyncio
async def test_retry_connect_restores_server():
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, fail_first=True)
    await mgr.connect_all()          # 首次失败
    assert mgr.failed_servers == ["test"]
    added = await mgr.retry_connect()  # 手动重连
    assert added == 1
    assert mgr.failed_servers == []
    assert len(await mgr.build_tools()) == 1


# ---- 6.2 资源缓存 TTL ----
@pytest.mark.asyncio
async def test_resource_cache_hit_and_invalidate():
    dl = DecisionLogger()
    mgr = make_fake_manager(dl, ttl=60.0)
    assert await mgr.ensure_connected() == 1
    first = await mgr.read_resource("test", "mem://r1")
    second = await mgr.read_resource("test", "mem://r1")
    assert first == second == "content of mem://r1"
    assert mgr.cache_info()["entries"] == 1
    assert any(dp.name == "mcp.resource_cache_hit" for dp in dl.decisions)
    assert mgr.invalidate_resources("test") == 1
    assert mgr.cache_info()["entries"] == 0


@pytest.mark.asyncio
async def test_resource_cache_disabled_when_ttl_zero():
    mgr = make_fake_manager(ttl=0.0)
    assert await mgr.ensure_connected() == 1
    await mgr.read_resource("test", "mem://r1")
    await mgr.read_resource("test", "mem://r1")
    assert mgr.cache_info()["entries"] == 0  # TTL=0 不缓存


@pytest.mark.asyncio
async def test_resource_cache_expires_by_ttl():
    mgr = make_fake_manager(ttl=0.05)
    assert await mgr.ensure_connected() == 1
    await mgr.read_resource("test", "mem://r1")
    import asyncio
    await asyncio.sleep(0.08)  # 超过 TTL
    await mgr.read_resource("test", "mem://r1")
    # 第二次读发生在过期后 -> 重新拉取并重写缓存（条目仍为 1）
    assert mgr.cache_info()["entries"] == 1


# ---- 6.2b 真实服务器上的缓存（走 mcp SDK 实际路径） ----
@pytest.mark.asyncio
async def test_real_server_resource_cache(ws_tmp):
    dl = DecisionLogger()
    mgr = MCPManager(
        servers=[MCPClientConfig(name="test", transport="stdio",
                                 command=sys.executable, args=[SERVER])],
        connect_timeout=10.0, tool_timeout=10.0,
        reconnect_attempts=1, reconnect_delay=0.1,
        resource_cache_ttl=60.0,
        decision_logger=dl,
    )
    try:
        assert await mgr.ensure_connected() == 1
        a = await mgr.read_resource("test", "memory://facts")
        b = await mgr.read_resource("test", "memory://facts")
        assert a == b and "pytest" in a
        assert any(dp.name == "mcp.resource_cache_hit" for dp in dl.decisions)
    finally:
        await mgr.disconnect_all()
