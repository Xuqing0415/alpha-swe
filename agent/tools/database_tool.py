# -*- coding: utf-8 -*-
"""数据库查询工具（方向二阶段三 3.1）。

- 支持 SQLite（标准库，离线可测）、PostgreSQL（psycopg 可选）、MySQL（pymysql 可选）；
- 安全机制：默认只读；写操作需 ``read_only=false`` 且显式 ``confirm=true``；
- 查询超时、行数截断、结果以文本/JSON 返回；相对路径解析到 workspace 内。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any, Dict, List

from agent.tools.base import (ErrorCategory, ExecutionContext, Tool,
                              ToolResult)


class DatabaseTool(Tool):
    name = "database"
    description = ("执行数据库查询。engine: sqlite|postgres|mysql；"
                   "默认只读，写操作需 read_only=false 且 confirm=true。")
    parameters = {
        "type": "object",
        "properties": {
            "engine": {"type": "string", "enum": ["sqlite", "postgres", "mysql"],
                       "description": "数据库类型"},
            "path": {"type": "string",
                     "description": "SQLite 数据库路径（相对 workspace）"},
            "dsn": {"type": "string",
                    "description": "PostgreSQL/MySQL 连接串，如 "
                                   "postgresql://user:pass@host:5432/db"},
            "query": {"type": "string", "description": "SQL 语句"},
            "read_only": {"type": "boolean", "default": True,
                          "description": "false 表示允许写操作"},
            "confirm": {"type": "boolean", "default": False,
                        "description": "写操作必须显式确认"},
            "max_rows": {"type": "integer", "default": 50},
            "timeout": {"type": "number", "default": 15},
        },
        "required": ["query"],
    }

    def __init__(self, default_timeout: float = 15.0, max_rows: int = 100,
                 allow_write: bool = False, decision_logger=None):
        self.default_timeout = max(1.0, default_timeout)
        self.max_rows = max(1, max_rows)
        self.allow_write = allow_write
        self.decision_logger = decision_logger

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        engine = str(params.get("engine") or "sqlite").lower()
        query = str(params.get("query") or "").strip()
        read_only = bool(params.get("read_only", True))
        max_rows = int(params.get("max_rows") or self.max_rows)
        timeout = float(params.get("timeout") or self.default_timeout)

        if not query:
            return ToolResult(success=False, error="缺少 query",
                              error_category=ErrorCategory.PERMANENT)
        if not read_only:
            if not self.allow_write:
                return ToolResult(
                    success=False,
                    error="写操作被禁用（工具未开启 allow_write）",
                    error_category=ErrorCategory.PERMISSION)
            if params.get("confirm") is not True:
                return ToolResult(
                    success=False,
                    error="写操作需要 confirm=true 确认",
                    error_category=ErrorCategory.PERMISSION)
        if self.decision_logger is not None:
            self.decision_logger.record(
                "database.query", "tools.database", engine,
                f"read_only={read_only} {query[:80]}",
            )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._run_sync, engine, params, query,
                                  read_only, max_rows, context.workspace),
                timeout=timeout)
            return ToolResult(success=True, output=result["output"],
                              metadata=result.get("meta", {}))
        except asyncio.TimeoutError:
            return ToolResult(
                success=False, error=f"查询超时（{timeout:.0f}s）",
                error_category=ErrorCategory.TRANSIENT)
        except Exception as e:  # 数据库错误归为永久性，避免无谓重试
            return ToolResult(success=False,
                              error=f"数据库错误: {e}",
                              error_category=ErrorCategory.PERMANENT)

    # ---- 同步执行（在线程池中运行） ----
    def _run_sync(self, engine: str, params: Dict[str, Any], query: str,
                  read_only: bool, max_rows: int, workspace: str) -> Dict:
        if engine == "sqlite":
            return self._run_sqlite(params, query, read_only, max_rows,
                                    workspace)
        if engine == "postgres":
            return self._run_pg(params, query, max_rows)
        if engine == "mysql":
            return self._run_my(params, query, max_rows)
        raise ValueError(f"不支持的数据库引擎: {engine}")

    def _sqlite_path(self, params, workspace: str) -> str:
        raw = str(params.get("path") or "memory.db")
        if os.path.isabs(raw):
            return raw
        return os.path.normpath(os.path.join(workspace, raw))

    def _run_sqlite(self, params, query, read_only, max_rows,
                    workspace: str) -> Dict:
        path = self._sqlite_path(params, workspace)
        uri = f"file:{path}?mode=ro" if read_only else path
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query)
            if cur.description is None:  # 非查询语句
                conn.commit()
                return {"output": f"影响行数: {cur.rowcount}",
                        "meta": {"affected": cur.rowcount}}
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            cols = [d[0] for d in cur.description]
            text = _format_rows(cols, rows, truncated)
            return {"output": text,
                    "meta": {"columns": cols, "rows": len(rows),
                             "truncated": truncated}}
        finally:
            conn.close()

    def _run_pg(self, params, query, max_rows) -> Dict:
        import psycopg
        dsn = str(params.get("dsn") or "")
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                if cur.description is None:
                    return {"output": "执行成功"}
                cols = [d.name for d in cur.description]
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                return {"output": _format_rows(cols, rows[:max_rows],
                                               truncated),
                        "meta": {"columns": cols, "rows": len(rows),
                                 "truncated": truncated}}

    def _run_my(self, params, query, max_rows) -> Dict:
        import pymysql
        dsn = str(params.get("dsn") or "")
        if "://" in dsn:  # mysql://user:pass@host:port/db
            from urllib.parse import urlsplit
            u = urlsplit(dsn)
            kwargs = {"host": u.hostname or "localhost",
                      "port": u.port or 3306,
                      "user": u.username or "",
                      "password": u.password or "",
                      "database": u.path.lstrip("/") or "",
                      "connect_timeout": 10}
        else:
            raise ValueError("mysql 需要 dsn: mysql://user:pass@host:port/db")
        conn = pymysql.connect(**kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute(query)
                if cur.description is None:
                    conn.commit()
                    return {"output": "执行成功"}
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                return {"output": _format_rows(cols, rows[:max_rows],
                                               truncated),
                        "meta": {"columns": cols, "rows": len(rows),
                                 "truncated": truncated}}
        finally:
            conn.close()


def _format_rows(cols: List[str], rows, truncated: bool) -> str:
    lines = ["\t".join(cols)]
    for r in rows:
        lines.append("\t".join("" if v is None else str(v) for v in r))
    if truncated:
        lines.append(f"... 截断，仅显示前 {len(rows)} 行")
    return "\n".join(lines)
