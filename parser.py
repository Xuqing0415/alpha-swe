"""输出解析器——从 LLM 响应中提取结构化操作 + 实体提取"""
import json
import re
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ParsedAction:
    """解析后的动作"""
    action_type: str  # "tool_call" | "final_answer" | "think" | "error"
    tool_name: Optional[str] = None
    params: dict = field(default_factory=dict)
    content: str = ""
    raw: str = ""
    # 从工具输出中提取的实体
    entities: List[dict] = field(default_factory=list)


class Parser:
    """解析 LLM 输出，提取工具调用或最终答案"""

    # 占位符标记（第五关：压缩摘要）
    COMPRESSED_PLACEHOLDER = "[COMPRESSED_SUMMARY]"

    # 实体提取规则（轻量级，不调用 LLM）
    ENTITY_RULES = [
        # 文件路径提取
        (re.compile(r'(?:^|\s)(\.?/?[\w./-]+\.\w{1,5})\b', re.MULTILINE), "file"),
        # 类名提取
        (re.compile(r'class\s+(\w+)'), "class"),
        # 函数名提取
        (re.compile(r'def\s+(\w+)'), "function"),
        # import 模块提取
        (re.compile(r'(?:import|from)\s+(\w+)'), "module"),
        # 错误类型提取
        (re.compile(r'(\w+Error)\b'), "error_type"),
        # find/ls 输出中的路径
        (re.compile(r'^\.?/([\w/.-]+)$', re.MULTILINE), "file"),
    ]

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

    def extract_entities(self, text: str, max_per_type: int = 5) -> List[dict]:
        """从工具输出中提取实体（轻量级规则，不调用 LLM）"""
        entities = []
        seen = set()
        type_counts = {}

        for pattern, etype in self.ENTITY_RULES:
            matches = pattern.findall(text)
            for m in matches:
                if isinstance(m, tuple):
                    m = m[0]
                m = m.strip()
                if m and m not in seen and len(m) < 200:
                    seen.add(m)
                    entities.append({"type": etype, "name": m})
                    type_counts[etype] = type_counts.get(etype, 0) + 1
                    if type_counts[etype] >= max_per_type:
                        break

        return entities

    def parse_tool_output(self, tool_name: str, output: str) -> List[dict]:
        """根据工具类型智能提取实体"""
        entities = self.extract_entities(output)

        # 针对特定工具做优化
        if tool_name == "terminal_execute":
            if "find" in output.lower() or "ls" in output.lower():
                # 提取文件列表中的每一行
                lines = output.strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith(".") and "." in line:
                        entities.append({"type": "file", "name": line[:100]})

        elif tool_name == "file_ops":
            # 代码文件，提取更多语言结构
            extra = self.extract_entities(output, max_per_type=10)
            entities.extend(extra)

        return entities

    def _extract_json(self, text: str) -> Optional[str]:
        """提取 JSON 块"""
        # 尝试匹配 ```json ... ``` 代码块
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if m:
            return m.group(1).strip()

        # 尝试匹配 { ... }（括号配对，避免贪婪匹配跨多个 JSON 对象）
        start = text.find("{")
        if start != -1:
            depth = 0
            in_str = False
            escaped = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1].strip()
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