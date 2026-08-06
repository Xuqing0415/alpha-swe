"""MCP 集成层：客户端、管理器、工具适配。

对应设计第 13 节：
- 启动时按 config/mcp.yaml 连接服务器并执行 initialize 握手；
- MCP 工具合并为统一 Tool 接口；
- 资源/提示可被上下文管理器拉取注入 Prompt。
"""
from agent.mcp.client import MCPClient
from agent.mcp.manager import MCPManager
from agent.mcp.tool import MCPTool

__all__ = ["MCPClient", "MCPManager", "MCPTool"]