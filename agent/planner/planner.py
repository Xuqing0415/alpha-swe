"""任务规划器 —— 将用户指令拆分为带依赖的任务 DAG。

对应设计第 3.3 节：LLM 生成子任务与依赖；失败/无 LLM 时回退为单任务。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from agent.core.task import Task, TaskDAG
from agent.llm import BaseLLM, MockLLM

logger = logging.getLogger("alpha-swe.planner")

PLAN_PROMPT = """你是一个任务规划器。请把下面的用户指令拆解为 1-8 个可执行子任务，
每个子任务必须能用 terminal_execute / file_ops 工具独立完成。

只输出 JSON 数组，不要输出其他内容，格式如下:
[
  {"instruction": "任务描述", "dependencies": ["前驱任务索引(可选，如 0)"], "priority": 0}
]

用户指令: {prompt}
项目上下文: {context}
"""


class Planner:
    def __init__(self, llm: Optional[BaseLLM] = None, max_tasks: int = 8):
        self.llm = llm or MockLLM()
        self.max_tasks = max_tasks

    async def plan(self, prompt: str, context: str = "") -> List[Task]:
        """返回规划出的任务列表（由调用方提交给 Scheduler）。"""
        try:
            raw = await self.llm.complete([
                {"role": "system", "content": "你是任务规划器，只输出 JSON。"},
                {"role": "user", "content": _render_plan_prompt(prompt, context)},
            ])
            tasks = self._parse_plan(raw, prompt)
            if tasks:
                logger.info("规划完成: %d 个子任务", len(tasks))
                return tasks
        except Exception as e:
            logger.warning("LLM 规划失败，回退单任务: %s", e)
        return [Task(id="t0", instruction=prompt)]

    def _parse_plan(self, raw: str, fallback_prompt: str) -> List[Task]:
        m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
        json_text = m.group(1) if m else re.search(r"\[[\s\S]*\]", raw)
        if not json_text:
            return []
        try:
            data = json.loads(json_text.group(0) if hasattr(json_text, "group") else json_text)
        except json.JSONDecodeError:
            return []

        tasks: List[Task] = []
        for i, item in enumerate(data[: self.max_tasks]):
            instruction = str(item.get("instruction", "")).strip()
            if not instruction:
                continue
            dep_indices = item.get("dependencies", [])
            deps = []
            for d in dep_indices:
                try:
                    idx = int(d)
                    if 0 <= idx < len(data) and idx != i:
                        deps.append(f"t{idx}")
                except (TypeError, ValueError):
                    pass
            tasks.append(Task(
                id=f"t{i}",
                instruction=instruction,
                dependencies=list(dict.fromkeys(deps)),
                priority=int(item.get("priority", 0)),
            ))
        return tasks

def _render_plan_prompt(prompt: str, context: str) -> str:
    """渲染规划提示，避免用户指令中的花括号触发 format() 错误。"""
    return PLAN_PROMPT.replace("{prompt}", prompt).replace("{context}", context)