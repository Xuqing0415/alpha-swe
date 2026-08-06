"""MCP 工具适配 —— 把远程 MCP 工具包装成统一 Tool 接口。

对应设计第 6 节「统一接口」与第 13.3 节「工具调用路由」：
ToolManager 按工具实例分发给内建执行器或 MCP 客户端。
"""
from __future__ import annotations

from typing import Any, Dict

from agent.tools.base import ExecutionContext, Tool, ToolResult


class MCPTool(Tool):
    """指向某个 MCP 服务器工具的 Tool 包装。"""

    def __init__(self, server_name: str, name: str, description: str,
                 parameters: Dict[str, Any], manager, timeout: float = 30.0):
        self.server_name = server_name
        self.name = name
        self.description = f"{description} (via MCP server: {server_name})"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._manager = manager
        self.timeout = timeout

    async def execute(self, params: Dict[str, Any], context: ExecutionContext) -> ToolResult:
        return await self._manager.call_tool(self.server_name, self.name, params)