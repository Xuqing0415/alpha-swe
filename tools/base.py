"""基准工具基类"""
from dataclasses import dataclass, field
from typing import Optional, Any
import time


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0


class BaseTool:
    """所有工具的基类"""
    name: str = "base"
    description: str = ""

    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def to_schema(self) -> dict:
        """返回工具的 JSON Schema 描述"""
        return {
            "name": self.name,
            "description": self.description,
        }