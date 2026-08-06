"""上下文管理器：插件/技能匹配注入 + 轨迹自动压缩。

压缩策略（对应设计第 11 节，基础版）:
1. 过长的工具输出截断为「摘要 + 原始输出存档」；
2. 早于 keep_recent 轮的历史替换为阶段摘要。
"""
from __future__ import annotations

import logging
from pathlib import Path
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
    """上下文管理：token 阈值触发压缩，支持 summary / vector_retrieval 两种压缩方法。

    对应设计 11 节：max_tokens * compression_threshold 作为触发线；
    压缩方法由 context.compression_method 驱动并在决策日志中记录。
    """

    def __init__(self, keep_recent_rounds: int = 3,
                 token_threshold: float = 0.8,
                 max_token_limit: int = 100_000,
                 skills_dir: str = "./skills",
                 max_tokens: Optional[int] = None,
                 compression_threshold: Optional[float] = None,
                 compression_method: str = "summary",
                 decision_logger=None,
                 active_skills: Optional[List[str]] = None,
                 active_plugins: Optional[List[str]] = None):
        self.keep_recent_rounds = keep_recent_rounds
        # 新配置（ContextConfig）优先；旧参数保持兼容
        self.token_threshold = (
            compression_threshold
            if compression_threshold is not None else token_threshold
        )
        self.max_token_limit = max_tokens if max_tokens is not None else max_token_limit
        self.compression_method = compression_method
        self.skills_dir = skills_dir
        self.compression_count = 0
        self.decision_logger = decision_logger
        # 白名单：非空时只激活列出的技能/插件（对应配置 active_skills / active_plugins）
        self.active_skills = list(active_skills or [])
        self.active_plugins_config = list(active_plugins or [])

    def active_plugins(self, instruction: str) -> List[Dict[str, str]]:
        """按关键词匹配激活插件/技能；active_skills 非空时按名称白名单过滤。"""
        matched: List[Dict[str, str]] = []
        text = instruction.lower()
        for rule in SKILL_RULES:
            if self.active_skills and rule["name"] not in self.active_skills:
                continue
            if any(k in text for k in rule["keywords"]):
                matched.append({"name": rule["name"], "content": rule["content"]})
        return matched

    def _load_plugin_files(self) -> List[str]:
        """读取 active_plugins 指向的上下文文件（相对路径基于 ./plugins/）。"""
        if not self.active_plugins_config:
            return []
        base = Path(self.skills_dir).parent / "plugins"
        blocks = []
        for name in self.active_plugins_config:
            p = Path(name)
            if not p.is_absolute():
                p = base / name
            if p.is_file():
                blocks.append(
                    f"[plugin:{name}]\n" +
                    p.read_text(encoding="utf-8", errors="replace")[:4000]
                )
        return blocks

    def build_skill_context(self, instruction: str) -> str:
        parts = [f"[{p['name']}] {p['content']}"
                 for p in self.active_plugins(instruction)]
        parts.extend(self._load_plugin_files())
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill_activation", "active_skills", self.active_skills,
                f"激活技能/插件: {[p['name'] for p in self.active_plugins(instruction)] + self.active_plugins_config}",
            )
        return "\n".join(parts)

    def should_compact(self, history: List[Dict[str, Any]]) -> bool:
        total = sum(estimate_tokens(str(h.get("content", ""))) for h in history)
        threshold = int(self.max_token_limit * self.token_threshold)
        if total > threshold:
            if self.decision_logger is not None:
                self.decision_logger.record(
                    "trigger_compression", "context.compression_threshold",
                    self.token_threshold,
                    f"触发压缩: {total} > {threshold} tokens",
                )
            return True
        return False

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
        logger.info("上下文压缩 #%d: %d 轮 -> %s", self.compression_count,
                    len(old), self.compression_method)
        history[:] = recent
        if self.decision_logger is not None:
            self.decision_logger.record(
                "compression_method", "context.compression_method",
                self.compression_method,
                f"使用 {self.compression_method} 式压缩（归档 {len(old)} 轮）",
            )
        if self.compression_method == "vector_retrieval":
            # 向量检索式：归档内容按条目摘要，供后续向量化检索
            return f"[已归档(可向量检索) {self.compression_count}: {summary[:600]}]"
        return f"[Previous summary: {summary}]"