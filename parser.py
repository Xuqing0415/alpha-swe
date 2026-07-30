"""输出解析器——从 LLM 响应中提取结构化操作"""
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class ParsedAction:
    """解析后的动作"""
    action_type: str  # "tool_call" | "final_answer" | "think" | "error"
    tool_name: Optional[str] = None
    params: dict = field(default_factory=dict)
    content: str = ""
    raw: str = ""


class Parser:
    """解析 LLM 输出，提取工具调用或最终答案"""

    # 占位符标记（第五关：压缩摘要）
    COMPRESSED_PLACEHOLDER = "[COMPRESSED_SUMMARY]"

    def parse(self, llm_output: str) -> ParsedAction:
        """解析 LLM 输出"""
        raw = llm_output.strip()

        # 尝试提取 JSON 块
        json_str = self._extract_json(raw)
        if json_str:
            try:
                data = json.loads(json_str)
                return self._parse_json(data, raw)
            except json.JSONDecodeError:
                pass

        # 尝试从文本中提取 tool 调用
        tool_match = re.search(r'"tool"\s*:\s*"(\w+)"', raw)
        if tool_match:
            return ParsedAction(
                action_type="tool_call",
                tool_name=tool_match.group(1),
                params=self._extract_params(raw),
                raw=raw
            )

        # 默认当作最终答案
        return ParsedAction(
            action_type="final_answer",
            content=raw,
            raw=raw
        )

    def _extract_json(self, text: str) -> Optional[str]:
        """提取 JSON 块"""
        # 尝试匹配 ```json ... ``` 代码块
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if m:
            return m.group(1).strip()

        # 尝试匹配 { ... }
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return m.group(0).strip()

        return None

    def _parse_json(self, data: dict, raw: str) -> ParsedAction:
        """解析 JSON 数据"""
        if "tool" in data:
            return ParsedAction(
                action_type="tool_call",
                tool_name=data["tool"],
                params=data.get("params", {}),
                raw=raw
            )
        elif "final_answer" in data:
            content = data["final_answer"]
            # 检查是否包含压缩占位符
            if self.COMPRESSED_PLACEHOLDER in content:
                content = content.replace(
                    self.COMPRESSED_PLACEHOLDER,
                    "[此处为历史上下文压缩摘要，已保留关键信息]"
                )
            return ParsedAction(
                action_type="final_answer",
                content=content,
                raw=raw
            )
        elif "think" in data:
            return ParsedAction(
                action_type="think",
                content=data["think"],
                raw=raw
            )
        else:
            return ParsedAction(
                action_type="error",
                content=f"未知 JSON 格式: {data}",
                raw=raw
            )

    def _extract_params(self, text: str) -> dict:
        """从文本中提取 params"""
        m = re.search(r'"params"\s*:\s*(\{[^}]+\})', text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        return {}