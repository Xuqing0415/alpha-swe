"""异步文件 IO 工具 —— read / write / append / search(regex)。

路径一律锚定在沙箱工作区内，禁止路径穿越；文件操作放到线程池执行。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.code.ast_summary import is_code_file, summarize_file
from agent.sandbox.audit import FileAuditStore
from agent.tools.base import ErrorCategory, ExecutionContext, Tool, ToolResult

TRAVERSAL_PATTERN = re.compile(r"(\.\./|\.\.\\)")


def resolve_workspace_path(workspace: str, path: str) -> Path:
    """把相对路径解析到工作区内；绝对路径必须落在工作区内。"""
    root = Path(workspace).resolve()
    p = Path(path)
    if p.is_absolute():
        resolved = p
    else:
        resolved = root / p
    resolved = resolved.resolve()
    if not (resolved == root or root in resolved.parents):
        raise PermissionError(f"路径越界，拒绝访问: {path}")
    return resolved


WRITE_ACTIONS = {"write", "append", "edit", "delete", "rm"}


class FileIOTool(Tool):
    name = "file_ops"
    description = "沙箱内的文件读写操作: read/write/append/search"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "append", "search"]},
            "path": {"type": "string", "description": "工作区相对路径或合法绝对路径"},
            "content": {"type": "string", "description": "写入内容（write/append 需要）"},
            "pattern": {"type": "string", "description": "正则表达式（search 需要）"},
        },
        "required": ["action", "path"],
    }

    def __init__(self, workspace: str = "", read_only: bool = False,
                 audit_store: Optional[FileAuditStore] = None, docker=None,
                 call_graph=None, decision_logger=None):
        self.workspace = workspace
        self.read_only = read_only
        self.audit_store = audit_store
        self.docker = docker  # DockerSandbox；running 时文件操作路由进容器
        self.call_graph = call_graph  # 项目级调用图（读取时注入影响范围）
        self.decision_logger = decision_logger

    async def execute(self, params: Dict[str, Any], context: ExecutionContext) -> ToolResult:
        action = str(params.get("action", ""))
        path = str(params.get("path", ""))
        start = time.time()

        if not path:
            return ToolResult(success=False, error="缺少 path 参数", elapsed_ms=0.0,
                              error_category=ErrorCategory.PERMANENT)
        if self.read_only and action in WRITE_ACTIONS:
            return ToolResult(
                success=False,
                error=f"只读角色禁止写操作: {action}",
                elapsed_ms=0.0,
                error_category=ErrorCategory.PERMISSION,
            )
        if TRAVERSAL_PATTERN.search(path):
            return ToolResult(success=False, error=f"禁止路径穿越: {path}", elapsed_ms=0.0,
                              error_category=ErrorCategory.PERMISSION)

        try:
            target = resolve_workspace_path(context.workspace, path)
        except PermissionError as e:
            return ToolResult(success=False, error=str(e), elapsed_ms=0.0,
                              error_category=ErrorCategory.PERMISSION)

        if self.docker is not None and getattr(self.docker, "running", False):
            rel = os.path.relpath(target, os.path.abspath(context.workspace))
            rel = rel.replace("\\", "/")
            try:
                if action == "read":
                    content = await self.docker.read_file(rel)
                    output, meta = self._augment_read(str(target), content)
                    return ToolResult(success=True, output=output,
                                      metadata={"path": str(target), "docker": True,
                                                "size": len(content), **meta},
                                      elapsed_ms=(time.time() - start) * 1000)
                if action == "write":
                    before = await self._docker_before(rel)
                    await self.docker.write_file(rel, params.get("content", ""))
                    self._audit("write", target, before, params.get("content", ""),
                                task_id=context.task_id or "")
                    return ToolResult(success=True,
                                      output=f"写入成功（容器内）: {target}",
                                      metadata={"path": str(target), "docker": True,
                                                "diff_before": before,
                                                "diff_after": params.get("content", "")},
                                      elapsed_ms=(time.time() - start) * 1000)
                if action == "append":
                    before = await self._docker_before(rel)
                    await self.docker.append_file(rel, params.get("content", ""))
                    self._audit("append", target, before,
                                (before or "") + params.get("content", ""),
                                task_id=context.task_id or "")
                    return ToolResult(success=True,
                                      output=f"追加成功（容器内）: {target}",
                                      metadata={"path": str(target), "docker": True,
                                                "diff_before": before,
                                                "diff_after": (before or "") + params.get("content", "")},
                                      elapsed_ms=(time.time() - start) * 1000)
                if action == "search":
                    output = await self.docker.search_file(
                        params.get("pattern", ""), rel)
                    return ToolResult(success=True, output=output or f"未匹配到 '{params.get('pattern', '')}'",
                                      metadata={"path": str(target), "docker": True},
                                      elapsed_ms=(time.time() - start) * 1000)
                return ToolResult(success=False, error=f"未知操作: {action}",
                                  elapsed_ms=(time.time() - start) * 1000,
                                  error_category=ErrorCategory.PERMANENT)
            except Exception as e:
                return ToolResult(success=False, error=str(e),
                                  elapsed_ms=(time.time() - start) * 1000,
                                  error_category=ErrorCategory.UNKNOWN)

        try:
            if action == "read":
                return await self._read(target, start)
            if action == "write":
                return await self._write(target, params.get("content", ""), start,
                                         task_id=context.task_id or "")
            if action == "append":
                return await self._append(target, params.get("content", ""), start,
                                          task_id=context.task_id or "")
            if action == "search":
                return await self._search(target, params.get("pattern", ""), start)
            return ToolResult(success=False, error=f"未知操作: {action}",
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.PERMANENT)
        except Exception as e:
            return ToolResult(success=False, error=str(e),
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.UNKNOWN)

    async def _read(self, target: Path, start: float) -> ToolResult:
        if not await asyncio.to_thread(target.exists):
            return ToolResult(success=False, error=f"文件不存在: {target}",
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.PERMANENT)
        data = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="ignore")
        output, meta = self._augment_read(str(target), data)
        return ToolResult(success=True, output=output,
                          metadata={"path": str(target), "size": len(data), **meta},
                          elapsed_ms=(time.time() - start) * 1000)

    def _augment_read(self, path: str, content: str):
        """读取代码文件时附加 AST 摘要与调用图影响面（阶段一 1.1/1.2）。

        返回 (augmented_output, extra_metadata)；非代码文件原样返回。
        """
        meta: Dict[str, Any] = {}
        if not is_code_file(path) or not content:
            return content, meta
        summary = summarize_file(path, content)
        block = summary.to_text()
        if not block:
            return content, meta
        meta["ast_summary"] = True
        if self.decision_logger is not None:
            self.decision_logger.record(
                "symbol.retrieved", "code.ast", True,
                f"读取 {path} 提取 {len(summary.symbols)} 个符号"
                f"（{len(summary.imports)} 个依赖）",
            )
        if self.call_graph is not None:
            impact = []
            for s in summary.symbols[:20]:
                callers = self.call_graph.callers_of(s.name)
                if callers:
                    impact.append(f"{s.name} 被 {len(callers)} 处调用")
                callees = [c for c in self.call_graph.callees_of(s.name)
                           if self.call_graph.defs.get(c)]
                if callees:
                    impact.append(
                        f"{s.name} 调用 {','.join(callees[:4])}")
            if impact:
                block += "\n影响范围: " + ", ".join(impact[:10])
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "call_graph.hit", "code.call_graph", len(impact),
                        f"符号影响面: {', '.join(impact[:5])}",
                    )
        return f"{content}\n\n{block}", meta

    async def _write(self, target: Path, content: str, start: float,
                     task_id: str = "") -> ToolResult:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        before = await self._read_before(target)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        self._audit("write", target, before, content, task_id)
        return ToolResult(success=True, output=f"写入成功: {target}",
                          metadata={"path": str(target), "size": len(content),
                                    "audited": self.audit_store is not None,
                                    "diff_before": before,
                                    "diff_after": content},
                          elapsed_ms=(time.time() - start) * 1000)

    async def _append(self, target: Path, content: str, start: float,
                      task_id: str = "") -> ToolResult:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        before = await self._read_before(target)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        after = (before or "") + content
        self._audit("append", target, before, after, task_id)
        return ToolResult(success=True, output=f"追加成功: {target}",
                          metadata={"path": str(target),
                                    "audited": self.audit_store is not None,
                                    "diff_before": before,
                                    "diff_after": after},
                          elapsed_ms=(time.time() - start) * 1000)

    @staticmethod
    async def _read_before(target: Path) -> Optional[str]:
        """写入前读取旧内容（文件不存在返回 None）。"""
        if not await asyncio.to_thread(target.exists):
            return None
        return await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )

    def _audit(self, action: str, target: Path, before: Optional[str],
               after: str, task_id: str) -> None:
        if self.audit_store is None:
            return
        try:
            self.audit_store.record(action, str(target), before, after,
                                    task_id=task_id)
        except Exception as e:
            logging.getLogger("alpha-swe.tools").warning("文件审计失败: %s", e)

    async def _docker_before(self, rel: str) -> Optional[str]:
        """docker 模式下写入前读取旧内容（不存在返回 None）。"""
        try:
            return await self.docker.read_file(rel)
        except FileNotFoundError:
            return None

    async def _search(self, target: Path, pattern: str, start: float) -> ToolResult:
        if not pattern:
            return ToolResult(success=False, error="search 需要 pattern 参数",
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.PERMANENT)
        if not await asyncio.to_thread(target.exists):
            return ToolResult(success=False, error=f"路径不存在: {target}",
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.PERMANENT)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(success=False, error=f"正则无效: {e}",
                              elapsed_ms=(time.time() - start) * 1000,
                              error_category=ErrorCategory.PERMANENT)

        def _scan(path: Path) -> List[str]:
            hits: List[str] = []
            if path.is_file():
                _match_in_file(path, regex, hits)
            elif path.is_dir():
                for p in sorted(path.rglob("*")):
                    if p.is_file() and p.suffix in (".py", ".ts", ".js", ".tsx", ".jsx",
                                                    ".md", ".txt", ".json", ".yaml", ".yml"):
                        _match_in_file(p, regex, hits)
            return hits

        def _match_in_file(p: Path, rx: "re.Pattern[str]", hits: List[str]) -> None:
            try:
                for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{p.relative_to(target.parent) if target.is_dir() else p}:{lineno}: {line.strip()[:200]}")
            except OSError:
                pass

        hits = await asyncio.to_thread(_scan, target)
        if not hits:
            return ToolResult(success=True, output=f"未匹配到 '{pattern}'",
                              metadata={"hits": 0}, elapsed_ms=(time.time() - start) * 1000)
        return ToolResult(success=True,
                          output=f"匹配 {len(hits)} 处:\n" + "\n".join(hits[:100]),
                          metadata={"hits": len(hits)},
                          elapsed_ms=(time.time() - start) * 1000)