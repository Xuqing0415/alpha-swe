"""MCP 管理器 —— 管理多个服务器连接、工具合并与资源订阅。

对应设计第 13.3 节：
- 启动时依次连接所有配置的服务器（失败容忍，不阻塞 Agent 启动）；
- build_tools() 把各服务器工具合并为 MCPTool 列表；
- list_resources() / read_resource() 供上下文管理器拉取资源。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.config import MCPClientConfig, MCPConfig, MCPOptions
from agent.mcp.client import MCPClient
from agent.mcp.tool import MCPTool
from agent.tools.base import ToolResult

logger = logging.getLogger("alpha-swe.mcp.manager")


class MCPManager:
    def __init__(self, servers: List[MCPClientConfig],
                 connect_timeout: float = 8.0,
                 tool_timeout: float = 30.0):
        self.connect_timeout = connect_timeout
        self.tool_timeout = tool_timeout
        self._clients: Dict[str, MCPClient] = {
            cfg.name: MCPClient(cfg, tool_timeout=tool_timeout)
            for cfg in servers
        }

    @classmethod
    def from_config(cls, mcp_cfg: Optional[MCPConfig] = None,
                    options: Optional[MCPOptions] = None) -> "MCPManager":
        mcp_cfg = mcp_cfg or MCPConfig()
        opts = options or MCPOptions()
        return cls(servers=mcp_cfg.mcp_servers,
                   connect_timeout=opts.connect_timeout,
                   tool_timeout=opts.tool_timeout)

    # ---- 连接 ----
    async def connect_all(self) -> int:
        """连接所有服务器，返回成功连接数。

        必须：在调用者的 task 中逐个连接（不能用 asyncio.gather / wait_for 把
        connect() 包进子任务）。mcp SDK 的 ClientSession 与 stdio_client 会在
        connect() 所在任务中创建 anyio 任务组/取消作用域；若后续 close() 在另一个
        task 中退出这些作用域，会抛出
        "Attempted to exit cancel scope in a different task" 并残留后台任务。
        """
        if not self._clients:
            return 0
        ok = 0
        for name, client in self._clients.items():
            try:
                async with asyncio.timeout(self.connect_timeout):
                    if await client.connect():
                        ok += 1
            except Exception as e:
                logger.warning("连接 MCP 服务器 %s 超时/失败: %s", name, e)
        return ok

    async def disconnect_all(self) -> None:
        # close() 同样必须在连接时所在的同一 task 中执行（原因同上）。
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as e:
                logger.warning("关闭 MCP 服务器 %s 异常: %s", client.config.name, e)

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self._clients.values())

    # ---- 工具 ----
    async def build_tools(self) -> List[MCPTool]:
        """把已连接服务器的工具合并为 MCPTool 列表。"""
        tools: List[MCPTool] = []
        for server_name, client in self._clients.items():
            if not client.connected:
                continue
            try:
                for t in await client.list_tools():
                    tools.append(MCPTool(
                        server_name=server_name,
                        name=t["name"],
                        description=t["description"],
                        parameters=t["parameters"],
                        manager=self,
                        timeout=self.tool_timeout,
                    ))
            except Exception as e:
                logger.warning("获取 MCP 服务器 %s 的工具列表失败: %s",
                               server_name, e)
        return tools

    async def call_tool(self, server_name: str, name: str,
                        arguments: Dict[str, Any]) -> ToolResult:
        client = self._clients.get(server_name)
        if client is None:
            return ToolResult(success=False,
                              error=f"未知 MCP 服务器: {server_name}")
        return await client.call_tool(name, arguments)

    # ---- 资源 ----
    async def list_resources(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for server_name, client in self._clients.items():
            if not client.connected:
                continue
            try:
                for r in await client.list_resources():
                    out.append({"server": server_name, **r})
            except Exception as e:
                logger.warning("获取 MCP 服务器 %s 的资源列表失败: %s",
                               server_name, e)
        return out

    async def read_resource(self, server_name: str, uri: str) -> str:
        client = self._clients.get(server_name)
        if client is None or not client.connected:
            return ""
        try:
            return await client.read_resource(uri)
        except Exception as e:
            logger.warning("读取 MCP 资源失败 %s/%s: %s", server_name, uri, e)
            return ""

    # ---- 状态 ----
    def status(self) -> Dict[str, bool]:
        return {name: client.connected for name, client in self._clients.items()}