"""工具统一接口 —— 对应设计第 6 节。

    class Tool(ABC):
        name / description / parameters(JSON Schema)
        async def execute(self, params, context) -> ToolResult
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ExecutionContext:
    """工具执行上下文：沙箱根目录、任务信息、环境变量与中断信号。"""
    workspace: str = "."
    task_id: Optional[str] = None
    instruction: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    interrupt_event: Optional[object] = None  # asyncio.Event


@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool
    output: str = ""
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
            "elapsed_ms": self.elapsed_ms,
        }


class Tool(ABC):
    """所有工具的基类。"""
    name: str = "base"
    description: str = ""
    parameters: Dict[str, Any] = {}  # JSON Schema

    @abstractmethod
    async def execute(self, params: Dict[str, Any], context: ExecutionContext) -> ToolResult:
        raise NotImplementedError

    def to_schema(self) -> Dict[str, Any]:
        """返回给 LLM 的 JSON Schema 描述。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }