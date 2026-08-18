"""测试运行工具 —— run_tests（方案 3.3：测试与验证闭环）。

统一接口执行项目测试套件，返回结构化失败分析（用例名/断言差异/堆栈），
覆盖率可选采集；Agent 可基于失败摘要进入「修复-重测」循环。
"""
from __future__ import annotations

from typing import Any, Dict

from agent.code.test_runner import run_tests
from agent.tools.base import ExecutionContext, Tool, ToolResult


class TestRunnerTool(Tool):
    name = "run_tests"
    description = ("运行项目测试套件并返回结构化失败分析"
                   "（支持 pytest/jest/go/unittest/npm，可选覆盖率）")
    parameters = {
        "type": "object",
        "properties": {
            "framework": {
                "type": "string",
                "enum": ["auto", "pytest", "jest", "go", "unittest", "npm"],
                "description": "测试框架，auto 时保守按 pytest",
            },
            "target": {
                "type": "string",
                "description": "测试目标（文件/目录/表达式），空 = 全量",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数，默认 300",
            },
            "coverage": {
                "type": "boolean",
                "description": "是否采集覆盖率（pytest-cov）",
            },
        },
        "required": [],
    }

    def __init__(self, default_timeout: float = 300.0, decision_logger=None):
        self.default_timeout = max(10.0, default_timeout)
        self.decision_logger = decision_logger

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        framework = str(params.get("framework") or "auto")
        target = str(params.get("target") or "").strip()
        timeout = float(params.get("timeout") or self.default_timeout)
        coverage = bool(params.get("coverage", False))
        result = await run_tests(
            framework, target, context.workspace,
            timeout=timeout, collect_coverage=coverage,
        )
        if self.decision_logger is not None:
            self.decision_logger.record(
                "test.run", "agent.test_framework", framework,
                f"{'通过' if result.success else '失败'}: "
                f"{len(result.failures)} 个失败，耗时 {result.duration_ms:.0f}ms"
                + (f"，覆盖率 {result.coverage}%"
                   if result.coverage is not None else ""),
            )
        cov = (f"，覆盖率 {result.coverage}%"
               if result.coverage is not None else "")
        if result.success:
            return ToolResult(
                success=True,
                output=f"测试通过（{result.framework}，"
                       f"{result.duration_ms:.0f}ms{cov}）",
                metadata={"framework": result.framework,
                          "coverage": result.coverage},
                elapsed_ms=result.duration_ms,
            )
        # 失败：输出结构化摘要 + 关键输出尾部，供 Agent 修复后重测
        tail_lines = result.output.splitlines()[-30:]
        out = (result.summary + "\n\n[输出尾部]\n"
               + "\n".join(tail_lines)[:3000])
        return ToolResult(
            success=False,
            output=out,
            metadata={"framework": result.framework,
                      "failures": [f.name for f in result.failures],
                      "coverage": result.coverage},
            elapsed_ms=result.duration_ms,
        )
