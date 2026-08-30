"""phase-barrier 阶段门禁工具（编排器钩子模式，alpha-swe#1）。

Agent 通过该工具声明当前阶段 / 推进阶段；
未满足前置证据时，check / advance 返回约束提示回传 Agent。
"""
from __future__ import annotations

from typing import Any, Dict

from agent.tools.base import ErrorCategory, ExecutionContext, Tool, ToolResult


class PhaseBarrierGateTool(Tool):
    name = "phase_barrier_gate"
    description = (
        "阶段门禁（phase-barrier）：按标准工程流程推进"
        "（需求 -> spec -> 测试 -> 实现 -> 测试 -> 交付）。"
        "动作：inspect（查看门禁状态） / check（声明进入某阶段，必填 stage） / "
        "advance（完成当前阶段证据后推进，必填 stage） / "
        "record_test_run（记录一次测试运行结果） / "
        "verify（校验证据完整性）。"
        "未满足前置证据时会返回约束提示，"
        "请先补齐 spec.md 与测试用例再重试。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "check", "advance", "record_test_run", "verify"],
                "description": "门禁操作",
            },
            "stage": {
                "type": "integer",
                "description": "阶段号 0-6（check / advance 必填）",
            },
            "exit_code": {
                "type": "integer",
                "description": "record_test_run 的退出码（0 = 全部通过）",
            },
            "output": {
                "type": "string",
                "description": "record_test_run 的测试输出文本",
            },
        },
        "required": ["action"],
    }

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        action = str(params.get("action") or "inspect")
        try:
            if action == "inspect":
                data = self.bridge.inspect()
                return ToolResult(success=True, output=self._format_inspect(data),
                                  metadata=data)
            if action == "check":
                stage = self._to_stage(params)
                if stage is None:
                    return self._arg_error("check 操作需要整数 stage 参数（0-6）")
                data = self.bridge.check_stage(stage)
                meta = {k: data.get(k) for k in
                        ("allowed", "stage", "current_stage", "stage_name",
                         "violations", "skip")}
                message = str(data.get("message") or "")
                if data.get("skip"):
                    return ToolResult(success=True, output=message,
                                      metadata={**meta, "skipped": True})
                if not data.get("allowed"):
                    return ToolResult(success=False, error=message, output=message,
                                      metadata=meta,
                                      error_category=ErrorCategory.PERMISSION)
                return ToolResult(success=True, output=message, metadata=meta)
            if action == "advance":
                stage = self._to_stage(params)
                if stage is None:
                    return self._arg_error("advance 操作需要整数 stage 参数（0-6）")
                data = self.bridge.advance_stage(stage)
                meta = {k: data.get(k) for k in
                        ("success", "stage", "stage_name", "evidence", "skip")}
                message = str(data.get("message") or data.get("error") or "")
                if data.get("skip"):
                    return ToolResult(success=True, output=message,
                                      metadata={**meta, "skipped": True})
                if not data.get("success"):
                    return ToolResult(success=False, error=message, output=message,
                                      metadata=meta,
                                      error_category=ErrorCategory.PERMISSION)
                return ToolResult(success=True, output=message, metadata=meta)
            if action == "record_test_run":
                data = self.bridge.record_test_run({
                    "exit_code": int(params.get("exit_code", 0) or 0),
                    "output": str(params.get("output") or ""),
                })
                meta = {k: data.get(k) for k in ("exit_code", "passed", "summary", "skip")}
                return ToolResult(success=True,
                                  output=str(data.get("summary") or "已记录"),
                                  metadata=meta)
            if action == "verify":
                data = self.bridge.verify_evidence()
                ok = bool(data.get("ok"))
                message = ("证据完整" if ok
                           else "证据不完整：" +
                           "；".join(str(v) for v in (data.get("violations") or [])))
                meta = {k: data.get(k) for k in ("ok", "violations", "signed", "skip")}
                if data.get("skip"):
                    return ToolResult(success=True, output=message,
                                      metadata={**meta, "skipped": True})
                return ToolResult(success=ok, output=message,
                                  error=None if ok else message, metadata=meta)
            return self._arg_error("未知动作: " + action)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False,
                              error=f"phase_barrier_gate 执行异常: {exc}",
                              error_category=ErrorCategory.UNKNOWN)

    @staticmethod
    def _to_stage(params: Dict[str, Any]) -> Any:
        stage = params.get("stage")
        if isinstance(stage, bool):
            return None
        if isinstance(stage, int):
            return stage
        if isinstance(stage, str) and stage.strip().isdigit():
            return int(stage)
        return None

    @staticmethod
    def _arg_error(message: str) -> ToolResult:
        return ToolResult(success=False, error=message, output=message, metadata={},
                          error_category=ErrorCategory.CONFIGURATION)

    @staticmethod
    def _format_inspect(data: Dict[str, Any]) -> str:
        if data.get("skip"):
            return "门禁不可用（skip）"
        return (
            f"当前阶段: {data.get('current_stage')} {data.get('stage_name') or ''} | "
            f"已完成: {data.get('completed_stages')} | "
            f"已交付: {data.get('complete')}"
        )
