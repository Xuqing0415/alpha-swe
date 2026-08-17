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

from agent.config import AppConfig, WorkerRoleConfig, load_team_config
from agent.core.decision_logger import DecisionLogger
from agent.core.scheduler import Scheduler
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import BaseLLM
from agent.multiagent.blackboard import Blackboard
from agent.multiagent.messages import Message, MsgType
from agent.multiagent.workers import WorkerAgent
from agent.selfimprove.capability import CapabilityProfile

logger = logging.getLogger("alpha-swe.multiagent.orchestrator")

def _load_role_configs() -> List[WorkerRoleConfig]:
    """从 config/team.yaml 加载角色库配置（动态角色分配的懒创建来源）。"""
    try:
        return list(load_team_config().roles)
    except Exception as e:
        logger.warning("角色库配置加载失败: %s", e)
        return []


# 主线二 2.1：动态角色分配——关键词路由优先级（LLM 规划失败/未给角色时的回退）
_ROLE_PRIORITY = ["reviewer", "tester", "security", "debugger",
                  "architect", "documenter", "ops", "coder"]
_DEFAULT_ROLE_KEYWORDS = {
    "reviewer": ["审查", "评审", "review", "check", "检查", "审阅", "代码规范"],
    "tester": ["测试", "test", "pytest", "jest", "coverage", "跑通"],
    "security": ["安全", "漏洞", "注入", "越权", "密钥", "security", "vuln"],
    "debugger": ["调试", "定位", "bug", "崩溃", "异常", "debug", "根因", "复现"],
    "architect": ["架构", "设计", "api", "接口设计", "重构方案", "architect"],
    "documenter": ["文档", "readme", "注释", "使用说明", "document"],
    "ops": ["部署", "ci", "构建", "环境配置", "docker", "deploy", "build"],
    "coder": ["实现", "编写", "修改", "implement", "write code"],
}

TEAM_PLAN_PROMPT = """你是多 Agent 团队规划器。可用的 Worker 角色（含职责）：
{roles}

可用角色能力画像（按历史表现，仅作参考）：
{capability}

把用户指令拆解为 1-8 个带角色的子任务，注意：
- 从上述角色库中为每个子任务选择最合适的 role，不限于 coder/reviewer/tester；
- reviewer 审查必须依赖对应的产出任务，tester 依赖被测任务；
- 每个子任务给出 role_rationale，说明为什么选该角色（角色需求显式化）；
- 有依赖关系的子任务用前驱任务索引（0 起）声明依赖。

只输出 JSON 数组，不要输出其他内容：
[
  {{"instruction": "任务描述", "role": "角色名", "role_rationale": "选择理由", "dependencies": [0], "priority": 0}}
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
    needs_intervention: bool = False  # 评审耗尽/无法仲裁时升级人工介入


class TeamPlanner:
    """LLM 生成带角色的子任务列表；失败回退为单个 coder 任务。"""

    def __init__(self, llm: BaseLLM, roles: List[str], max_tasks: int = 8,
                 role_descriptions: Optional[Dict[str, str]] = None,
                 capability_profiles: Optional[Dict[str, CapabilityProfile]] = None):
        self.llm = llm
        self.roles = roles
        self.max_tasks = max_tasks
        self.role_descriptions = role_descriptions or {}
        # 交叉集成：能力画像 x 角色分配——供提示注入与回退路由参考
        self.capability_profiles: Dict[str, CapabilityProfile] = \
            capability_profiles or {}

    @classmethod
    def roles_from_config(cls) -> List[str]:
        """从 config/team.yaml 加载全部角色名（动态角色分配的角色库）。"""
        try:
            return [r.name for r in load_team_config().roles]
        except Exception as e:
            logger.warning("角色库加载失败，使用默认三角色: %s", e)
            return ["coder", "reviewer", "tester"]

    async def plan(self, prompt: str) -> List[Task]:
        if self.role_descriptions:
            roles_text = "\n".join(
                f"- {name}: {self.role_descriptions.get(name, '')}" for name in self.roles)
        else:
            roles_text = " / ".join(self.roles)
        cap_hint = self._capability_hint()
        user_content = (TEAM_PLAN_PROMPT
            .replace("{roles}", roles_text)
            .replace("{capability}", cap_hint or "（暂无历史能力数据）")
            .replace("{prompt}", prompt))
        try:
            raw = await self.llm.complete([
                {"role": "system", "content": "你是多 Agent 团队规划器，只输出 JSON。"},
                {"role": "user", "content": user_content},
            ])
            tasks = self._parse(raw)
            if tasks:
                return tasks
        except Exception as e:
            logger.warning("团队规划失败，回退单任务: %s", e)
        # 回退：按指令分类路由（编码类 -> coder，审查类 -> reviewer，测试类 -> tester）
        return [Task(id="s0", instruction=prompt, role=self._classify_role(prompt))]

    def _capability_hint(self) -> str:
        """按角色历史能力画像生成紧凑提示（供 Planner 参考）。"""
        if not self.capability_profiles:
            return ""
        parts = []
        for role in self.roles:
            prof = self.capability_profiles.get(role)
            if prof is None:
                continue
            hint = prof.role_hint_text()
            if hint:
                parts.append(f"- {role}: {hint}")
        return "\n".join(parts)

    @classmethod
    def _role_keywords(cls) -> Dict[str, List[str]]:
        """角色关键词表：优先用 config/team.yaml 的 routing_keywords，缺省用内置默认。"""
        try:
            cfg_roles = {r.name: list(r.routing_keywords or [])
                         for r in load_team_config().roles}
        except Exception:
            cfg_roles = {}
        merged: Dict[str, List[str]] = {}
        for name, defaults in _DEFAULT_ROLE_KEYWORDS.items():
            merged[name] = list(cfg_roles.get(name) or defaults)
        return merged

    @classmethod
    def _classify_role(cls, instruction: str,
                       role_keywords: Optional[Dict[str, List[str]]] = None,
                       capability_scores: Optional[Dict[str, float]] = None) -> str:
        """基于关键词的规则路由：LLM 未给出角色/规划失败时的自动派发回退。

        按 _ROLE_PRIORITY 顺序匹配（reviewer > tester > security > ... > coder），
        避免"编写测试"被 coder 的"编写"关键词抢先；
        多个角色命中时若提供 capability_scores，则优先选能力分更高的角色
        （交叉集成：能力画像驱动角色分配）。
        """
        text = (instruction or "").lower()
        kws = role_keywords if role_keywords is not None else cls._role_keywords()
        matched = [
            role for role in _ROLE_PRIORITY
            if any(k in text for k in (kws.get(role)
                                       or _DEFAULT_ROLE_KEYWORDS.get(role, [])))
        ]
        if not matched:
            return "coder"
        if capability_scores:
            best = max(matched, key=lambda r: capability_scores.get(r, 0.0))
            if capability_scores.get(best, 0.0) > 0:
                return best
        return matched[0]

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
            role = str(item.get("role", "")).strip()
            if role not in self.roles:
                role = self._classify_role(instruction)
            deps = []
            for d in item.get("dependencies", []):
                try:
                    idx = int(d)
                    if 0 <= idx < len(data) and idx != i:
                        deps.append(f"s{idx}")
                except (TypeError, ValueError):
                    pass
            rationale = str(item.get("role_rationale", "")).strip()
            tasks.append(Task(
                id=f"s{i}",
                instruction=instruction,
                role=role,
                dependencies=list(dict.fromkeys(deps)),
                priority=int(item.get("priority", 0)),
                metadata={"role_rationale": rationale} if rationale else {},
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
        roles_config: Optional[List[WorkerRoleConfig]] = None,
        planner: Optional[TeamPlanner] = None,
        max_review_retries: Optional[int] = None,
        concurrency: Optional[int] = None,
        decision_logger: Optional[DecisionLogger] = None,
    ) -> None:
        self.config = config or AppConfig()
        self.llm = llm
        self.blackboard = blackboard or Blackboard()
        self.workers = workers or {}
        # 主线二 2.1：角色库（全部可用角色，含未实例化 Worker 的懒创建来源）
        self._role_map: Dict[str, WorkerRoleConfig] = {}
        for r in (roles_config
                  or self.config.team.roles
                  or _load_role_configs()):
            self._role_map[r.name] = r
        if not self._role_map:
            self._role_map["coder"] = WorkerRoleConfig(
                name="coder", tools=["terminal_execute", "file_ops"])
            self._role_map["reviewer"] = WorkerRoleConfig(
                name="reviewer", tools=["file_ops"], read_only=True)
        self.max_review_retries = (
            max_review_retries
            if max_review_retries is not None
            else self.config.team.max_review_retries
        )
        self.concurrency = concurrency or self.config.team.concurrency
        self.decision_logger = decision_logger or DecisionLogger(
            log_path=self.config.decision_log_path or None,
        )
        # 规划器默认使用完整角色库（而非仅已实例化 Worker），
        # 让 Planner 可以挑选 debugger/documenter/architect 等角色
        # 交叉集成：能力画像 x 角色分配——每个角色独立持久化画像
        self._role_profiles: Dict[str, CapabilityProfile] = {}
        try:
            self._role_profiles = {
                name: CapabilityProfile.for_role(
                    name, base_dir=self.config.agent.self_improve_dir)
                for name in self._role_map
            }
        except Exception as e:
            logger.warning("角色能力画像装配失败: %s", e)
        self.planner = planner or TeamPlanner(
            llm=llm,
            roles=list(self._role_map.keys()),
            role_descriptions={name: r.description
                               for name, r in self._role_map.items()},
            capability_profiles=self._role_profiles,
        )
        self._dag: Optional[TaskDAG] = None
        self._retries: Dict[str, int] = {}
        self.review_log: List[ReviewRecord] = []
        self.needs_intervention = False

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
        if task.role is not None:
            role = task.role
        else:
            # 能力感知路由：多个关键词角色命中时优先高能力分角色
            cap_scores = {
                name: prof.score_for_instruction(task.instruction)
                for name, prof in self._role_profiles.items()
            }
            role = TeamPlanner._classify_role(
                task.instruction, capability_scores=cap_scores)
            if cap_scores:
                self.decision_logger.record(
                    "role.capability", "team.roles", role,
                    f"关键词路由命中多角色，按能力分选 {role}"
                    f"（{cap_scores.get(role, 0.0):.2f}）",
                )
        worker = self.workers.get(role)
        if worker is None:
            role_cfg = self._role_map.get(role)
            if role_cfg is not None:
                # 动态角色分配：角色在库中但未预实例化 -> 按角色配置懒创建 Worker
                worker = WorkerAgent(
                    role_cfg, config=self.config, llm=self.llm,
                    blackboard=self.blackboard)
                self.workers[role] = worker
                self.decision_logger.record(
                    "role.routing", "team.roles", role,
                    f"角色 {role} 未预配置，已按角色库自动实例化 Worker",
                )
            else:
                # 自动路由回退：角色不在库中时按指令分类重定向
                fallback_role = TeamPlanner._classify_role(task.instruction)
                worker = self.workers.get(fallback_role)
                if worker is None:
                    task.mark(TaskStatus.FAILED, error=f"未配置角色: {role}")
                    return
                self.decision_logger.record(
                    "role.routing", "team.roles", role,
                    f"角色 {role} 未配置，按指令分类回退到 {fallback_role}",
                )
                role = fallback_role
        if worker.decision_logger is None:
            worker.decision_logger = self.decision_logger
        self.blackboard.post(Message(
            sender="orchestrator", receiver=role, type=MsgType.TASK_ASSIGN,
            payload={"task_id": task.id, "instruction": task.instruction},
            priority=task.priority,
            timeout=self.config.team.message_timeout,
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
            # 仲裁上限耗尽：升级为人工介入（标记 + 决策日志），任务按失败收尾
            self.needs_intervention = True
            self.decision_logger.record(
                "review.exhausted", "team.max_review_retries",
                self.max_review_retries,
                f"评审 {retries + 1} 轮未通过（{coder.id if coder else ''}），"
                f"升级人工介入: {suggestion[:80]}",
            )
            task.mark(TaskStatus.FAILED,
                      error=f"评审未通过（{retries + 1} 轮），需人工介入: {suggestion}")

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
            needs_intervention=self.needs_intervention,
        )


__all__ = ["OrchestratorAgent", "TeamPlanner", "TeamResult", "ReviewRecord"]
