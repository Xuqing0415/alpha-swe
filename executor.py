"""工具执行器——注册工具并执行，集成沙箱拦截"""
import json
import logging
from typing import Dict, Optional, Any
from tools.base import BaseTool, ToolResult
from tools.terminal import TerminalTool
from tools.file_tool import FileTool

logger = logging.getLogger("alpha-swe.executor")


class Executor:
    """工具注册与执行引擎"""

    def __init__(self, sandbox=None, mcp_config: dict = None):
        self.tools: Dict[str, BaseTool] = {}
        self.sandbox = sandbox  # 第六关：沙箱引用
        self.mcp_config = mcp_config or {}  # 第七关：MCP 配置
        self._register_defaults()

    def _register_defaults(self):
        """注册默认工具"""
        self.register(TerminalTool())
        self.register(FileTool())

    def register(self, tool: BaseTool):
        """注册工具"""
        self.tools[tool.name] = tool
        logger.info(f"工具已注册: {tool.name}")

    def unregister(self, name: str):
        """注销工具"""
        if name in self.tools:
            del self.tools[name]
            logger.info(f"工具已注销: {name}")

    def get_tools(self) -> list:
        """获取所有已注册工具"""
        return list(self.tools.values())

    def execute(self, action: str, params: dict = None) -> ToolResult:
        """执行工具调用，经过沙箱拦截"""
        params = params or {}
        tool_name = action

        # 第七关：MCP 配置控制
        if self.mcp_config:
            enabled = self.mcp_config.get("tools", {}).get(tool_name, {}).get("enabled", True)
            if not enabled:
                return ToolResult(
                    success=False, output="",
                    error=f"工具 {tool_name} 已被 MCP 配置禁用"
                )

        tool = self.tools.get(tool_name)
        if not tool:
            return ToolResult(
                success=False, output="",
                error=f"未找到工具: {tool_name}，可用工具: {list(self.tools.keys())}"
            )

        # 第六关：沙箱拦截
        if self.sandbox:
            allowed, reason = self.sandbox.check(tool_name, params)
            if not allowed:
                logger.warning(f"沙箱拦截: {tool_name} -> {reason}")
                return ToolResult(
                    success=False, output="",
                    error=f"Permission Denied (Sandbox blocked): {reason}"
                )

        try:
            logger.info(f"执行工具: {tool_name} params={json.dumps(params, ensure_ascii=False)[:200]}")
            return tool.execute(**params)
        except Exception as e:
            logger.error(f"工具执行异常: {tool_name} -> {e}")
            return ToolResult(success=False, output="", error=str(e))

    def execute_by_name(self, tool_name: str, params: dict = None) -> ToolResult:
        """按名称执行工具"""
        return self.execute(tool_name, params)