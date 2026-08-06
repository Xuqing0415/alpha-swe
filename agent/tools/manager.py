"""工具注册管理器 —— 注册/注销、按配置启停、schema 导出、沙箱前置校验。

对应设计第 4.3 节（动态化工具描述）与第 13.3 节（MCP 工具合并的入口）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext, Tool, ToolResult

logger = logging.getLogger("alpha-swe.tools")


class ToolManager:
    def __init__(self, policy: Optional[SandboxPolicy] = None):
        self._tools: Dict[str, Tool] = {}
        self.policy = policy or SandboxPolicy()

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        logger.info("工具已注册: %s", tool.name)
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        logger.info("工具已注销: %s", name)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def enabled_names(self, enabled: Optional[Dict[str, bool]] = None) -> List[str]:
        """按配置过滤：enabled=False 的工具对 LLM 隐藏。"""
        names = []
        for name in self._tools:
            if enabled is not None and not enabled.get(name, True):
                continue
            names.append(name)
        return names

    def schemas(self, enabled: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
        return [self._tools[n].to_schema() for n in self.enabled_names(enabled)]

    async def execute(self, name: str, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"未找到工具: {name}，可用: {self.names()}")

        # 沙箱前置校验（路径、危险命令、网络策略、文件保护）
        allowed, reason = self.policy.check(name, params, context)
        if not allowed:
            logger.warning("沙箱拦截: %s -> %s", name, reason)
            return ToolResult(success=False, error=f"沙箱拦截: {reason}")

        # 假网络模式：curl/wget 命中预设响应时直接返回，不发起真实请求
        fake = self.policy.intercept(name, params)
        if fake is not None:
            return ToolResult(success=True, output=fake,
                              metadata={"fake_network": True})

        return await tool.execute(params, context)