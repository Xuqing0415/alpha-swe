"""工具统一接口 —— 对应设计第 6 节。

    class Tool(ABC):
        name / description / parameters(JSON Schema)
        async def execute(self, params, context) -> ToolResult
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class ErrorCategory(str, Enum):
    """错误分类 —— 对应方案 4.1：每类错误映射不同的处理策略。

    TRANSIENT      临时性错误，建议重试（网络抖动、命令超时）
    PERMANENT      永久性错误，重试无效（语法错误、文件不存在）
    PERMISSION     权限不足（沙箱策略拦截、文件权限）
    RESOURCE       资源耗尽（内存超限、磁盘满）
    CONFIGURATION  配置错误（模型不可用、API Key 无效）
    USER_ABORT     用户中断
    UNKNOWN        未知错误，需要人工分析
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    PERMISSION = "permission"
    RESOURCE = "resource"
    CONFIGURATION = "configuration"
    USER_ABORT = "user_abort"
    UNKNOWN = "unknown"


@dataclass
class ExecutionContext:
    """工具执行上下文：沙箱根目录、任务信息、环境变量与中断信号。"""
    workspace: str = "."
    task_id: Optional[str] = None
    instruction: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    interrupt_event: Optional[object] = None  # asyncio.Event
    # 实时输出回调（TUI 终端窗格用）：收到一行 stdout/stderr 即调用
    output_callback: Optional[Callable[[str], None]] = None


@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool
    output: str = ""
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    error_category: ErrorCategory = ErrorCategory.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
            "elapsed_ms": self.elapsed_ms,
            "error_category": self.error_category.value,
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