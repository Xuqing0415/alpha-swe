"""MCP 管理器 —— 管理多个服务器连接、工具合并、资源订阅与缓存。

对应设计第 13.3 节与阶段六：
- ensure_connected(): 首次连接 + 按配置自动重连失败服务器（mcp.reconnect_attempts）；
- 失败降级：失败服务器记录在 _failed，其工具从 build_tools() 中隐藏（mcp.degrade）；
- read_resource(): 资源缓存（TTL=mcp.resource_cache_ttl），命中记录 mcp.resource_cache_hit；
- list_resources() / read_resource() 供上下文管理器拉取资源。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.config import MCPClientConfig, MCPConfig, MCPOptions
from agent.mcp.client import MCPClient
from agent.mcp.tool import MCPTool
from agent.tools.base import ToolResult

logger = logging.getLogger("alpha-swe.mcp.manager")

CacheKey = Tuple[str, str]


class MCPManager:
    def __init__(self, servers: List[MCPClientConfig],
                 connect_timeout: float = 8.0,
                 tool_timeout: float = 30.0,
                 reconnect_attempts: int = 2,
                 reconnect_delay: float = 1.0,
                 resource_cache_ttl: float = 60.0,
                 decision_logger=None,
                 client_factory: Optional[Callable[[MCPClientConfig], MCPClient]] = None):
        self.connect_timeout = connect_timeout
        self.tool_timeout = tool_timeout
        self.reconnect_attempts = max(0, reconnect_attempts)
        self.reconnect_delay = max(0.0, reconnect_delay)
        self.resource_cache_ttl = max(0.0, resource_cache_ttl)
        self._decision = decision_logger
        self._clients: Dict[str, MCPClient] = {
            cfg.name: (client_factory(cfg) if client_factory
                       else MCPClient(cfg, tool_timeout=tool_timeout))
            for cfg in servers
        }
        self._failed: set[str] = set()
        self._resource_cache: Dict[CacheKey, Tuple[float, str]] = {}

    @classmethod
    def from_config(cls, mcp_cfg: Optional[MCPConfig] = None,
                    options: Optional[MCPOptions] = None,
                    decision_logger=None) -> "MCPManager":
        mcp_cfg = mcp_cfg or MCPConfig()
        opts = options or MCPOptions()
        return cls(servers=mcp_cfg.mcp_servers,
                   connect_timeout=opts.connect_timeout,
                   tool_timeout=opts.tool_timeout,
                   reconnect_attempts=opts.reconnect_attempts,
                   reconnect_delay=opts.reconnect_delay,
                   resource_cache_ttl=opts.resource_cache_ttl,
                   decision_logger=decision_logger)

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
                        self._failed.discard(name)
                        self._log("mcp.connect", "mcp.enabled", True,
                                  f"服务器 {name} 已连接")
                    else:
                        self._failed.add(name)
            except Exception as e:
                self._failed.add(name)
                self._log("mcp.connect_failed", "mcp.enabled", True,
                          f"服务器 {name} 连接失败: {str(e)[:100]}")
        return ok

    async def retry_connect(self, names: Optional[List[str]] = None) -> int:
        """对失败（或指定）的服务器重连；返回新成功连接数。"""
        targets = names or sorted(self._failed)
        ok = 0
        for name in targets:
            client = self._clients.get(name)
            if client is None or client.connected:
                continue
            try:
                async with asyncio.timeout(self.connect_timeout):
                    if await client.connect():
                        ok += 1
                        self._failed.discard(name)
                        self._log("mcp.reconnect", "mcp.reconnect_attempts",
                                  self.reconnect_attempts,
                                  f"服务器 {name} 重连成功")
            except Exception as e:
                self._log("mcp.reconnect_failed", "mcp.reconnect_attempts",
                          self.reconnect_attempts,
                          f"服务器 {name} 重连失败: {str(e)[:100]}")
        return ok

    async def ensure_connected(self) -> int:
        """首次连接 + 按配置次数自动重连失败服务器（阶段六 6.1）。"""
        ok = await self.connect_all()
        attempt = 0
        while self._failed and attempt < self.reconnect_attempts:
            attempt += 1
            await asyncio.sleep(self.reconnect_delay)
            ok += await self.retry_connect()
        return ok

    async def disconnect_all(self) -> None:
        # close() 同样必须在连接时所在的同一 task 中执行（原因同上）。
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as e:
                logger.warning("关闭 MCP 服务器 %s 异常: %s", client.config.name, e)
        self._failed.clear()
        self._resource_cache.clear()

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self._clients.values())

    @property
    def failed_servers(self) -> List[str]:
        """当前不可用（连接失败）的服务器名。"""
        return sorted(self._failed)

    # ---- 工具 ----
    async def build_tools(self) -> List[MCPTool]:
        """把已连接服务器的工具合并为 MCPTool 列表；失败服务器降级隐藏工具。"""
        if self._failed:
            self._log("mcp.degrade", "mcp.enabled", True,
                      f"{len(self._failed)} 个服务器不可用，工具降级隐藏: "
                      f"{sorted(self._failed)}")
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
        """读取资源并缓存（TTL=mcp.resource_cache_ttl，0 表示不缓存）。"""
        client = self._clients.get(server_name)
        if client is None or not client.connected:
            return ""
        key: CacheKey = (server_name, uri)
        if self.resource_cache_ttl > 0:
            hit = self._resource_cache.get(key)
            if hit is not None and time.time() - hit[0] < self.resource_cache_ttl:
                self._log("mcp.resource_cache_hit", "mcp.resource_cache_ttl",
                          self.resource_cache_ttl,
                          f"资源缓存命中 {server_name}/{uri}")
                return hit[1]
        try:
            content = await client.read_resource(uri)
        except Exception as e:
            logger.warning("读取 MCP 资源失败 %s/%s: %s", server_name, uri, e)
            return ""
        if self.resource_cache_ttl > 0 and content:
            self._resource_cache[key] = (time.time(), content)
        return content

    def invalidate_resources(self, server_name: Optional[str] = None,
                             uri: Optional[str] = None) -> int:
        """失效缓存；返回清除条数。None 表示不限。"""
        keys = [
            k for k in self._resource_cache
            if (server_name is None or k[0] == server_name)
            and (uri is None or k[1] == uri)
        ]
        for k in keys:
            del self._resource_cache[k]
        return len(keys)

    def cache_info(self) -> Dict[str, Any]:
        return {
            "entries": len(self._resource_cache),
            "ttl": self.resource_cache_ttl,
            "keys": [f"{s}/{u}" for s, u in sorted(self._resource_cache)],
        }

    # ---- 状态 ----
    def status(self) -> Dict[str, bool]:
        return {name: client.connected for name, client in self._clients.items()}

    def _log(self, name: str, config_key: str, config_value: Any,
             decision: str) -> None:
        if self._decision is not None:
            self._decision.record(name, config_key, config_value, decision)


__all__ = ["MCPManager"]
