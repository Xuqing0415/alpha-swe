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

from agent.tools.base import ExecutionContext, Tool, ToolResult

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

    async def execute(self, params: Dict[str, Any], context: ExecutionContext) -> ToolResult:
        action = str(params.get("action", ""))
        path = str(params.get("path", ""))
        start = time.time()

        if not path:
            return ToolResult(success=False, error="缺少 path 参数", elapsed_ms=0.0)
        if TRAVERSAL_PATTERN.search(path):
            return ToolResult(success=False, error=f"禁止路径穿越: {path}", elapsed_ms=0.0)

        try:
            target = resolve_workspace_path(context.workspace, path)
        except PermissionError as e:
            return ToolResult(success=False, error=str(e), elapsed_ms=0.0)

        try:
            if action == "read":
                return await self._read(target, start)
            if action == "write":
                return await self._write(target, params.get("content", ""), start)
            if action == "append":
                return await self._append(target, params.get("content", ""), start)
            if action == "search":
                return await self._search(target, params.get("pattern", ""), start)
            return ToolResult(success=False, error=f"未知操作: {action}",
                              elapsed_ms=(time.time() - start) * 1000)
        except Exception as e:
            return ToolResult(success=False, error=str(e),
                              elapsed_ms=(time.time() - start) * 1000)

    async def _read(self, target: Path, start: float) -> ToolResult:
        if not await asyncio.to_thread(target.exists):
            return ToolResult(success=False, error=f"文件不存在: {target}",
                              elapsed_ms=(time.time() - start) * 1000)
        data = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="ignore")
        return ToolResult(success=True, output=data, metadata={"path": str(target), "size": len(data)},
                          elapsed_ms=(time.time() - start) * 1000)

    async def _write(self, target: Path, content: str, start: float) -> ToolResult:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        return ToolResult(success=True, output=f"写入成功: {target}",
                          metadata={"path": str(target), "size": len(content)},
                          elapsed_ms=(time.time() - start) * 1000)

    async def _append(self, target: Path, content: str, start: float) -> ToolResult:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(success=True, output=f"追加成功: {target}",
                          metadata={"path": str(target)},
                          elapsed_ms=(time.time() - start) * 1000)

    async def _search(self, target: Path, pattern: str, start: float) -> ToolResult:
        if not pattern:
            return ToolResult(success=False, error="search 需要 pattern 参数",
                              elapsed_ms=(time.time() - start) * 1000)
        if not await asyncio.to_thread(target.exists):
            return ToolResult(success=False, error=f"路径不存在: {target}",
                              elapsed_ms=(time.time() - start) * 1000)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(success=False, error=f"正则无效: {e}",
                              elapsed_ms=(time.time() - start) * 1000)

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