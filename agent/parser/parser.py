"""输出解析器 —— JSON 代码块优先，正则回退，Final Answer 单独处理。

对应设计第 5 节：解析失败时生成重试反馈并记录失败上下文。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.parser")

FENCE_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
OBJECT_JSON = re.compile(r"\{[\s\S]*\}")
TOOL_TEXT = re.compile(
    r"(?:Tool|工具)\s*:\s*(\w+)\s*(?:Input|输入)\s*:\s*([\s\S]+)", re.IGNORECASE
)


@dataclass
class ParsedAction:
    """解析后的动作。"""
    action_type: str  # tool_call | think | final_answer | error
    tool_name: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    content: str = ""
    raw: str = ""
    error: Optional[str] = None
    # 决策理由显式化（进阶 1.1）：Agent 解释为什么选择这个动作/方案
    reasoning: str = ""
    # 单次响应多工具调用（design 6 节）：tool_calls 列表中除第一个外的其余项
    extra_tool_calls: List[Dict[str, Any]] = field(default_factory=list)


class Parser:
    """输出解析器：mode="strict" 要求 JSON 结构，mode="loose" 允许纯文本回退。

    由 llm.temperature 驱动（<0.3 严格 / 其余宽松），并在决策日志中记录。
    """

    def __init__(self, max_retries: int = 3, mode: str = "loose",
                 decision_logger=None, require_reasoning: bool = False):
        self.max_retries = max_retries
        self.mode = mode
        self.decision_logger = decision_logger
        # 进阶 1.1：开启后强制 tool_call / final_answer 携带 reasoning
        # （为什么这样做，至少 10 个字符），缺失则拒绝并要求重试
        self.require_reasoning = require_reasoning
        self.failures: List[Dict[str, Any]] = []

    def parse(self, llm_output: str) -> ParsedAction:
        raw = llm_output.strip()
        if not raw:
            return ParsedAction(action_type="error", raw=raw, error="空响应")

        # 文本格式 "Tool: x\nInput: ..." 优先（Input 可能是 JSON 对象）
        m = TOOL_TEXT.search(raw)
        if m:
            action = ParsedAction(
                action_type="tool_call",
                tool_name=m.group(1).strip(),
                params=self._guess_params(m.group(2).strip()),
                raw=raw,
            )
            return self._check_reasoning(action, raw)

        # JSON 代码块 / 独立 JSON 对象
        data = self._extract_json_object(raw)
        if data is not None:
            return self._check_reasoning(self._from_object(data, raw), raw)

        # 兜底：正则抓 tool 名
        m = re.search(r'"tool"\s*:\s*"(\w+)"', raw)
        if m:
            return ParsedAction(action_type="tool_call", tool_name=m.group(1), raw=raw)

        if self.mode == "strict":
            # 严格模式：输出必须是可识别的 JSON/工具格式，否则报错重试
            return ParsedAction(
                action_type="error", raw=raw,
                error="严格模式：输出不是可识别的 JSON/工具格式",
            )
        return ParsedAction(action_type="final_answer", content=raw, raw=raw)

    def retry_feedback(self, action: ParsedAction, attempt: int) -> str:
        """生成反馈给 LLM，要求其修正输出格式。"""
        self.failures.append({"attempt": attempt, "raw": action.raw[:500], "error": action.error})
        logger.warning("解析失败第 %d 次: %s", attempt, action.error)
        return (
            f"你的上一条输出无法解析（错误: {action.error}）。"
            f"请只输出 JSON 代码块，并携带 reasoning 字段解释为什么这样做，"
            f"例如: ```json {{\"tool\": \"terminal_execute\", "
            f"\"params\": {{\"command\": \"dir\"}}, "
            f"\"reasoning\": \"先查看目录结构以定位问题\"}}``` "
            f"或 {{\"final_answer\": \"...\", \"reasoning\": \"...\"}}。"
        )

    # ---- 内部 ----
    @staticmethod
    def _reasoning_of(data: Dict[str, Any]) -> str:
        """从动作 JSON 中提取 reasoning（缺省为空串）。"""
        return str(data.get("reasoning", "")).strip()

    def _check_reasoning(self, action: ParsedAction, raw: str) -> ParsedAction:
        """require_reasoning 开启时，关键动作必须携带可解释的决策理由。"""
        if (self.require_reasoning
                and action.action_type in ("tool_call", "final_answer")
                and len(action.reasoning) < 10):
            return ParsedAction(
                action_type="error", raw=raw,
                error="缺少 reasoning 字段：每个动作需解释为什么这么做（至少 10 个字符）",
            )
        return action

    def _extract_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        m = FENCE_JSON.search(text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        m = OBJECT_JSON.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None

    def _from_object(self, data: Dict[str, Any], raw: str) -> ParsedAction:
        if "tool_calls" in data:
            calls = data["tool_calls"]
            if isinstance(calls, list) and calls and isinstance(calls[0], dict):
                first = calls[0]
                if "tool" in first:
                    params = first.get("params")
                    if not isinstance(params, dict):
                        params = {}
                    rest = [
                        c for c in calls[1:]
                        if isinstance(c, dict) and "tool" in c
                    ]
                    return ParsedAction(
                        action_type="tool_call",
                        tool_name=str(first["tool"]),
                        params=params,
                        extra_tool_calls=rest,
                        reasoning=self._reasoning_of(first),
                        raw=raw,
                    )
            return ParsedAction(
                action_type="error", raw=raw,
                error="tool_calls 字段格式错误",
            )
        if "tool" in data:
            params = data.get("params")
            if not isinstance(params, dict):
                params = {}
            action = ParsedAction(
                action_type="tool_call",
                tool_name=str(data["tool"]),
                params=params,
                reasoning=self._reasoning_of(data),
                raw=raw,
            )
            if "think" in data:
                # 模型把思考与工具调用写在同一个 JSON：保留思考文本，
                # 循环先展示思考再执行工具（避免思考被吞掉）。
                action.content = str(data["think"])
            return action
        if "think" in data:
            return ParsedAction(
                action_type="think", content=str(data["think"]),
                reasoning=self._reasoning_of(data), raw=raw)
        if "final_answer" in data:
            return ParsedAction(
                action_type="final_answer", content=str(data["final_answer"]),
                reasoning=self._reasoning_of(data), raw=raw)
        return ParsedAction(
            action_type="error",
            raw=raw,
            error=f"JSON 中缺少 tool/think/final_answer 字段: {list(data.keys())}",
        )

    @staticmethod
    def _guess_params(text: str) -> Dict[str, Any]:
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {"input": text}
        except json.JSONDecodeError:
            return {"input": text}
