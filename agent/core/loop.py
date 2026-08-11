"""AgentLoop —— 异步 ReAct 主循环。

对应设计第 2 节：
- 会话级状态机 IDLE->PLANNING->READY->RUNNING->WAITING->COMPLETED/FAILED；
- 每个任务执行 Think -> Act -> Observe -> Parse 的内部循环；
- 每次 Observe 后检查中断信号（yield 控制点），支持用户注入高优先级任务；
- 与 Scheduler 配合实现 DAG 依赖调度与并发。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.config import AppConfig, load_config, load_mcp_config
from agent.context.manager import ContextManager
from agent.context.plugin import PluginManager, ProjectContext
from agent.context.skill import SkillLibrary
from agent.core.decision_logger import DecisionLogger
from agent.core.scheduler import Scheduler
from agent.core.state import AgentPhase, StateMachine
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import BaseLLM, build_llm
from agent.mcp.manager import MCPManager
from agent.observability import MetricsRegistry, SessionArchive, Tracer
from agent.memory.factory import build_memory
from agent.memory.store import (MemoryStore, NoopMemoryStore,
                                 classify_task_type, format_experience_text)
from agent.memory.summarizer import ExperienceSummarizer
from agent.parser.parser import Parser
from agent.planner.planner import Planner
from agent.prompt.builder import PromptBuilder, estimate_tokens
from agent.sandbox.audit import FileAuditStore
from agent.sandbox.docker_sandbox import DockerSandbox
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext, ToolResult
from agent.tools.fileio import FileIOTool
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool

logger = logging.getLogger("alpha-swe.loop")

OBSERVATION_TRUNCATE = 2000


class TaskInterrupted(Exception):
    """任务被用户中断（yield 控制点抛出）。"""


@dataclass
class LoopResult:
    final_answer: str = ""
    phase: AgentPhase = AgentPhase.FAILED
    tasks: List[Task] = field(default_factory=list)
    total_rounds: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.phase == AgentPhase.COMPLETED


class AgentLoop:
    def __init__(
        self,
        config: Optional[AppConfig] = None,
        llm: Optional[BaseLLM] = None,
        tools: Optional[ToolManager] = None,
        planner: Optional[Planner] = None,
        parser: Optional[Parser] = None,
        prompt_builder: Optional[PromptBuilder] = None,
        context_manager: Optional[ContextManager] = None,
        memory: Optional[MemoryStore] = None,
        sandbox: Optional[SandboxPolicy] = None,
        scheduler: Optional[Scheduler] = None,
        summarizer: Optional[ExperienceSummarizer] = None,
        mcp_manager: Optional[MCPManager] = None,
        output_callback: Optional[Callable[[str], None]] = None,
        decision_logger: Optional[DecisionLogger] = None,
        confirmation_callback: Optional[
            Callable[[str, Dict[str, Any], Optional[str]], Any]
        ] = None,
        plugin_manager: Optional[PluginManager] = None,
        skill_library: Optional[SkillLibrary] = None,
        docker_sandbox: Optional[DockerSandbox] = None,
    ):
        self.config = config or load_config()
        self.state = StateMachine()
        self.cancel_event = asyncio.Event()
        self.events: List[Dict[str, Any]] = []
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._output_callback = output_callback
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 默认运行态；pause() 后清空
        self._decision = decision_logger or DecisionLogger(
            log_path=self.config.decision_log_path or None,
        )
        self.confirmation_callback = confirmation_callback
        self._max_rounds = (
            self.config.agent.max_loop_iterations or self.config.agent.max_rounds
        )
        # 阶段七：可观测性装配（分布式追踪 / 实时指标 / 会话档案）
        self.tracer = Tracer(
            self.config.agent.trace_dir,
            self.config.agent.trace_enabled,
            self._decision,
        )
        self.metrics = MetricsRegistry(self.config.agent.metrics_enabled)
        self.archive = SessionArchive(
            self.config.agent.session_archive_dir,
            self.config.agent.archive_enabled,
        )
        self._approve_rules: set[str] = set()  # “批准所有同类操作”的规则集合
        self._decision.record(
            "trace_enabled", "agent.trace_enabled",
            self.config.agent.trace_enabled,
            f"分布式追踪: {'开启' if self.config.agent.trace_enabled else '关闭'}",
        )
        self._decision.record(
            "archive_enabled", "agent.archive_enabled",
            self.config.agent.archive_enabled,
            f"会话档案: {'开启' if self.config.agent.archive_enabled else '关闭'}"
            f"（目录 {self.config.agent.session_archive_dir}）",
        )

        # 组件装配（缺省即用默认实现，便于测试注入）
        self.llm = llm or build_llm(self.config.llm)
        self.sandbox = sandbox or SandboxPolicy(
            workspace=self.config.sandbox.workspace,
            allowed_paths=self.config.sandbox.allowed_paths,
            blocked_paths=self.config.sandbox.blocked_paths,
            block_commands=self.config.sandbox.block_commands,
            network_enabled=self.config.sandbox.is_network_enabled,
            decision_logger=self._decision,
            network_policy=self.config.sandbox.network_policy,
            network_allowed_commands=self.config.sandbox.network_allowed_commands,
            fake_network=self.config.sandbox.fake_network,
            fake_network_responses=self.config.sandbox.fake_network_responses,
            protected_paths=self.config.sandbox.protected_paths,
        )
        self.docker = docker_sandbox or DockerSandbox(
            self.config.sandbox, self._decision,
        )
        self.tools = tools or self._default_tools()
        self._tool_enabled = getattr(self, "_tool_enabled", None)
        self.planner = planner or Planner(
            llm=self.llm, config=self.config.planner,
            decision_logger=self._decision,
        )
        self.parser = parser or Parser(
            max_retries=self.config.agent.max_retries,
            mode="strict" if self.config.llm.temperature < 0.3 else "loose",
            decision_logger=self._decision,
        )
        self.context = context_manager or ContextManager(
            keep_recent_rounds=self.config.agent.keep_recent_rounds,
            max_tokens=self.config.context.max_tokens,
            compression_threshold=self.config.context.compression_threshold,
            compression_method=self.config.context.compression_method,
            decision_logger=self._decision,
            active_skills=self.config.active_skills,
            active_plugins=self.config.active_plugins,
            archive_dir=self.config.context.archive_dir,
            output_truncate=self.config.context.output_truncate,
            light_threshold=self.config.context.light_threshold,
            medium_threshold=self.config.context.medium_threshold,
            heavy_threshold=self.config.context.heavy_threshold,
        )
        self.memory = memory or build_memory(self.config.memory)
        self.summarizer = summarizer or ExperienceSummarizer(
            llm=self.llm, enabled=self.config.memory.auto_experience
        )
        self.mcp = mcp_manager or MCPManager.from_config(
            load_mcp_config(), self.config.mcp,
            decision_logger=self._decision,
        )
        self._mcp_connected = False
        self.prompt_builder = prompt_builder or PromptBuilder(
            tool_schemas=self.tools.schemas(enabled=self._tool_enabled),
            llm_config=self.config.llm,
            decision_logger=self._decision,
        )
        dag = TaskDAG()
        # Docker 沙箱回滚以容器为单位：强制串行，避免并行任务间回滚互相踩踏
        max_concurrency = (1 if self.config.sandbox.docker_enabled
                           else self.config.agent.max_concurrency)
        self.scheduler = scheduler or Scheduler(
            dag=dag, max_concurrency=max_concurrency
        )
        worker = (self._docker_task_worker if self.config.sandbox.docker_enabled
                  else self._execute_task)
        self.scheduler.set_worker(worker)
        self.scheduler.set_on_task_failed(self._on_task_failed)
        self.plugins = plugin_manager or PluginManager(
            plugins_dir=self.config.plugin.dir,
            whitelist=self.config.active_plugins,
            max_active=self.config.plugin.max_active,
            enabled=self.config.plugin.enabled,
            decision_logger=self._decision,
        )
        self.skill_library = skill_library or SkillLibrary(
            skills_dir=self.config.skills.dir,
            whitelist=self.config.active_skills,
            max_active=self.config.skills.max_active,
            enabled=self.config.skills.enabled,
            decision_logger=self._decision,
        )
        self._project_ctx: Optional[ProjectContext] = None

        # 沙箱工作目录
        os.makedirs(self.config.sandbox.workspace, exist_ok=True)
    # ---- 默认工具集 ----
    def _default_tools(self) -> ToolManager:
        manager = ToolManager(policy=self.sandbox)
        docker = self.docker if self.config.sandbox.docker_enabled else None
        manager.register(TerminalTool(
            resource_monitor=self.config.sandbox.resource_monitor,
            memory_limit_mb=self.config.sandbox.memory_limit_mb,
            poll_interval=self.config.sandbox.poll_interval,
            docker=docker,
        ))
        manager.register(FileIOTool(
            audit_store=FileAuditStore(self.config.sandbox.audit_dir),
            docker=docker,
        ))
        raw = self.config.tools.model_dump()
        self._tool_enabled = {name: cfg["enabled"] for name, cfg in raw.items()}
        return manager

    # ---- 事件 ----
    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """注册实时事件回调（TUI/观测面板用）。callback 在事件循环内被同步调用。"""
        self._subscribers.append(callback)

    def _emit(self, event_type: str, **data: Any) -> None:
        record = {"type": event_type, "data": data, "ts": time.time()}
        self.events.append(record)
        for callback in list(self._subscribers):
            try:
                callback(record)
            except Exception:
                logger.exception("事件回调执行失败: %s", event_type)
        logger.info("[event] %s %s", event_type, data)

    # ---- 暂停/恢复 ----
    def pause(self) -> None:
        """暂停主循环：下一个 checkpoint 处挂起（TUI Ctrl+P）。"""
        self._pause_event.clear()

    def resume(self) -> None:
        """恢复主循环。"""
        self._pause_event.set()

    @property
    def paused(self) -> bool:
        return not self._pause_event.is_set()

    # ---- 中断 ----
    def interrupt(self, prompt: str) -> Task:
        """用户注入高优先级指令：打断当前任务流并重新调度。"""
        self.metrics.inc("interrupts")
        task = self.scheduler.spawn(instruction=prompt, priority=10)
        self.state.inject_interrupt(prompt, self.cancel_event)
        try:
            self.scheduler.wake()
        except NotImplementedError:  # 调度循环尚未启动
            pass
        self._emit("interrupt", prompt=prompt, task_id=task.id)
        return task

    async def wake(self) -> None:
        """唤醒 WAITING 状态（后台任务完成/用户输入就绪时调用）。"""
        if hasattr(self.scheduler, "wake"):
            self.scheduler.wake()

    # ---- 主入口 ----
    async def run(self, prompt: str) -> LoopResult:
        self.state.transition(AgentPhase.PLANNING)
        self._emit("run_start", prompt=prompt)
        self.metrics.set("phase", "planning")
        run_span = self.tracer.start_span("run", "run", prompt=prompt)

        # MCP 集成：连接服务器 -> 工具合并 -> 资源注入（失败容忍）
        if self.config.mcp.enabled and not self._mcp_connected:
            connected = await self.mcp.ensure_connected()
            if connected:
                await self._register_mcp_tools()
                await self._load_mcp_resources(prompt)
            self._mcp_connected = True
            self._decision.record(
                "mcp_servers", "mcp.enabled", True,
                f"MCP 服务器状态: {self.mcp.status()}；"
                f"不可用: {self.mcp.failed_servers}",
            )

        # Docker 沙箱：会话级容器启动（失败自动降级到本地工具）
        if self.config.sandbox.docker_enabled:
            await self.docker.start(self.config.sandbox.workspace)
            self._decision.record(
                "docker_enabled", "sandbox.docker_enabled", True,
                f"容器状态: {self.docker.status()}",
            )

        # 启动决策记录：循环上限 / 记忆后端 / 沙箱网络
        self._decision.record(
            "max_loop_iterations", "agent.max_loop_iterations",
            self._max_rounds, f"主循环最大迭代: {self._max_rounds}",
        )
        self._decision.record(
            "memory_backend", "memory.backend", self.config.memory.backend,
            f"长期记忆后端: {self.config.memory.backend}",
        )
        self._decision.record(
            "sandbox_network", "sandbox.network_enabled",
            self.config.sandbox.is_network_enabled,
            f"沙箱网络策略: {self.config.sandbox.network_mode}",
        )
        self._decision.record(
            "network_policy", "sandbox.network_policy",
            self.config.sandbox.network_policy,
            f"网络细粒度策略: {self.config.sandbox.network_policy}"
            f"{'（假网络）' if self.config.sandbox.fake_network else ''}",
        )
        self._decision.record(
            "resource_monitor", "sandbox.resource_monitor",
            self.config.sandbox.resource_monitor,
            f"资源熔断: {'开启' if self.config.sandbox.resource_monitor else '关闭'}"
            f"（上限 {self.config.sandbox.memory_limit_mb}MB）",
        )
        self._decision.record(
            "file_protection", "sandbox.protected_paths",
            self.config.sandbox.protected_paths,
            f"受保护路径: {self.config.sandbox.protected_paths}",
        )

        # 技能/插件上下文 + 长期记忆注入（项目上下文扫描一次，供插件/技能匹配）
        self._project_ctx = ProjectContext.scan(
            self.config.sandbox.workspace, max_depth=2, max_files=200,
            skip={"venv", ".venv", "node_modules", "dist", "build",
                  "__pycache__", ".git", ".idea"},
        )
        # 项目约定/技术栈注入 + 调用图挂载（阶段一 1.1/1.3）
        if self._project_ctx.profile_text:
            self.prompt_builder.set_project_profile(
                self._project_ctx.profile_text)
            self._decision.record(
                "profile.injected", "code.project_profile", True,
                "注入项目约定与技术栈摘要",
            )
        if self._project_ctx.call_graph is not None and \
                self._project_ctx.call_graph.symbol_count():
            self._decision.record(
                "call_graph.indexed", "code.call_graph",
                self._project_ctx.call_graph.symbol_count(),
                f"构建调用图: {self._project_ctx.call_graph.symbol_count()} 个符号，"
                f"{self._project_ctx.call_graph.edge_count()} 条调用边",
            )
        file_tool = self.tools.get("file_ops")
        if file_tool is not None:
            file_tool.call_graph = self._project_ctx.call_graph
            file_tool.decision_logger = self._decision
        skill = self._build_injected_context(prompt)
        if skill:
            self.prompt_builder.set_skill(skill)
        try:
            memory_hits = self.memory.retrieve(
                prompt, top_k=self.config.memory.top_k)
        except Exception as e:
            # 记忆检索失败不应影响主流程：降级为无记忆注入
            logger.warning("长期记忆检索失败，跳过记忆注入: %s", e)
            self._decision.record(
                "retrieval_error", "memory.backend", self.config.memory.backend,
                f"记忆检索失败已降级: {str(e)[:100]}",
            )
            memory_hits = []
        if isinstance(self.memory, NoopMemoryStore):
            self._decision.record(
                "retrieval_skip", "memory.backend", self.config.memory.backend,
                "跳过记忆检索（长期记忆已禁用）",
            )
        elif self.config.memory.similarity_threshold > 0:
            before = len(memory_hits)
            memory_hits = [
                h for h in memory_hits
                if float(h.get("score", 1.0)) >= self.config.memory.similarity_threshold
            ]
            self._decision.record(
                "retrieval_result", "memory.similarity_threshold",
                self.config.memory.similarity_threshold,
                f"检索到 {before} 项，相似度过滤后保留 {len(memory_hits)} 项",
            )
        memory_text = self.memory.format_context(memory_hits)
        if memory_text:
            self.prompt_builder.set_memory(memory_text)

        # 规划 -> READY（技能命中时按工作流展开，否则走 LLM 规划器）
        plan = self._expand_skills(prompt)
        if not plan:
            plan = await self.planner.plan(prompt)
        self.scheduler.submit_plan(plan)
        self._emit("plan_created", total=len(plan),
                   tasks=[t.instruction for t in plan])
        self.state.transition(AgentPhase.READY)

        # 执行 -> RUNNING
        self.state.transition(AgentPhase.RUNNING)
        try:
            await self.scheduler.run_to_completion()
        except Exception as e:
            logger.exception("调度循环异常")
            self._emit("run_error", error=str(e))
            self.state.transition(AgentPhase.FAILED)
            self.metrics.set("phase", "failed")
            self.tracer.end_span(run_span, status="error", error=str(e))
            self.tracer.export()
            result = LoopResult(phase=AgentPhase.FAILED, tasks=plan,
                                events=self.events)
            self.archive.write(
                prompt, self.events, self.tracer.snapshot(),
                self._decision.records(), self.metrics.snapshot(), result,
            )
            return result

        failed = [t for t in self.scheduler.dag.all() if t.status == TaskStatus.FAILED]
        completed = [t for t in self.scheduler.dag.all() if t.status == TaskStatus.COMPLETED]
        final = self._collect_final(completed, failed, prompt)

        phase = AgentPhase.COMPLETED if not failed else AgentPhase.FAILED
        self.state.transition(phase)
        self._emit("run_done", phase=phase.value, final_answer=final)
        total_rounds = sum(t.round_count for t in plan)
        self.metrics.set("phase", phase.value)
        self.metrics.set("rounds", total_rounds)
        self.metrics.set("tasks_total", len(plan))
        self.metrics.set("tasks_completed", len(completed))
        self.metrics.set("tasks_failed", len(failed))
        self.tracer.end_span(
            run_span,
            status="ok" if phase == AgentPhase.COMPLETED else "error",
            phase=phase.value, total_rounds=total_rounds,
        )
        self.tracer.export()
        result = LoopResult(
            final_answer=final,
            phase=phase,
            tasks=plan,
            total_rounds=total_rounds,
            events=self.events,
        )
        self.archive.write(
            prompt, self.events, self.tracer.snapshot(),
            self._decision.records(), self.metrics.snapshot(), result,
        )
        return result

    # ---- Docker 任务包装：任务前快照，失败自动回滚（设计 12 节） ----
    async def _docker_task_worker(self, task: Task) -> None:
        snapshot = None
        if self.docker.running:
            snapshot = await self.docker.snapshot(f"pre-{task.id}")
        try:
            await self._execute_task(task)
        finally:
            if (snapshot and self.config.sandbox.auto_rollback
                    and task.status == TaskStatus.FAILED):
                await self.docker.rollback(snapshot)

    # ---- 单任务 ReAct ----
    async def _execute_task(self, task: Task) -> None:
        """每个任务的内部 ReAct 循环：Think -> Act -> Observe -> Parse。"""
        task.mark(TaskStatus.RUNNING)
        self._emit("task_start", task_id=task.id, instruction=task.instruction)
        self.metrics.inc("tasks_started")
        task_span = self.tracer.start_span(
            f"task:{task.id}", "task", instruction=task.instruction,
        )
        self._load_task_memory(task)
        self._load_task_plugins(task)
        parse_failures = 0

        try:
            while task.round_count < self._max_rounds:
                await self._checkpoint(task)
                task.round_count += 1

                # 上下文自动压缩（对应设计第 11 节）
                if self.context.should_compact(task.history):
                    self.metrics.inc("compressions")
                    summary = self.context.compact(task.history)
                    if summary:
                        task.history.insert(0, {"role": "system", "content": summary})

                upstream = self._upstream_tasks(task)
                messages = self.prompt_builder.build(task, upstream)

                llm_span = self.tracer.start_span("llm", "llm", task_id=task.id)
                self.metrics.inc("llm_calls")
                resp = await self.llm.complete(messages)
                self.metrics.record_token_usage(estimate_tokens(resp))
                self.tracer.end_span(llm_span, status="ok", chars=len(resp))
                parsed = self.parser.parse(resp)
                task.history.append({"role": "assistant", "content": resp})

                if parsed.action_type == "think":
                    self._emit("think", task_id=task.id, content=parsed.content[:300])
                    continue

                if parsed.action_type == "tool_call":
                    calls = [(parsed.tool_name, parsed.params)]
                    calls.extend(
                        (str(c.get("tool")),
                         c.get("params") if isinstance(c.get("params"), dict) else {})
                        for c in parsed.extra_tool_calls
                    )
                    parallel = self.config.agent.parallel_tool_calls and len(calls) > 1
                    if parallel:
                        self._decision.record(
                            "parallel_execution", "agent.parallel_tool_calls",
                            len(calls), f"并行执行 {len(calls)} 个工具",
                        )
                        results = await asyncio.gather(
                            *(self._run_tool(name, params, task)
                              for name, params in calls)
                        )
                    else:
                        if len(calls) > 1:
                            self._decision.record(
                                "sequential_execution", "agent.parallel_tool_calls",
                                False, f"顺序执行 {len(calls)} 个工具",
                            )
                        results = []
                        for name, params in calls:
                            results.append(await self._run_tool(name, params, task))
                    for (name, params), result in zip(calls, results):
                        obs = self._summarize_observation(name, result)
                        task.history.append({"role": "observation", "content": obs})
                        self._emit("tool_call", task_id=task.id, tool=name,
                                   params=params, success=result.success,
                                   output=obs[:300])
                        self._index_code(params, result)
                    if any(r.metadata.get("waiting") for r in results):
                        task.mark(TaskStatus.WAITING)  # 挂起，释放控制权
                        return
                    continue

                if parsed.action_type == "final_answer":
                    task.mark(TaskStatus.COMPLETED, result=parsed.content)
                    self._emit("task_done", task_id=task.id, ok=True)
                    self.metrics.inc("tasks_completed")
                    self.tracer.end_span(task_span, status="ok",
                                         rounds=task.round_count)
                    await self._remember_experience(task)
                    return

                # 解析失败：反馈重试（最多 max_retries 次）
                parse_failures += 1
                self.metrics.inc("retries")
                if parse_failures >= self.parser.max_retries:
                    task.mark(TaskStatus.FAILED,
                              error=f"输出解析失败 {parse_failures} 次: {parsed.error}")
                    self.metrics.inc("tasks_failed")
                    self.tracer.end_span(task_span, status="error",
                                         error=task.error)
                    self._remember_error(task)
                    return
                task.history.append({
                    "role": "user",
                    "content": self.parser.retry_feedback(parsed, parse_failures),
                })

            task.mark(TaskStatus.FAILED, error=f"超过最大轮数 {self._max_rounds}")
            self.metrics.inc("tasks_failed")
            self.tracer.end_span(task_span, status="error", error=task.error)
            self._remember_error(task)
        except TaskInterrupted:
            task.mark(TaskStatus.READY)  # 中断后回到就绪，等待重新调度
            self._emit("task_interrupted", task_id=task.id)
            self.metrics.inc("interrupts")
            self.tracer.end_span(task_span, status="error", error="interrupted")
        except Exception as e:
            logger.exception("任务执行异常: %s", task.id)
            task.mark(TaskStatus.FAILED, error=str(e))
            self.metrics.inc("tasks_failed")
            self.tracer.end_span(task_span, status="error", error=str(e))
            self._remember_error(task)

    # ---- 工具 ----
    async def _run_tool(self, name: str, params: Dict[str, Any],
                        task: Task) -> ToolResult:
        """带确认策略执行单个工具（require_confirmation / auto_approve）。

        确认回调可返回：
        - True / False：批准一次 / 拒绝；
        - "approved_all:<rule>"：批准所有同类操作；
        - dict：批准并携带修改后的参数（阶段八“修改参数后执行”）。
        """
        span = self.tracer.start_span(
            f"tool:{name}", "tool", task_id=task.id,
            params=self._short_params(params),
        )
        try:
            rule = self._needs_confirmation(name, params)
            if rule is not None:
                decision = await self._ask_confirmation(name, params, rule)
                if decision is False:
                    self._decision.record(
                        "user_rejected", "agent.require_confirmation",
                        name, f"用户拒绝了 {name}",
                    )
                    result = ToolResult(
                        success=False,
                        error="用户拒绝了工具调用（require_confirmation）",
                    )
                    self.metrics.record_tool_result(False)
                    self.tracer.end_span(span, status="error",
                                         error=result.error, rejected=True)
                    return result
                if isinstance(decision, dict):
                    params = {**params, **decision}
            result = await self.tools.execute(
                name, params,
                ExecutionContext(
                    workspace=self.config.sandbox.workspace,
                    task_id=task.id,
                    instruction=task.instruction,
                    interrupt_event=self.cancel_event,
                    output_callback=self._output_callback,
                ),
            )
            self.metrics.record_tool_result(result.success)
            self.tracer.end_span(
                span,
                status="ok" if result.success else "error",
                error=result.error or "",
            )
            return result
        except Exception:
            self.metrics.record_tool_result(False)
            self.tracer.end_span(span, status="error")
            raise

    def _needs_confirmation(self, tool_name: str,
                            params: Dict[str, Any]) -> Optional[str]:
        """返回需要确认的匹配规则；None 表示无需确认。

        优先级：已批准的“所有同类”规则 > auto_approve > require_confirmation。
        """
        for rule in self._approve_rules:
            if self._match_rule(rule, tool_name, params):
                self._decision.record(
                    "approve_all", "agent.require_confirmation", rule,
                    f"工具 {tool_name} 命中已批准规则 {rule}，免确认",
                )
                return None
        for rule in self.config.agent.auto_approve:
            if self._match_rule(rule, tool_name, params):
                self._decision.record(
                    "auto_approve", "agent.auto_approve", rule,
                    f"工具 {tool_name} 命中 auto_approve，免确认",
                )
                return None
        for rule in self.config.agent.require_confirmation:
            if self._match_rule(rule, tool_name, params):
                self._decision.record(
                    "require_confirmation", "agent.require_confirmation", rule,
                    f"工具 {tool_name} 需要用户确认",
                )
                return rule
        return None

    @staticmethod
    def _match_rule(rule: str, tool_name: str,
                    params: Dict[str, Any]) -> bool:
        if rule == tool_name:
            return True
        if rule.startswith("terminal:") and tool_name == "terminal_execute":
            command = str(params.get("command", "")).strip()
            return command.startswith(rule[len("terminal:"):])
        action_map = {
            "file_write": "write", "file_read": "read",
            "file_append": "append", "file_search": "search",
        }
        if tool_name == "file_ops" and action_map.get(rule) == params.get("action"):
            return True
        return False

    async def _ask_confirmation(self, tool_name: str,
                                params: Dict[str, Any],
                                rule: Optional[str] = None) -> Any:
        """调用确认回调并解析返回值（True/False/str/dict）。"""
        if self.confirmation_callback is None:
            self._decision.record(
                "confirmation_bypassed", "agent.require_confirmation",
                tool_name, "无确认回调，按自动通过处理",
            )
            return True
        try:
            decision = await self.confirmation_callback(tool_name, params, rule)
        except TypeError:
            # 兼容旧的两参回调签名
            decision = await self.confirmation_callback(tool_name, params)
        if isinstance(decision, str) and decision.startswith("approved_all:"):
            approve_rule = decision[len("approved_all:"):] or rule or tool_name
            self._approve_rules.add(approve_rule)
            self._decision.record(
                "approve_all", "agent.require_confirmation", approve_rule,
                f"用户批准所有同类操作: {approve_rule}",
            )
            return True
        return decision

    async def _checkpoint(self, task: Task) -> None:
        """yield 控制点：每次 Observe 后检查中断信号。"""
        if self.cancel_event.is_set():
            self.cancel_event.clear()
            prompt = self.state.consume_interrupt()
            if prompt:
                task.history.append({"role": "user", "content": f"[用户中断] {prompt}"})
            raise TaskInterrupted()
        if not self._pause_event.is_set():
            await self._pause_event.wait()  # 暂停挂起，直到 resume()
        await asyncio.sleep(0)  # 让出事件循环

    def _upstream_tasks(self, task: Task) -> List[Task]:
        return [
            self.scheduler.dag.get(dep) for dep in task.dependencies
            if self.scheduler.dag.get(dep) is not None
        ]

    @staticmethod
    def _short_params(params: Any, limit: int = 200) -> str:
        """工具参数摘要（避免大段内容进入 span 属性）。"""
        text = str(params)
        return text if len(text) <= limit else text[:limit] + f"...({len(text)} chars)"

    @staticmethod
    def _summarize_observation(tool_name: str, result) -> str:
        """过长工具输出截断为摘要（原始输出在完整日志中可查）。"""
        if result.error:
            text = f"[{tool_name}] 失败: {result.error}"
        else:
            text = f"[{tool_name}] {result.output}"
        if len(text) > OBSERVATION_TRUNCATE:
            text = text[:OBSERVATION_TRUNCATE] + "\n...[输出过长已截断]..."
        return text

    async def _remember_experience(self, task: Task) -> None:
        """任务完成后生成经验摘要并写入长期记忆（设计 7.2 节）。

        写入前做相似度去重（> dedup_threshold 只更新引用计数，记录 memory.dedup）；
        成功写入记录 memory.write 决策。
        """
        try:
            if self.memory.disabled:
                self._decision.record("memory.write", "memory.backend", "none",
                                      "记忆已禁用，跳过经验写入")
                return
            summary = await self.summarizer.summarize_task(task)
            if not summary:
                return
            task_type = classify_task_type(task.instruction)
            summary.setdefault("task_type", task_type)
            summary.setdefault(
                "outcome",
                "success" if task.status == TaskStatus.COMPLETED else "failed",
            )
            threshold = self.config.memory.dedup_threshold
            similar = self.memory.find_similar(
                format_experience_text(summary),
                top_k=1, kinds=["experience"],
            )
            if threshold > 0 and similar:
                best = similar[0]
                if float(best.get("score", 0)) >= threshold:
                    self.memory.bump(best.get("id"))
                    self._decision.record(
                        "memory.dedup", "memory.dedup_threshold", threshold,
                        f"相似经验已存在（score={best.get('score'):.3f}），"
                        f"仅更新引用计数（id={best.get('id')}）",
                    )
                    return
            self.memory.remember_experience(summary)
            self._decision.record(
                "memory.write", "memory.task_type_filter", task_type,
                f"写入经验（task_type={task_type}, outcome={summary.get('outcome')}）",
            )
        except Exception as e:  # 记忆写入失败不应影响主流程
            logger.warning("经验摘要写入失败: %s", e)

    def _remember_error(self, task: Task) -> None:
        """任务失败时记录错误记忆：错误类型 + 上下文 +（可能）解决方案。"""
        try:
            error_text = task.error or "未知错误"
            err_type = (error_text.split(":")[0][:80]) or "UnknownError"
            observations = [
                str(h.get("content", ""))[:200]
                for h in task.history if h.get("role") == "observation"
            ][-2:]
            context = "\n".join([error_text] + observations) or error_text
            self.memory.remember_error(
                err_type,
                context,
                metadata={"task_id": task.id,
                          "instruction": task.instruction[:200],
                          "task_type": classify_task_type(task.instruction)},
            )
            if not self.memory.disabled:
                self._decision.record(
                    "memory.write", "memory.counter_example_penalty",
                    self.config.memory.counter_example_penalty,
                    f"写入反例（task_type={classify_task_type(task.instruction)}, "
                    f"error={err_type}）",
                )
        except Exception as e:
            logger.warning("错误记忆写入失败: %s", e)

    def _index_code(self, params: Dict[str, Any], result) -> None:
        """文件写入/读取后索引代码（路径 + 符号 + 片段，设计 7.2 节）。"""
        try:
            action = params.get("action")
            if action not in ("write", "read"):
                return
            path = params.get("path", "")
            if not path:
                return
            content = params.get("content", "") if action == "write" else result.output
            if content:
                self.memory.index_code(path, content)
        except Exception as e:
            logger.warning("代码索引失败: %s", e)

    def _load_task_memory(self, task: Task) -> None:
        """按任务指令检索相关记忆并注入 Prompt（设计 7.3 节）。

        优先按任务类型（fix/add/refactor/test/general）过滤同类型经验；
        命中为空时放宽为全量检索；记录 memory.retrieve 决策。
        """
        try:
            if self.memory.disabled:
                self._decision.record(
                    "retrieval_skip", "memory.backend", "none",
                    "跳过记忆检索（已禁用）",
                )
                return
            task_type = classify_task_type(task.instruction)
            hits = self.memory.search(
                task.instruction, top_k=self.config.memory.top_k,
                kinds=["experience", "error", "note"],
                metadata_filter={"task_type": task_type},
            )
            filtered_by_type = bool(hits)
            if not hits:
                hits = self.memory.search(
                    task.instruction, top_k=self.config.memory.top_k)
            if hits:
                self.prompt_builder.set_memory(self.memory.format_context(hits))
            self._decision.record(
                "memory.retrieve", "memory.task_type_filter", task_type,
                f"检索到 {len(hits)} 条记忆（task_type={task_type}"
                f"{'' if filtered_by_type else '，类型过滤无命中已放宽'}）",
            )
        except Exception as e:
            logger.warning("任务记忆检索失败: %s", e)

    # ---- 插件/技能（对应设计第 10 节） ----
    def _build_injected_context(self, instruction: str) -> str:
        """合并静态技能规则 + 动态插件上下文，注入 Prompt 的 skill 区块。"""
        parts: List[str] = []
        try:
            parts.append(self.context.build_skill_context(instruction))
        except Exception as e:
            logger.warning("静态技能上下文构建失败: %s", e)
        ctx = ProjectContext.from_instruction(instruction, self._project_ctx)
        try:
            active = self.plugins.get_active(instruction, ctx.files, ctx.deps)
            parts.append(self.plugins.to_context(active))
        except Exception as e:
            logger.warning("插件激活失败: %s", e)
        return "\n\n".join(x for x in parts if x)

    def _expand_skills(self, prompt: str) -> List[Task]:
        """技能命中时把 YAML 工作流展开为子任务 DAG（设计 10.2 节）。"""
        if not (self.config.skills.enabled and self.config.skills.workflow_enabled):
            return []
        try:
            ctx = ProjectContext.from_instruction(prompt, self._project_ctx)
            matched = self.skill_library.match(prompt, ctx.files, ctx.deps)
            if not matched:
                return []
            plan: List[Task] = []
            for skill in matched:
                self._decision.record(
                    "skill.activate", "skills.enabled", True,
                    f"技能 {skill.name}（{len(skill.steps)} 步）命中激活",
                )
                plan.extend(self.skill_library.expand(skill, prompt))
            self._emit("skills_activated",
                       skills=[s.name for s in matched], total=len(plan))
            return plan
        except Exception as e:
            logger.warning("技能工作流展开失败，回退 LLM 规划: %s", e)
            return []

    def _load_task_plugins(self, task: Task) -> None:
        """按子任务指令刷新插件上下文（文件类型/项目依赖触发按任务感知）。"""
        try:
            injected = self._build_injected_context(task.instruction)
            if injected:
                self.prompt_builder.set_skill(injected)
        except Exception as e:
            logger.warning("子任务插件上下文注入失败: %s", e)

    def _on_task_failed(self, task: Task) -> None:
        """技能步骤失败处理：fallback 回退重试 / orchestrate 升级介入（设计 10.2 节）。"""
        meta = task.metadata or {}
        on_failure = str(meta.get("on_failure", ""))
        if on_failure == "fallback" and meta.get("fallback"):
            if not self.config.skills.allow_fallback:
                self._decision.record(
                    "skill.step_fallback", "skills.allow_fallback", False,
                    f"步骤 {task.id} 失败但 fallback 被禁用",
                )
                return
            fallback_task = self.scheduler.spawn(
                instruction=f"{meta['fallback']}（原步骤: {task.instruction[:120]}）",
                parent_id=task.id,
                priority=task.priority,
            )
            self._decision.record(
                "skill.step_fallback", "skills.allow_fallback", True,
                f"技能步骤 {task.id} 失败，回退任务 {fallback_task.id}: "
                f"{str(meta['fallback'])[:80]}",
            )
            self._emit("skill_fallback", task_id=task.id, fallback_id=fallback_task.id)
        elif on_failure == "orchestrate":
            self._decision.record(
                "skill.step_intervention", "skills.workflow_enabled", True,
                f"技能步骤 {task.id} 需要 Orchestrator/人工介入",
            )
            self._emit("skill_intervention", task_id=task.id)

    # ---- MCP ----
    async def _register_mcp_tools(self) -> None:
        """把 MCP 工具合并进 ToolManager 并刷新 Prompt 工具描述。"""
        try:
            tools = await self.mcp.build_tools()
            for tool in tools:
                self.tools.register(tool)
            self.prompt_builder.update_tools(
                self.tools.schemas(enabled=self._tool_enabled)
            )
            self._emit("mcp_tools", count=len(tools),
                       names=[t.name for t in tools])
        except Exception as e:
            logger.warning("MCP 工具合并失败: %s", e)

    async def _load_mcp_resources(self, prompt: str) -> None:
        """按任务关键词匹配并读取 MCP 资源，注入 Prompt（设计 13.3 节）。"""
        try:
            resources = await self.mcp.list_resources()
            if not resources:
                return
            terms = [t for t in re.split(r"\W+", prompt.lower()) if len(t) > 1]
            scored = []
            for r in resources:
                hay = f"{r.get('name', '')} {r.get('uri', '')} {r.get('description', '')}".lower()
                score = sum(1 for t in terms if t in hay)
                if score > 0:
                    scored.append((score, r))
            scored.sort(key=lambda x: -x[0])
            chosen = [r for _, r in scored[: self.config.mcp.max_resources_per_run]]
            blocks = []
            for r in chosen:
                content = await self.mcp.read_resource(r["server"], r["uri"])
                if content:
                    blocks.append(
                        f"[{r['name']}] ({r['uri']})\n{content[:800]}"
                    )
            if blocks:
                self.prompt_builder.set_resources("\n\n".join(blocks))
                self._emit("mcp_resources", count=len(blocks))
        except Exception as e:
            logger.warning("MCP 资源加载失败: %s", e)

    async def close(self) -> None:
        """释放 MCP 连接与 Docker 沙箱等资源。"""
        await self.mcp.disconnect_all()
        self._mcp_connected = False
        if self.docker.running:
            await self.docker.stop()

    def _collect_final(self, completed: List[Task], failed: List[Task], prompt: str) -> str:
        answers = [t.result for t in completed if isinstance(t.result, str) and t.result]
        if answers:
            return "\n".join(answers)
        if failed:
            return f"任务失败: {'; '.join(t.error or t.instruction for t in failed[:3])}"
        return f"（未产生最终结果）: {prompt}"