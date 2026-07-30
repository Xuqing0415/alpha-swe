"""第二关：Planner Agent——只输出 JSON 计划，不执行具体代码"""
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger("alpha-swe.planner")

PLANNER_SYSTEM_PROMPT = """你是一个任务规划器（Planner Agent）。你的职责是：
1. 分析用户需求，将复杂任务拆解为可执行的子任务
2. 输出 JSON 格式的计划，不执行任何具体操作
3. 当 Executor 执行失败时，分析原因并给出纠正方案

输出格式:
{
  "plan": [
    {"action": "terminal_execute", "description": "...", "params": {"command": "..."}},
    {"action": "file_ops", "description": "...", "params": {"action": "read", "path": "..."}}
  ],
  "reasoning": "整体策略说明"
}

纠正格式（当 Executor 失败时）:
{
  "correction": {
    "original_action": "...",
    "failure_reason": "...",
    "new_action": {"action": "...", "description": "...", "params": {...}}
  }
}"""


class PlannerAgent:
    """规划器 Agent——只负责拆解和纠正，不执行"""

    def __init__(self, llm_call=None):
        self.llm_call = llm_call or self._default_plan
        self.system_prompt = PLANNER_SYSTEM_PROMPT
        self.plan_history: List[dict] = []

    def plan(self, user_prompt: str) -> List[dict]:
        """拆解用户指令为执行计划"""
        logger.info(f"[Planner] 开始规划: {user_prompt[:100]}")

        plan = self.llm_call(user_prompt)
        self.plan_history.append({
            "prompt": user_prompt,
            "plan": plan,
            "timestamp": __import__('datetime').datetime.now().isoformat()
        })

        logger.info(f"[Planner] 规划结果: {len(plan)} 个任务")
        return plan

    def correct(self, failed_task: dict, result: dict) -> Optional[dict]:
        """根据 Executor 的失败结果，生成纠正方案"""
        error = result.get("error", "未知错误")
        logger.warning(f"[Planner] 检测到执行失败: {failed_task} -> {error}")

        # 智能纠正策略
        action = failed_task.get("action", "")
        params = failed_task.get("params", {})

        if action == "file_ops":
            # 文件不存在 -> 尝试 search
            if "不存在" in str(error) or "No such file" in str(error):
                path = params.get("path", "")
                logger.info(f"[Planner] 文件不存在，切换为搜索模式: {path}")
                return {
                    "action": "terminal_execute",
                    "description": f"搜索文件（原路径: {path}）",
                    "params": {"command": f"find . -name '{path.split('/')[-1]}' -type f 2>/dev/null"}
                }

        if action == "terminal_execute":
            cmd = params.get("command", "")
            # 权限不足 -> 尝试 sudo 替代
            if "Permission denied" in str(error):
                logger.info(f"[Planner] 权限不足，尝试替代方案")
                return {
                    "action": "terminal_execute",
                    "description": f"替代命令（原命令: {cmd}）",
                    "params": {"command": f"find . -type f -readable 2>/dev/null | head -20"}
                }

        return None

    def _default_plan(self, user_prompt: str) -> List[dict]:
        """默认规划器（无 LLM 时使用）"""
        plan = []

        if "read" in user_prompt.lower() or "读取" in user_prompt:
            # 提取文件模式
            import re
            patterns = re.findall(r'\.\w+', user_prompt)
            ext = patterns[0] if patterns else ".txt"
            plan.append({
                "action": "terminal_execute",
                "description": f"搜索{ext}文件",
                "params": {"command": f"find . -name '*{ext}' -type f 2>/dev/null | head -20"}
            })

        if "console.log" in user_prompt.lower() or "console" in user_prompt.lower():
            plan.append({
                "action": "terminal_execute",
                "description": "搜索 console.log 调用",
                "params": {"command": "grep -rn 'console\\.log' . --include='*.js' --exclude-dir=node_modules 2>/dev/null | head -50"}
            })

        if "report" in user_prompt.lower() or "生成" in user_prompt:
            plan.append({
                "action": "file_ops",
                "description": "生成报告文件",
                "params": {"action": "write", "path": "report.txt", "content": ""}
            })

        if not plan:
            plan.append({
                "action": "terminal_execute",
                "description": "探索工作目录",
                "params": {"command": "ls -la && find . -type f -name '*.py' -o -name '*.js' | head -20"}
            })

        return plan