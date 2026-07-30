"""第二关：Executor Agent——只负责根据 Planner 指令执行具体操作"""
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger("alpha-swe.executor_agent")

EXECUTOR_SYSTEM_PROMPT = """你是一个执行器（Executor Agent）。你的职责是：
1. 接收 Planner 的任务指令，精确执行
2. 不要自行规划或修改任务范围
3. 执行完毕后返回结果（成功/失败 + 输出）

输入格式: Planner 的任务 JSON
输出格式: {"success": true/false, "output": "...", "error": "..."}"""


class ExecutorAgent:
    """执行器 Agent——只负责执行，不负责规划"""

    def __init__(self, executor):
        self.executor = executor  # 实际工具执行器
        self.system_prompt = EXECUTOR_SYSTEM_PROMPT
        self.execution_history: list = []

    def execute(self, task: dict) -> dict:
        """执行 Planner 分配的任务"""
        action = task.get("action", "")
        params = task.get("params", {})
        description = task.get("description", "")

        logger.info(f"[Executor] 执行: {description} ({action})")

        result = self.executor.execute(action, params)

        execution_record = {
            "task": task,
            "success": result.success,
            "output": result.output[:500] if result.output else "",
            "error": result.error,
            "elapsed_ms": result.elapsed_ms
        }
        self.execution_history.append(execution_record)

        if result.success:
            logger.info(f"[Executor] 成功: {result.output[:100]}")
        else:
            logger.error(f"[Executor] 失败: {result.error}")

        return execution_record

    def get_history(self) -> list:
        """获取执行历史"""
        return self.execution_history

    def get_stats(self) -> dict:
        """获取执行统计"""
        total = len(self.execution_history)
        success = sum(1 for r in self.execution_history if r["success"])
        return {
            "total": total,
            "success": success,
            "failed": total - success,
            "success_rate": f"{success / total * 100:.1f}%" if total > 0 else "0%"
        }