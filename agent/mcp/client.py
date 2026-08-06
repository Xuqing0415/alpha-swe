"""单个 MCP 服务器的客户端封装。

支持 transport:
- stdio: 本地命令（npx / python ...）
- sse: 远程 URL
- streamable-http / http: 现代远程端点
"""
from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from agent.config import MCPClientConfig
from agent.tools.base import ToolResult

logger = logging.getLogger("alpha-swe.mcp")

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    HAS_MCP = True
except ImportError:  # mcp SDK 未安装时的降级标记
    HAS_MCP = False

    class ClientSession:  # type: ignore[no-redef]
        pass


class MCPClient:
    """封装一个 MCP 服务器的连接生命周期。"""

    def __init__(self, config: MCPClientConfig, tool_timeout: float = 30.0):
        if not HAS_MCP:
            raise RuntimeError("MCP 功能需要安装 mcp SDK: pip install mcp")
        self.config = config
        self.tool_timeout = tool_timeout
        self._session: Optional[ClientSession] = None
        self._stack: Optional[AsyncExitStack] = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    # ---- 生命周期 ----
    async def connect(self) -> bool:
        """initialize 握手；失败返回 False 且不影响其他服务器。"""
        if self.connected:
            return True
        stack = AsyncExitStack()
        ok = False
        try:
            read, write = await self._enter_transport(stack)
            session = ClientSession(read, write)
            await stack.enter_async_context(session)
            await session.initialize()
            self._session = session
            self._stack = stack
            ok = True
            logger.info("MCP 服务器已连接: %s (%s)", self.config.name, self.config.transport)
            return True
        except Exception as e:
            logger.warning("MCP 服务器 %s 连接失败: %s", self.config.name, e)
            return False
        finally:
            # 握手失败或被 asyncio.timeout 取消（CancelledError 不是 Exception 子类，
            # except Exception 捕获不到）时也必须关闭 AsyncExitStack，否则 stdio
            # 子进程与后台任务会泄漏，拖住事件循环 teardown。
            if not ok:
                await self._safe_aclose(stack)

    @staticmethod
    async def _safe_aclose(stack: AsyncExitStack) -> None:
        try:
            # shield：即使当前任务正被取消，也要把传输层/session 干净关闭。
            import anyio
            with anyio.CancelScope(shield=True):
                await stack.aclose()
        except Exception:
            logger.debug("MCP 连接失败后的资源清理异常", exc_info=True)

    async def close(self) -> None:
        # 先置空句柄，保证 connected 立即失效；关闭异常只记日志
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await self._safe_aclose(stack)

    # ---- 发现 ----
    async def list_tools(self) -> List[Dict[str, Any]]:
        """获取工具列表（统一 schema 结构）。"""
        self._require_connected()
        res = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "title": t.title or "",
                "description": t.description or "",
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            }
            for t in res.tools
        ]

    async def list_resources(self) -> List[Dict[str, Any]]:
        self._require_connected()
        res = await self._session.list_resources()
        return [
            {"name": r.name or r.uri, "uri": r.uri, "description": r.description or ""}
            for r in res.resources
        ]

    async def read_resource(self, uri: str) -> str:
        self._require_connected()
        res = await self._session.read_resource(uri)
        parts = []
        for content in res.contents:
            text = getattr(content, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(content))
        return "\n".join(parts)

    # ---- 调用 ----
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        if not self.connected:
            return ToolResult(success=False,
                              error=f"MCP 服务器未连接: {self.config.name}")
        try:
            res = await self._session.call_tool(
                name, arguments, read_timeout_seconds=self.tool_timeout
            )
            output = self._format_call_result(res)
            if getattr(res, "is_error", False):
                return ToolResult(
                    success=False, output=output,
                    error=output or f"MCP 工具 {name} 调用失败",
                    metadata={"mcp_server": self.config.name, "mcp_tool": name},
                )
            return ToolResult(
                success=True, output=output or "(empty)",
                metadata={"mcp_server": self.config.name, "mcp_tool": name},
            )
        except Exception as e:
            return ToolResult(
                success=False, error=f"MCP 调用失败: {e}",
                metadata={"mcp_server": self.config.name, "mcp_tool": name},
            )

    # ---- 内部 ----
    def _require_connected(self) -> None:
        if not self.connected:
            raise ConnectionError(f"MCP 服务器未连接: {self.config.name}")

    async def _enter_transport(self, stack: AsyncExitStack) -> Tuple[Any, Any]:
        cfg = self.config
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError("stdio transport 需要 command")
            params = StdioServerParameters(
                command=cfg.command,
                args=list(cfg.args or []),
                env={**os.environ, **(cfg.env or {})},
            )
            return await stack.enter_async_context(stdio_client(params))
        if cfg.transport == "sse":
            if not cfg.url:
                raise ValueError("sse transport 需要 url")
            return await stack.enter_async_context(sse_client(cfg.url))
        if cfg.transport in ("streamable-http", "http"):
            if not cfg.url:
                raise ValueError(f"{cfg.transport} transport 需要 url")
            return await stack.enter_async_context(streamable_http_client(cfg.url))
        raise ValueError(f"未知 transport: {cfg.transport}")

    @staticmethod
    def _format_call_result(res: Any) -> str:
        parts = []
        for block in getattr(res, "content", None) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(block))
        if not parts:
            structured = getattr(res, "structured_content", None)
            if structured is not None:
                parts.append(str(structured))
        return "\n".join(parts)