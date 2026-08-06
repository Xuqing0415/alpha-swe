"""PromptBuilder —— 对应设计第 4 节「Prompt 动态拼接」。

System Prompt(角色/规则) + Tool Descriptions(JSON Schema)
+ 激活技能 + 检索记忆 + 任务上下文 + 压缩后的轨迹。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from jinja2 import Environment, BaseLoader

from agent.core.task import Task
from agent.prompt import templates


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1 字/token，英文约 1.3 词/token）。"""
    return max(len(text.split()), len(text) // 2)


class PromptBuilder:
    def __init__(self, tool_schemas: List[Dict[str, Any]],
                 system_template: Optional[str] = None,
                 user_template: Optional[str] = None):
        self.tool_schemas = tool_schemas
        self.env = Environment(loader=BaseLoader(), trim_blocks=True, lstrip_blocks=True)
        self.env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)
        self.system_template = self.env.from_string(system_template or templates.SYSTEM_TEMPLATE)
        self.user_template = self.env.from_string(user_template or templates.USER_TEMPLATE)
        self.memory_context = ""
        self.skill_context = ""
        self.resources_context = ""

    def set_memory(self, context: str) -> None:
        self.memory_context = context

    def set_skill(self, context: str) -> None:
        self.skill_context = context

    def update_tools(self, schemas: List[Dict[str, Any]]) -> None:
        """动态化工具描述（如按沙箱能力隐藏 curl/网络工具）。"""
        self.tool_schemas = schemas

    def set_resources(self, context: str) -> None:
        """注入 MCP 资源内容（外部知识库/文件/Schema）。"""
        self.resources_context = context

    def build(self, task: Task, upstream: List[Task] = None) -> List[Dict[str, str]]:
        system = self.system_template.render(
            tools=self.tool_schemas,
            memory=self.memory_context,
            skill=self.skill_context,
            mcp_resources=self.resources_context,
        )
        upstream_text = ""
        if upstream:
            lines = [f"- {t.instruction}: {t.result}" for t in upstream if t.result]
            upstream_text = "\n".join(lines)
        user = self.user_template.render(
            instruction=task.instruction,
            upstream=upstream_text,
            history=task.history,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def build_with_retry(self, task: Task, feedback: Optional[str] = None) -> List[Dict[str, str]]:
        """在轨迹中追加解析失败反馈，要求 LLM 重试（对应设计第 5 节）。"""
        messages = self.build(task)
        if feedback:
            messages.append({"role": "user", "content": feedback})
        return messages