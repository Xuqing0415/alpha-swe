"""单个 MCP 服务器的客户端封装。

支持 transport:
- stdio: 本地命令（npx / python ...）
- sse: 远程 URL
- streamable-http / http: 现代远程端点
"""
from __future__ import annotations

import asyncio
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

    def __init__(self, config: MCPClientConfig, tool_timeout: float = 30.0,
                 connect_timeout: float = 8.0):
        if not HAS_MCP:
            raise RuntimeError("MCP 功能需要安装 mcp SDK: pip install mcp")
        self.config = config
        self.tool_timeout = tool_timeout
        self.connect_timeout = connect_timeout
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
        """在可能被取消的上下文中尽量干净地关闭 AsyncExitStack。

        必须在当前任务中执行 aclose：mcp SDK 的 stdio_client/sse_client 与
        ClientSession 会在 connect() 所在任务创建 anyio 任务组/取消作用域，
        跨任务关闭会抛 "Attempted to exit cancel scope in a different task
        than it was entered in" 并残留后台任务。因此不能用 asyncio.shield /
        ensure_future（会把清理挪到别的任务），也不能用
        anyio.CancelScope(shield=True)（会与 SDK 内部的取消作用域栈互相干扰，
        抛 "Attempted to exit a cancel scope that isn't the current task's
        current cancel scope"）。正确做法：若当前任务正被取消，先 uncancel
        消费取消信号，让 aclose 在同一任务里完整执行。
        """
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            task.uncancel()
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            # SDK 内部收尾可能再次触发取消；尽力等待其任务组结束
            logger.debug("MCP 清理过程被取消", exc_info=True)
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
            return await self._enter_async_gen(stack, stdio_client(params))
        if cfg.transport == "sse":
            if not cfg.url:
                raise ValueError("sse transport 需要 url")
            # 把握手超时下发给 httpx 传输层：不可达/挂死的端点会以普通异常
            # 快速失败，而不是一直挂到外层 asyncio.timeout 取消（后者会与
            # SDK 的 anyio 取消作用域互相干扰，见 _connect_one 注释）。
            return await self._enter_async_gen(
                stack, sse_client(cfg.url, timeout=self.connect_timeout)
            )
        if cfg.transport in ("streamable-http", "http"):
            if not cfg.url:
                raise ValueError(f"{cfg.transport} transport 需要 url")
            return await self._enter_async_gen(
                stack, streamable_http_client(cfg.url)
            )
        raise ValueError(f"未知 transport: {cfg.transport}")

    @staticmethod
    async def _enter_async_gen(stack: AsyncExitStack, agen) -> Tuple[Any, Any]:
        """进入 async 生成器上下文；__aenter__ 失败时显式 aclose 兜底。

        mcp SDK 的 stdio_client/sse_client 在 __aenter__ 内部创建 anyio
        任务组与取消作用域；若 __aenter__ 抛异常（含超时取消），生成器会被
        contextlib 直接丢弃，内部后台任务与取消作用域随之泄漏——泄漏的取消
        作用域会取消后续连接（表现为 "Cancelled via cancel scope ..." 崩溃，
        且同一进程内后续连接全部遭殃）。这里在失败路径上显式关闭生成器。
        """
        try:
            return await stack.enter_async_context(agen)
        except BaseException:  # noqa: B036
            try:
                import anyio
                with anyio.CancelScope(shield=True):
                    await agen.aclose()
            except BaseException:  # noqa: B036
                pass
            raise

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
