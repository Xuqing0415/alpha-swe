"""上下文管理器：插件/技能匹配注入 + 轨迹自动压缩。

压缩策略（对应设计第 11 节，基础版）:
1. 过长的工具输出截断为「摘要 + 原始输出存档」；
2. 早于 keep_recent 轮的历史替换为阶段摘要。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from agent.prompt.builder import estimate_tokens

logger = logging.getLogger("alpha-swe.context")

# 技能触发关键词 -> 技能内容（示例：从 skills/ 目录读取的简化版）
SKILL_RULES: List[Dict[str, Any]] = [
    {
        "name": "python",
        "keywords": ["python", "py文件", "flask", "django"],
        "content": "- 优先使用类型标注；- 用 pytest 写测试；- 保持向后兼容。",
    },
    {
        "name": "react",
        "keywords": ["react", "组件", "tsx", "jsx"],
        "content": "- 组件命名 PascalCase；- 使用函数组件 + hooks；- 状态提升到最近公共父级。",
    },
]


class ContextManager:
    def __init__(self, keep_recent_rounds: int = 3,
                 token_threshold: float = 0.8,
                 max_token_limit: int = 100_000,
                 skills_dir: str = "./skills"):
        self.keep_recent_rounds = keep_recent_rounds
        self.token_threshold = token_threshold
        self.max_token_limit = max_token_limit
        self.skills_dir = skills_dir
        self.compression_count = 0

    def active_plugins(self, instruction: str) -> List[Dict[str, str]]:
        """按关键词匹配激活插件/技能（可扩展为任务元数据+文件类型匹配）。"""
        matched: List[Dict[str, str]] = []
        text = instruction.lower()
        for rule in SKILL_RULES:
            if any(k in text for k in rule["keywords"]):
                matched.append({"name": rule["name"], "content": rule["content"]})
        return matched

    def build_skill_context(self, instruction: str) -> str:
        plugins = self.active_plugins(instruction)
        if not plugins:
            return ""
        return "\n".join(
            f"[{p['name']}] {p['content']}" for p in plugins
        )

    def should_compact(self, history: List[Dict[str, Any]]) -> bool:
        total = sum(estimate_tokens(str(h.get("content", ""))) for h in history)
        return total > self.max_token_limit * self.token_threshold

    def compact(self, history: List[Dict[str, Any]]) -> str:
        """压缩历史，返回阶段摘要文本；早于窗口的轨迹被替换。"""
        if len(history) <= self.keep_recent_rounds:
            return ""
        self.compression_count += 1
        old = history[: -self.keep_recent_rounds]
        recent = history[-self.keep_recent_rounds:]

        # 长输出截断
        truncated = []
        for h in old:
            content = str(h.get("content", ""))
            if len(content) > 2000:
                truncated.append({"role": h.get("role"), "content": content[:600] + "\n...[已截断]..."})
            else:
                truncated.append(h)

        summary = "；".join(
            f"{h.get('role')}: {str(h.get('content', ''))[:150]}" for h in truncated
        )
        logger.info("上下文压缩 #%d: %d 轮 -> 摘要", self.compression_count, len(old))
        history[:] = recent
        return f"[Previous summary: {summary}]"