"""PromptBuilder —— 对应设计第 4 节「Prompt 动态拼接」。

System Prompt(角色/规则) + Tool Descriptions(JSON Schema)
+ 激活技能 + 检索记忆 + 任务上下文 + 压缩后的轨迹。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from jinja2 import Environment, BaseLoader

from agent.config import LLMConfig, LLMProvider
from agent.core.task import Task
from agent.prompt import templates


# CJK 统一表意文字/扩展 A、假名、谚文、CJK 标点、全角形式
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)


def estimate_tokens(text) -> int:
    """粗略估算 token 数（中文约 1 字/token，英文约 1.3 词/token）。

    支持 str 或消息列表 [{"role": ..., "content": ...}]：列表自动拼接 content。
    对 CJK 按单字符计（约 1 token/字），其余保持 max(词数, 字符数//2) 的
    原估算，避免中文内容被低估一半导致压缩迟迟不触发（排查方案 2.4）。
    """
    if not isinstance(text, str):
        parts = []
        for msg in text or []:
            content = msg.get("content") if isinstance(msg, dict) else msg
            if content:
                parts.append(str(content))
        text = "".join(parts)
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = _CJK_RE.sub("", text)
    return cjk + max(len(rest.split()), len(rest) // 2)


class PromptBuilder:
    """动态拼接 Prompt；llm.provider 决定系统提示风格，llm.temperature 决定解析器严格度。

    决策点：
    - llm.provider == anthropic -> Anthropic XML 风格系统提示；
    - llm.temperature < 0.3 -> 解析器 strict 模式，否则 loose。
    """

    def __init__(self, tool_schemas: List[Dict[str, Any]],
                 system_template: Optional[str] = None,
                 user_template: Optional[str] = None,
                 llm_config: Optional[LLMConfig] = None,
                 decision_logger=None):
        self.tool_schemas = tool_schemas
        self.llm_config = llm_config
        self.decision_logger = decision_logger
        self.env = Environment(loader=BaseLoader(), trim_blocks=True, lstrip_blocks=True)
        self.env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)
        self.system_template = self.env.from_string(
            system_template or self._resolve_system_template()
        )
        self.user_template = self.env.from_string(user_template or templates.USER_TEMPLATE)
        self.memory_context = ""
        self.skill_context = ""
        self.resources_context = ""
        self.project_profile = ""
        self.project_state = ""
        self.exec_env = ""
        self.gate_notice = ""

    def _resolve_system_template(self) -> str:
        """按 llm.provider 选择系统提示风格并记录决策。"""
        provider = None
        if self.llm_config is not None:
            provider = self.llm_config.provider
        if provider == LLMProvider.ANTHROPIC:
            if self.decision_logger is not None:
                self.decision_logger.record(
                    "system_prompt_style", "llm.provider", provider.value,
                    "使用 Anthropic XML 风格提示",
                )
            return templates.SYSTEM_TEMPLATE_ANTHROPIC
        if provider is not None:
            if self.decision_logger is not None:
                self.decision_logger.record(
                    "system_prompt_style", "llm.provider", provider.value,
                    "使用 OpenAI Markdown 风格提示",
                )
        return templates.SYSTEM_TEMPLATE

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

    def set_exec_env(self, context: str) -> None:
        """注入实际执行环境（Windows PowerShell / Linux bash），避免 shell 语法误用。"""
        self.exec_env = context

    def set_gate_notice(self, context: str) -> None:
        """注入阶段门禁约束提示（phase-barrier）。"""
        self.gate_notice = context

    def set_project_profile(self, context: str) -> None:
        """注入项目约定与技术栈摘要（阶段一 1.3）。"""
        self.project_profile = context

    def set_project_state(self, context: str) -> None:
        """注入上次会话以来的项目变化与未完成任务（主线一 1.1/1.2）。"""
        self.project_state = context

    def build(self, task: Task, upstream: List[Task] = None) -> List[Dict[str, str]]:
        # 决策点：temperature 决定解析器宽松度
        if self.llm_config is not None and self.decision_logger is not None:
            strictness = "strict" if self.llm_config.temperature < 0.3 else "loose"
            self.decision_logger.record(
                "parser_strictness", "llm.temperature",
                self.llm_config.temperature,
                f"解析器模式: {strictness}",
            )
        system = self.system_template.render(
            tools=self.tool_schemas,
            memory=self.memory_context,
            skill=self.skill_context,
            mcp_resources=self.resources_context,
            project_profile=self.project_profile,
            project_state=self.project_state,
            exec_env=self.exec_env,
            gate_notice=self.gate_notice,
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
