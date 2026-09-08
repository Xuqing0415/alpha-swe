"""任务调度器——将用户指令拆解为可执行步骤"""
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

import platform_cmds


@dataclass
class TaskStep:
    """单个执行步骤"""
    step_id: int
    action: str  # e.g. "terminal_execute", "file_ops", "think"
    description: str
    params: dict = field(default_factory=dict)
    status: str = "pending"  # pending/running/done/failed
    result: Optional[str] = None
    error: Optional[str] = None


class TaskScheduler:
    """将自然语言指令拆解为 TaskStep 列表"""

    def __init__(self, llm_call=None):
        self.llm_call = llm_call or self._default_plan

    def plan(self, user_prompt: str, context: str = "") -> List[TaskStep]:
        """解析用户指令，返回步骤列表"""
        if self.llm_call:
            return self._llm_plan(user_prompt, context)
        return self._default_plan(user_prompt, context)

    def _llm_plan(self, user_prompt: str, context: str) -> List[TaskStep]:
        try:
            plan_prompt = f"""你是一个任务规划器。将以下用户指令拆解为 JSON 步骤列表。
每个步骤包含: step_id, action (terminal_execute/file_ops/think), description, params。

用户指令: {user_prompt}
上下文: {context}

请只输出 JSON 数组，不要其他内容。例如:
[{{"step_id": 1, "action": "terminal_execute", "description": "列出文件", "params": {{"command": "ls"}}}}]"""

            resp = self.llm_call(plan_prompt)
            return self._parse_plan(resp)
        except Exception:
            return self._default_plan(user_prompt, context)

    def _default_plan(self, user_prompt: str, context: str) -> List[TaskStep]:
        """基于关键词的简单拆解（无需 LLM）"""
        steps = []
        sid = 1

        # 检测 "src/ 下所有 .ts/.js 文件" 或 "找出 console.log" 模式
        if ("src" in user_prompt.lower() and (".ts" in user_prompt or ".js" in user_prompt)) or \
           ("console.log" in user_prompt.lower() or "console" in user_prompt.lower()):
            # 排除 node_modules
            cmd = platform_cmds.search_console_log(
                exclude_node_modules="node_modules" in user_prompt
            )
            if "find" in user_prompt.lower() or "搜索" in user_prompt or "找出" in user_prompt:
                steps.append(TaskStep(sid, "terminal_execute", "搜索 console.log", {"command": cmd}))
                sid += 1

        if "report" in user_prompt.lower() or "生成" in user_prompt:
            steps.append(TaskStep(sid, "file_ops", "生成报告", {
                "action": "write", "path": "report.txt",
                "content": "# Console.log 分析报告\n\n{{search_results}}"
            }))
            sid += 1

        # 通用搜索
        if not steps:
            if "find" in user_prompt.lower() or "搜索" in user_prompt or "找出" in user_prompt:
                steps.append(TaskStep(sid, "terminal_execute", "搜索文件",
                                      {"command": platform_cmds.list_all_files()}))
                sid += 1

        # 检测读取文件：仅当提示词中出现明确的文件路径时才生成读步骤，
        # 避免读取名为 "auto" 的无效文件
        if "read" in user_prompt.lower() or "读取" in user_prompt or "cat" in user_prompt.lower():
            path_match = re.search(
                r'([\w./\\-]+\.(?:py|ts|js|jsx|tsx|txt|yaml|yml|json|md|csv|ini|cfg|toml))',
                user_prompt
            )
            if path_match:
                steps.append(TaskStep(sid, "file_ops", "读取文件", {
                    "action": "read", "path": path_match.group(1)
                }))
                sid += 1

        # 检测写入文件
        if ("write" in user_prompt.lower() or "写入" in user_prompt or "生成" in user_prompt or "report" in user_prompt.lower()) and \
           not any(s.action == "file_ops" and s.params.get("action") == "write" for s in steps):
            steps.append(TaskStep(sid, "file_ops", "生成报告", {"action": "write", "path": "report.txt"}))
            sid += 1

        # 检测终端命令
        if "ls" in user_prompt.lower() or "list" in user_prompt.lower():
            steps.append(TaskStep(sid, "terminal_execute", "列出目录",
                                  {"command": platform_cmds.list_dir()}))
            sid += 1

        # 如果没有任何匹配，至少添加一个思考步骤
        if not steps:
            steps.append(TaskStep(sid, "think", "分析用户意图", {"user_prompt": user_prompt}))
            sid += 1
            steps.append(TaskStep(sid, "terminal_execute", "执行搜索",
                                  {"command": platform_cmds.find_files((".py",))}))

        return steps

    def _parse_plan(self, llm_response: str) -> List[TaskStep]:
        """从 LLM 响应中提取步骤列表"""
        # 尝试提取 JSON 数组
        json_match = re.search(r'\[[\s\S]*\]', llm_response)
        if json_match:
            data = json.loads(json_match.group(0))
            return [
                TaskStep(
                    step_id=item.get("step_id", i + 1),
                    action=item.get("action", "think"),
                    description=item.get("description", ""),
                    params=item.get("params", {})
                )
                for i, item in enumerate(data)
            ]
        return self._default_plan("", "")

    def to_json(self, steps: List[TaskStep]) -> str:
        return json.dumps([
            {"step_id": s.step_id, "action": s.action, "description": s.description,
             "params": s.params, "status": s.status}
            for s in steps
        ], ensure_ascii=False, indent=2)