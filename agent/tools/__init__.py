"""工具层：统一 Tool 接口、ExecutionContext、ToolResult。"""
from agent.tools.base import ExecutionContext, Tool, ToolResult

__all__ = ["ExecutionContext", "Tool", "ToolResult"]