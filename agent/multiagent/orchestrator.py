"""Orchestrator Agent —— 对应设计第 8 节：主控、规划、派发、仲裁、汇总。

- 用 LLM 生成带角色的子任务 DAG（TeamPlanner）；
- 复用 Scheduler 按依赖/优先级调度，并发派发给各 Worker；
- Reviewer 返回 retry 时触发仲裁：带评审反馈重建 coder 任务并复审（最多 N 轮）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.core.decision_logger import DecisionLogger
from agent.core.scheduler import Scheduler
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import BaseLLM
from agent.multiagent.blackboard import Blackboard
from agent.multiagent.messages import Message, MsgType
from agent.multiagent.workers import WorkerAgent

logger = logging.getLogger("alpha-swe.multiagent.orchestrator")

TEAM_PLAN_PROMPT = """你是多 Agent 团队规划器。可用的 Worker 角色：{roles}。
把用户指令拆解为 1-8 个带角色的子任务，注意：
- coder 负责实现，reviewer 负责审查（必须依赖对应 coder 任务），tester 负责测试；
- 有依赖关系的子任务用前驱任务索引（0 起）声明依赖。

只输出 JSON 数组，不要输出其他内容：
[
  {{"instruction": "任务描述", "role": "coder|reviewer|tester", "dependencies": [0], "priority": 0}}
]

用户指令: {prompt}
"""


@dataclass
class ReviewRecord:
    """一次评审记录（用于仲裁与最终报告）。"""
    coder_task_id: str
    reviewer_task_id: str
    verdict: str
    suggestion: str = ""
    round: int = 0


@dataclass
class TeamResult:
    """团队会话最终结果。"""
    ok: bool
    final_answer: str = ""
    subtasks: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    review_log: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    blackboard_summary: Dict[str, Any] = field(default_factory=dict)


class TeamPlanner:
    """LLM 生成带角色的子任务列表；失败回退为单个 coder 任务。"""

    def __init__(self, llm: BaseLLM, roles: List[str], max_tasks: int = 8):
        self.llm = llm
        self.roles = roles
        self.max_tasks = max_tasks

    async def plan(self, prompt: str) -> List[Task]:
        roles_text = " / ".join(self.roles)
        try:
            raw = await self.llm.complete([
                {"role": "system", "content": "你是多 Agent 团队规划器，只输出 JSON。"},
                {"role": "user", "content": TEAM_PLAN_PROMPT
                 .replace("{roles}", roles_text).replace("{prompt}", prompt)},
            ])
            tasks = self._parse(raw)
            if tasks:
                return tasks
        except Exception as e:
            logger.warning("团队规划失败，回退单任务: %s", e)
        return [Task(id="s0", instruction=prompt, role="coder")]

    def _parse(self, raw: str) -> List[Task]:
        m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
        json_text = m.group(1) if m else re.search(r"\[[\s\S]*\]", raw)
        if not json_text:
            return []
        try:
            text = json_text.group(0) if hasattr(json_text, "group") else json_text
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        tasks: List[Task] = []
        for i, item in enumerate(data[: self.max_tasks]):
            instruction = str(item.get("instruction", "")).strip()
            if not instruction:
                continue
            role = str(item.get("role", "coder")).strip()
            if role not in self.roles:
                role = "coder"
            deps = []
            for d in item.get("dependencies", []):
                try:
                    idx = int(d)
                    if 0 <= idx < len(data) and idx != i:
                        deps.append(f"s{idx}")
                except (TypeError, ValueError):
                    pass
            tasks.append(Task(
                id=f"s{i}",
                instruction=instruction,
                role=role,
                dependencies=list(dict.fromkeys(deps)),
                priority=int(item.get("priority", 0)),
            ))
        return tasks


class OrchestratorAgent:
    """团队主控：规划 → 调度 → 派发 → 仲裁 → 汇总。"""

    def __init__(
        self,
        *,
        config: Optional[AppConfig] = None,
        llm: Optional[BaseLLM] = None,
        blackboard: Optional[Blackboard] = None,
        workers: Optional[Dict[str, WorkerAgent]] = None,
        planner: Optional[TeamPlanner] = None,
        max_review_retries: Optional[int] = None,
        concurrency: Optional[int] = None,
        decision_logger: Optional[DecisionLogger] = None,
    ) -> None:
        self.config = config or AppConfig()
        self.blackboard = blackboard or Blackboard()
        self.workers = workers or {}
        self.max_review_retries = (
            max_review_retries
            if max_review_retries is not None
            else self.config.team.max_review_retries
        )
        self.concurrency = concurrency or self.config.team.concurrency
        self.decision_logger = decision_logger or DecisionLogger(
            log_path=self.config.decision_log_path or None,
        )
        self.planner = planner or TeamPlanner(
            llm=llm, roles=list(self.workers.keys())
        )
        self._dag: Optional[TaskDAG] = None
        self._retries: Dict[str, int] = {}
        self.review_log: List[ReviewRecord] = []

    # ---- 主入口 ----
    async def run(self, prompt: str) -> TeamResult:
        subtasks = await self.planner.plan(prompt)
        self._dag = TaskDAG()
        for st in subtasks:
            self._dag.add(st)
        for st in subtasks:
            if self._dag.dependencies_satisfied(st):
                st.mark(TaskStatus.READY)

        scheduler = Scheduler(dag=self._dag, max_concurrency=self.concurrency)
        scheduler.set_worker(self._dispatch)
        try:
            await scheduler.run_to_completion()
        except Exception:
            logger.exception("团队调度异常")

        return self._finalize(prompt, subtasks)

    # ---- 派发 ----
    async def _dispatch(self, task: Task) -> None:
        role = task.role or "coder"
        worker = self.workers.get(role)
        if worker is None:
            task.mark(TaskStatus.FAILED, error=f"未配置角色: {role}")
            return
        if worker.decision_logger is None:
            worker.decision_logger = self.decision_logger
        self.blackboard.post(Message(
            sender="orchestrator", receiver=role, type=MsgType.TASK_ASSIGN,
            payload={"task_id": task.id, "instruction": task.instruction},
        ))
        if role == "reviewer":
            await self._run_reviewer(task, worker)
            return
        extra = self._upstream_artifacts(task)
        result = await worker.execute_task(task, extra_context=extra)
        self.blackboard.post(Message(
            sender=role, receiver="orchestrator", type=MsgType.TASK_RESULT,
            payload={"task_id": task.id, "ok": result.ok,
                     "output": result.output[:500]},
        ))
        if result.ok:
            task.mark(TaskStatus.COMPLETED, result=result.output)
        else:
            task.mark(TaskStatus.FAILED,
                      error=result.error or f"{role} 任务执行失败")

    async def _run_reviewer(self, task: Task, worker: WorkerAgent) -> None:
        coder = self._find_reviewed_coder(task)
        extra = self._artifact_text(coder) if coder else ""
        result = await worker.execute_task(task, extra_context=extra)
        verdict, suggestion = self._parse_verdict(result.output)
        root_id = self._root_coder_id(coder)
        review_round = self._retries.get(root_id, 0) if root_id else 0
        self.review_log.append(ReviewRecord(
            coder_task_id=coder.id if coder else "",
            reviewer_task_id=task.id,
            verdict=verdict,
            suggestion=suggestion,
            round=review_round,
        ))
        self.blackboard.post(Message(
            sender="reviewer", receiver="orchestrator", type=MsgType.REVIEW_RESULT,
            payload={"task_id": task.id, "verdict": verdict,
                     "suggestion": suggestion[:300]},
        ))
        if verdict == "pass" or coder is None:
            task.mark(TaskStatus.COMPLETED, result=result.output)
            return
        # 仲裁：retry -> 重建 coder + reviewer（计数锚定根 coder，避免新任务绕过上限）
        retries = self._retries.get(root_id, 0) if root_id else 0
        if retries < self.max_review_retries:
            self._retries[root_id] = retries + 1
            self._spawn_retry_pair(coder, suggestion, root_id=root_id)
            task.mark(TaskStatus.COMPLETED, result=result.output)  # 本轮评审完成
        else:
            task.mark(TaskStatus.FAILED,
                      error=f"评审未通过（{retries + 1} 轮）: {suggestion}")

    def _spawn_retry_pair(self, coder: Task, suggestion: str, root_id: str = "") -> None:
        assert self._dag is not None
        new_coder = self._dag.create_task(
            instruction=coder.instruction + f"\n[评审反馈] {suggestion}",
            role="coder",
            parent_id=coder.id,
        )
        new_coder.mark(TaskStatus.READY)
        self._dag.create_task(
            instruction=f"复审任务 {new_coder.id} 的产出",
            role="reviewer",
            dependencies=[new_coder.id],
        )
        logger.info("评审 retry: %s -> %s（第 %d 轮）",
                    coder.id, new_coder.id, self._retries.get(root_id, 1))

    def _root_coder_id(self, task: Optional[Task]) -> str:
        """沿 parent_id 链向上找到原始 coder 任务，作为重试计数的锚点。"""
        if task is None or self._dag is None:
            return ""
        root, cur, seen = task.id, task, set()
        while cur.parent_id and cur.id not in seen:
            seen.add(cur.id)
            parent = self._dag.get(cur.parent_id)
            if parent is None:
                break
            cur = parent
            root = cur.id
        return root

    # ---- 辅助 ----
    def _find_reviewed_coder(self, reviewer: Task) -> Optional[Task]:
        assert self._dag is not None
        for dep_id in reviewer.dependencies:
            dep = self._dag.get(dep_id)
            if dep is not None and dep.role == "coder":
                return dep
        return None

    def _upstream_artifacts(self, task: Task) -> str:
        """把上游任务的产出（文件/报告）作为上下文给当前任务。"""
        parts = []
        for dep_id in task.dependencies:
            text = self._artifact_text(self._dag.get(dep_id))
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _artifact_text(self, task: Optional[Task]) -> str:
        if task is None:
            return ""
        artifact = self.blackboard.get(f"task:{task.id}")
        if not artifact:
            return ""
        lines = [f"## 上游任务 {task.id} 的产出"]
        for path, content in (artifact.get("files") or {}).items():
            lines.append(f"### {path}\n{content[:800]}")
        if artifact.get("output"):
            lines.append(f"输出: {artifact['output'][:400]}")
        return "\n".join(lines)

    @staticmethod
    def _parse_verdict(output: str) -> tuple[str, str]:
        """从评审 Agent 输出中解析 verdict；解析失败时按 pass 处理（fail-open）。"""
        if not output:
            return "pass", ""
        m = re.search(r"\{[\s\S]*\}", output)
        if not m:
            return "pass", ""
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return "pass", ""
        verdict = str(data.get("verdict", "pass")).strip().lower()
        if verdict not in ("pass", "retry"):
            verdict = "pass"
        return verdict, str(data.get("suggestion", "")).strip()

    # ---- 汇总 ----
    def _finalize(self, prompt: str, subtasks: List[Task]) -> TeamResult:
        assert self._dag is not None
        all_tasks = self._dag.all()
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]

        ok = not failed
        if completed:
            answers = [t.result for t in completed
                       if isinstance(t.result, str) and t.result and t.role != "reviewer"]
            final = "\n".join(answers[:3])
            if not final and ok:
                final = "团队任务完成（无文本结果）"
            if failed:
                final = (final + "\n\n未通过环节: "
                         + "; ".join(t.error or t.instruction for t in failed[:3]))
        else:
            final = f"（团队未产生完成结果）: {prompt}"
            ok = False

        return TeamResult(
            ok=ok,
            final_answer=final,
            subtasks=[t.to_dict() for t in all_tasks],
            artifacts=self.blackboard.artifacts(),
            review_log=[r.__dict__ for r in self.review_log],
            messages=[m.to_dict() for m in self.blackboard.messages()],
            blackboard_summary=self.blackboard.summary(),
        )


__all__ = ["OrchestratorAgent", "TeamPlanner", "TeamResult", "ReviewRecord"]
