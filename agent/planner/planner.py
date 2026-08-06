"""任务规划器 —— 将用户指令拆分为带依赖的任务 DAG。

对应设计第 3.3 节：LLM 生成子任务与依赖；失败/无 LLM 时回退为单任务。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from agent.config import PlannerConfig
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
    """任务规划器：PlannerConfig 驱动拆分行为。

    决策点：
    - split_threshold_complexity：复杂度低于阈值时不拆分（记录 skip_split）；
    - max_subtasks：LLM 子任务数超出时截断（记录 truncate_subtasks）；
    - allow_parallel=False：强制子任务串行（记录 force_sequential）。
    """

    def __init__(self, llm: Optional[BaseLLM] = None, max_tasks: int = 8,
                 config: Optional[PlannerConfig] = None,
                 decision_logger=None):
        self.llm = llm or MockLLM()
        self.max_tasks = max_tasks
        self.config = config or PlannerConfig()
        self.decision_logger = decision_logger

    async def plan(self, prompt: str, context: str = "") -> List[Task]:
        """返回规划出的任务列表（由调用方提交给 Scheduler）。"""
        complexity = self._estimate_complexity(prompt)
        if complexity < self.config.split_threshold_complexity:
            if self.decision_logger is not None:
                self.decision_logger.record(
                    "skip_split", "planner.split_threshold_complexity",
                    self.config.split_threshold_complexity,
                    f"复杂度 {complexity:.2f} < {self.config.split_threshold_complexity}，不拆分",
                )
            return [Task(id="t0", instruction=prompt)]
        try:
            raw = await self.llm.complete([
                {"role": "system", "content": "你是任务规划器，只输出 JSON。"},
                {"role": "user", "content": _render_plan_prompt(prompt, context)},
            ])
            tasks = self._parse_plan(raw, prompt)
            if tasks:
                logger.info("规划完成: %d 个子任务", len(tasks))
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "execute_split", "planner.split_threshold_complexity",
                        self.config.split_threshold_complexity,
                        f"复杂度 {complexity:.2f}，执行拆分",
                    )
                if len(tasks) > self.config.max_subtasks:
                    if self.decision_logger is not None:
                        self.decision_logger.record(
                            "truncate_subtasks", "planner.max_subtasks",
                            self.config.max_subtasks,
                            f"子任务从 {len(tasks)} 截断为 {self.config.max_subtasks}",
                        )
                    tasks = tasks[: self.config.max_subtasks]
                if not self.config.allow_parallel and len(tasks) > 1:
                    # 强制串行：为每个后续任务追加前驱依赖
                    for i in range(1, len(tasks)):
                        tasks[i].dependencies.append(tasks[i - 1].id)
                    if self.decision_logger is not None:
                        self.decision_logger.record(
                            "force_sequential", "planner.allow_parallel",
                            self.config.allow_parallel,
                            f"强制 {len(tasks)} 个子任务串行执行",
                        )
                return tasks
        except Exception as e:
            logger.warning("LLM 规划失败，回退单任务: %s", e)
        return [Task(id="t0", instruction=prompt)]

    @staticmethod
    def _estimate_complexity(instruction: str) -> float:
        """规则化复杂度估计（0~1）：长度 + 句子/并列结构 + 多步骤关键词。"""
        text = (instruction or "").strip()
        if not text:
            return 0.0
        score = min(0.5, len(text) / 120.0)
        score += min(0.25, (text.count("，") + text.count(",")
                            + text.count("。") + text.count(".") + text.count(";")) * 0.05)
        triggers = ["同时", "并且", "然后", "分别", "多个", "以及", "多个文件",
                    "重构", "refactor", "multiple", "同时处理"]
        score += min(0.25, sum(1 for t in triggers if t in text) * 0.06)
        return round(min(1.0, score), 3)

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