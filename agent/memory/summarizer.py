"""经验摘要生成 —— 对应设计第 7.2 节「自动提取」。

任务完成后由 LLM 生成结构化摘要 {problem, steps, solution, outcome, key_files}；
LLM 缺失或失败时回退到规则提取，保证不阻塞主流程。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from agent.core.task import Task, TaskStatus
from agent.llm import BaseLLM

logger = logging.getLogger("alpha-swe.memory.summarizer")

FENCE_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
OBJECT_JSON = re.compile(r"\{[\s\S]*\}")

SUMMARY_PROMPT = """根据下面的 Agent 任务轨迹，生成一条可复用的工程经验摘要。
只输出 JSON，不要输出其他内容，格式:
{"problem": "问题描述", "steps": ["步骤1", ...], "solution": "最终解决方案",
 "outcome": "success|failed", "key_files": ["文件路径", ...]}

任务轨迹:
{trace}
"""


class ExperienceSummarizer:
    def __init__(self, llm: Optional[BaseLLM] = None,
                 max_trace_chars: int = 4000,
                 enabled: bool = True):
        self.llm = llm
        self.max_trace_chars = max_trace_chars
        self.enabled = enabled

    async def summarize_task(self, task: Task) -> Dict[str, Any]:
        """返回经验摘要 dict；LLM 失败时回退规则提取。"""
        if self.enabled and self.llm is not None:
            try:
                resp = await self.llm.complete([
                    {"role": "system", "content": "你是经验总结器，只输出 JSON。"},
                    {"role": "user", "content": SUMMARY_PROMPT.replace(
                        "{trace}", self._build_trace(task))},
                ])
                summary = self._parse_summary(resp)
                if summary:
                    return summary
            except Exception as e:
                logger.warning("LLM 经验摘要失败，回退规则提取: %s", e)
        return self._fallback(task)

    # ---- 内部 ----
    def _build_trace(self, task: Task) -> str:
        lines = [f"指令: {task.instruction}"]
        for h in task.history:
            content = str(h.get("content", ""))
            lines.append(f"{h.get('role')}: {content[:400]}")
        text = "\n".join(lines)
        return text[: self.max_trace_chars]

    def _parse_summary(self, resp: str) -> Optional[Dict[str, Any]]:
        data = None
        m = FENCE_JSON.search(resp)
        if m:
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        if data is None:
            m = OBJECT_JSON.search(resp)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
        if not isinstance(data, dict):
            return None
        problem = str(data.get("problem", "")).strip()
        solution = str(data.get("solution", "")).strip()
        if not problem and not solution:
            return None
        steps = data.get("steps")
        steps = [str(s) for s in steps] if isinstance(steps, list) else []
        key_files = data.get("key_files")
        key_files = [str(f) for f in key_files] if isinstance(key_files, list) else []
        return {
            "problem": problem or solution[:100],
            "steps": steps[:10],
            "solution": solution,
            "outcome": str(data.get("outcome", "success")),
            "key_files": key_files[:10],
        }

    def _fallback(self, task: Task) -> Dict[str, Any]:
        observations = [
            str(h.get("content", ""))[:200]
            for h in task.history if h.get("role") == "observation"
        ]
        return {
            "problem": task.instruction,
            "steps": observations[:8],
            "solution": str(task.result or "")[:500],
            "outcome": "success" if task.status == TaskStatus.COMPLETED else "failed",
            "key_files": [],
        }