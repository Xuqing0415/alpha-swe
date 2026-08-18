# -*- coding: utf-8 -*-
"""依赖管理工具（方向二阶段三 3.3 / 阶段二 2.5）。

- ``report``：扫描清单并输出依赖总览；
- ``audit``：返回各生态的审计命令建议（不直接联网执行）。
"""
from __future__ import annotations

from typing import Any, Dict

from agent.tools.base import ExecutionContext, Tool, ToolResult


class DependencyTool(Tool):
    name = "dependency"
    description = ("依赖清单识别与审计。action: report|audit；"
                   "audit 只返回审计命令建议，不自动执行。")
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["report", "audit"],
                       "default": "report"},
            "path": {"type": "string",
                     "description": "项目目录（默认 workspace）"},
        },
        "required": ["action"],
    }

    def __init__(self, decision_logger=None):
        self.decision_logger = decision_logger

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        from agent.code.dependency_manager import (
            audit_command, dependency_report, detect_manifests)
        action = str(params.get("action") or "report")
        path = str(params.get("path") or context.workspace or ".")
        if self.decision_logger is not None:
            self.decision_logger.record(
                "dependency.%s" % action, "tools.dependency", True,
                f"path={path}")
        if action == "audit":
            managers = sorted({m.manager for m in detect_manifests(path)})
            if not managers:
                return ToolResult(success=True,
                                  output="未发现依赖清单，无需审计。")
            lines = ["按生态执行以下审计命令（需联网且相应工具已安装）："]
            for mgr in managers:
                cmd = audit_command(mgr)
                lines.append(f"- {mgr}: {' '.join(cmd) if cmd else '(无)'}")
            return ToolResult(success=True, output="\n".join(lines))
        report = dependency_report(path)
        return ToolResult(success=True, output=report["text"],
                          metadata={"total_deps": report["total_deps"],
                                    "manifests": len(report["manifests"])})
