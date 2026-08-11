"""工具注册管理器 —— 注册/注销、按配置启停、schema 导出、沙箱前置校验。

对应设计第 4.3 节（动态化工具描述）与第 13.3 节（MCP 工具合并的入口）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import (ErrorCategory, ExecutionContext, Tool,
                               ToolResult)

logger = logging.getLogger("alpha-swe.tools")


class ToolManager:
    def __init__(self, policy: Optional[SandboxPolicy] = None,
                 default_timeout: float = 30.0):
        self._tools: Dict[str, Tool] = {}
        self.policy = policy or SandboxPolicy()
        self.default_timeout = default_timeout

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
                      context: ExecutionContext,
                      timeout: Optional[float] = None) -> ToolResult:
        """执行工具；支持外层超时安全网与统一错误分类（方案 2.1/4.1）。

        - timeout 为 None 时使用构造时的 default_timeout；
        - terminal_execute 自带 terminate->kill 的进程清理，不套外层
          wait_for（否则会留下孤儿子进程），其超时由工具层负责；
        - 超时返回 error_category=TRANSIENT + metadata.timed_out，
          供循环层做「连续超时熔断」。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                success=False,
                error=f"未找到工具: {name}，可用: {self.names()}",
                error_category=ErrorCategory.CONFIGURATION,
            )

        # 沙箱前置校验（路径、危险命令、网络策略、文件保护）
        allowed, reason = self.policy.check(name, params, context)
        if not allowed:
            logger.warning("沙箱拦截: %s -> %s", name, reason)
            return ToolResult(success=False, error=f"沙箱拦截: {reason}",
                              error_category=ErrorCategory.PERMISSION)

        # 假网络模式：curl/wget 命中预设响应时直接返回，不发起真实请求
        fake = self.policy.intercept(name, params)
        if fake is not None:
            return ToolResult(success=True, output=fake,
                              metadata={"fake_network": True})

        effective_timeout = (timeout if timeout is not None
                             else self.default_timeout)
        try:
            if (effective_timeout and effective_timeout > 0
                    and name != "terminal_execute"):
                return await asyncio.wait_for(
                    tool.execute(params, context), timeout=effective_timeout)
            return await tool.execute(params, context)
        except asyncio.TimeoutError:
            logger.warning("工具执行超时: %s（%.1fs）", name, effective_timeout)
            return ToolResult(
                success=False,
                error=f"工具 {name} 执行超时（{effective_timeout}s）并被终止",
                metadata={"timed_out": True},
                error_category=ErrorCategory.TRANSIENT,
            )
        except Exception as e:  # 工具自身异常兜底：转成结果而非让任务崩掉
            logger.exception("工具执行异常: %s", name)
            return ToolResult(success=False, error=f"工具 {name} 执行异常: {e}",
                              error_category=ErrorCategory.UNKNOWN)
