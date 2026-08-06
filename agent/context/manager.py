"""上下文管理器：插件/技能匹配注入 + 轨迹自动压缩。

压缩策略（对应设计第 11 节，基础版）:
1. 过长的工具输出截断为「摘要 + 原始输出存档」；
2. 早于 keep_recent 轮的历史替换为阶段摘要。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.prompt.builder import estimate_tokens

# 工具输出压缩：提取错误/警告/关键状态行（LLM 摘要的轻量替代，保真度高）
KEY_LINE_PATTERNS = [
    re.compile(r"(?i)(error|traceback|exception|failed|failure|fatal|killed|timeout)"),
    re.compile(r"(?i)(warning|warn|deprecat)"),
    re.compile(r"(?i)(assert|expected|actual|got|exit\s*code|status\s*[:=]|pass|ok\b)"),
]

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
                 active_plugins: Optional[List[str]] = None,
                 archive_dir: str = "./logs/archives",
                 output_truncate: int = 2000,
                 light_threshold: float = 0.8,
                 medium_threshold: float = 0.9,
                 heavy_threshold: float = 1.05):
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
        self.archive_dir = archive_dir
        self.output_truncate = max(200, output_truncate)
        self.light_threshold = light_threshold
        self.medium_threshold = medium_threshold
        self.heavy_threshold = heavy_threshold
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

    # ---- 分级压缩（对应设计 11 节：light / medium / heavy） ----
    def _compression_level(self, total_tokens: int) -> str:
        """按 token 压力分级：light 只压工具输出；medium 压旧对话保留决策点；heavy 递归摘要。"""
        pressure = total_tokens / max(1, self.max_token_limit)
        if pressure >= self.heavy_threshold:
            return "heavy"
        if pressure >= self.medium_threshold:
            return "medium"
        return "light"

    def _extract_key_lines(self, output: str, max_lines: int = 12) -> List[str]:
        """用正则提取错误/警告/关键状态行，保证不丢失精确行号等细节。"""
        lines = [ln.strip() for ln in str(output).splitlines() if ln.strip()]
        key_lines: List[str] = []
        seen = set()
        for ln in lines:
            lowered = ln.lower()
            hit = (
                any(p.search(ln) for p in KEY_LINE_PATTERNS)
                or any(k in lowered for k in (
                    "error", "exception", "traceback", "failed", "fatal",
                    "warning", "deprecat", "assert", "exit code", "killed",
                    "timeout", "panic",
                ))
            )
            if hit and ln not in seen:
                seen.add(ln)
                key_lines.append(ln[:300])
            if len(key_lines) >= max_lines:
                break
        return key_lines

    def _archive_output(self, content: str, index: int) -> str:
        """长输出存档到文件，返回可引用的相对路径。"""
        try:
            d = Path(self.archive_dir)
            d.mkdir(parents=True, exist_ok=True)
            rel = f"ctx_archive_{self.compression_count:03d}_{index:03d}.txt"
            (d / rel).write_text(str(content), encoding="utf-8", errors="replace")
            return str(Path(self.archive_dir) / rel)
        except OSError as e:
            logger.warning("输出存档失败: %s", e)
            return ""

    def _record_compression(self, before: int, after: int, level: str,
                            dropped_ids: List[str]) -> None:
        """增强压缩决策日志：级别、前后 token、丢弃消息 ID。"""
        if self.decision_logger is None:
            return
        self.decision_logger.record(
            "compression_level", "context.compression_threshold", level,
            f"压缩级别: {level}（{before} -> {after} tokens）",
        )
        self.decision_logger.record(
            "compression_method", "context.compression_method",
            self.compression_method,
            f"使用 {self.compression_method} 式压缩（归档 {len(dropped_ids)} 条消息）",
        )
        if dropped_ids:
            self.decision_logger.record(
                "compressed_message_ids", "context.archive_dir",
                str(self.archive_dir), f"丢弃消息: {dropped_ids[:20]}",
            )

    def compact(self, history: List[Dict[str, Any]]) -> str:
        """按压力分级压缩历史，返回阶段摘要文本；早于窗口的轨迹被替换。

        - light：只压缩长工具输出（关键行 + 存档引用）；
        - medium：压缩旧对话，保留 think/assistant/tool_call 等决策点；
        - heavy：递归摘要，全部历史压缩为简要摘要。
        """
        if len(history) <= self.keep_recent_rounds:
            return ""
        self.compression_count += 1
        before_tokens = sum(
            estimate_tokens(str(h.get("content", ""))) for h in history
        )
        old = history[: -self.keep_recent_rounds]
        recent = history[-self.keep_recent_rounds:]
        level = self._compression_level(before_tokens)

        parts: List[str] = []
        dropped_ids: List[str] = []
        archive_index = 0
        for i, h in enumerate(old):
            role = str(h.get("role", "?"))
            content = str(h.get("content", ""))
            long_output = role in ("observation", "tool_result") and \
                          len(content) > self.output_truncate
            if long_output:
                key_lines = self._extract_key_lines(content)
                ref = self._archive_output(content, archive_index)
                archive_index += 1
                excerpt = "；".join(key_lines[:8]) if key_lines else content[:200]
                if ref:
                    excerpt += f" [原始输出: {ref}]"
                else:
                    excerpt += " [原始输出过长，存档失败]"
                parts.append(f"observation: {excerpt}")
                dropped_ids.append(f"old#{i}(long-output)")
                continue
            if level == "light":
                # 轻度压力：只压缩长输出，其余消息完整保留
                parts.append(f"{role}: {content[:300]}")
                continue
            if level == "heavy":
                # 重度压力：递归摘要，所有历史压缩为一句
                parts.append(f"{role}: {content[:120]}")
                dropped_ids.append(f"old#{i}(heavy)")
                continue
            # medium：保留决策点（think/assistant/tool_call/user），压缩 observation
            if role in ("assistant", "think", "user", "tool_call", "system"):
                parts.append(f"{role}: {content[:200]}")
            else:
                parts.append(f"observation: {content[:120]}")
                dropped_ids.append(f"old#{i}")

        summary = "；".join(parts)
        after_tokens = sum(
            estimate_tokens(str(h.get("content", ""))) for h in recent
        )
        history[:] = recent
        self._record_compression(before_tokens, after_tokens, level, dropped_ids)
        logger.info("上下文压缩 #%d [%s]: %d -> %d tokens",
                    self.compression_count, level, before_tokens, after_tokens)
        if self.compression_method == "vector_retrieval":
            # 向量检索式：归档内容按条目摘要，供后续向量化检索
            return f"[已归档(可向量检索) {self.compression_count}: {summary[:600]}]"
        return f"[Previous summary: {summary}]"