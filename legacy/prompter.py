"""Prompt 构建器——拼接 System Prompt + 上下文 + 工具描述 + 记忆注入"""
import json
from typing import List


class Prompter:
    """构建发送给 LLM 的完整 Prompt"""

    SYSTEM_BASE = """你是一个智能编程助手 Agent。你可以使用以下工具来完成任务：

{tools_desc}

请按以下格式响应：
- 如果需要使用工具，输出 JSON: {"tool": "tool_name", "params": {...}}
- 如果是最终答案，输出: {"final_answer": "你的回答"}
- 如果是思考，输出: {"think": "你的思考内容"}

请始终用中文回复用户。"""

    def __init__(self, tools: list, system_prompt: str = None):
        self.tools = tools
        self.system_prompt = system_prompt or self.SYSTEM_BASE
        self.memory_context = ""  # 第一关：记忆注入
        self.skill_context = ""   # 第四关：技能注入
        self.compressed_context = ""  # 第五关：压缩上下文

    def build(self, user_prompt: str, history: List[dict] = None,
              current_step: dict = None, extra_context: str = "") -> str:
        """构建完整 Prompt"""
        tools_desc = "\n".join(
            f"- {t.name}: {t.description}" for t in self.tools
        )

        # 使用 replace 而非 format()，避免自定义 prompt 中含未转义 {} 时抛 KeyError
        parts = [
            self.system_prompt.replace("{tools_desc}", tools_desc),
        ]

        # 注入技能上下文（第四关）
        if self.skill_context:
            parts.append(f"\n## 当前激活的技能模块\n{self.skill_context}")

        # 注入记忆上下文（第一关）
        if self.memory_context:
            parts.append(f"\n## 历史记忆摘要\n{self.memory_context}")

        # 注入压缩上下文（第五关）
        if self.compressed_context:
            parts.append(f"\n## 历史压缩摘要\n{self.compressed_context}")

        # 对话历史
        if history:
            parts.append("\n## 对话历史")
            for h in history[-10:]:  # 最多保留 10 轮
                parts.append(f"User: {h.get('user', '')}")
                parts.append(f"Assistant: {h.get('assistant', '')}")

        # 当前任务
        if current_step:
            parts.append(f"\n## 当前任务\n步骤 {current_step.get('step_id')}: {current_step.get('description')}")
            parts.append(f"参数: {json.dumps(current_step.get('params', {}), ensure_ascii=False)}")

        # 额外上下文
        if extra_context:
            parts.append(f"\n## 额外信息\n{extra_context}")

        # 用户指令
        parts.append(f"\n## 用户指令\n{user_prompt}")

        parts.append("\n请以 JSON 格式响应。")

        return "\n".join(parts)

    def set_memory(self, context: str):
        """注入记忆上下文（第一关）"""
        self.memory_context = context

    def set_skill(self, context: str):
        """注入技能上下文（第四关）"""
        self.skill_context = context

    def set_compressed(self, context: str):
        """注入压缩上下文（第五关）"""
        self.compressed_context = context

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """粗略估算 Token 数（中文 1 字≈1 token，英文 1 词≈1.3 token）"""
        words = len(text.split())
        chars = len(text)
        return max(words, chars // 2)