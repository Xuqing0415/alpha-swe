"""AgentLoop —— 异步 ReAct 主循环。

对应设计第 2 节：
- 会话级状态机 IDLE->PLANNING->READY->RUNNING->WAITING->COMPLETED/FAILED；
- 每个任务执行 Think -> Act -> Observe -> Parse 的内部循环；
- 每次 Observe 后检查中断信号（yield 控制点），支持用户注入高优先级任务；
- 与 Scheduler 配合实现 DAG 依赖调度与并发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
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
from agent.counterfactual import prepend_warnings
from agent.planner.planner import Planner
from agent.prompt.builder import PromptBuilder, estimate_tokens
from agent.sandbox.audit import FileAuditStore
from agent.sandbox.docker_sandbox import DockerSandbox
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext, ToolResult
from agent.tools.background import BackgroundTaskTool
from agent.tools.fileio import FileIOTool
from agent.tools.git_tool import GitTool
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool
from agent.tools.test_tool import TestRunnerTool

logger = logging.getLogger("alpha-swe.loop")


class TaskInterrupted(Exception):
    """任务被用户中断（yield 控制点抛出）。"""

class TaskPreempted(Exception):
    """任务被更高优先级任务抢占（进阶 2.1：安全点暂停，等待恢复）。"""

class TaskBudgetExceeded(Exception):
    """任务资源预算耗尽（进阶 2.3）：token 或时间预算用尽，任务终止。"""

    def __init__(self, kind: str, used: float, budget: float,
                 report: Optional[Dict[str, Any]] = None,
                 borrowed: float = 0.0):
        super().__init__(
            f"任务预算耗尽（{kind}）: 已用 {used:.0f} / 预算 {budget:.0f}"
            + (f"，借用 {borrowed:.0f}" if borrowed else ""))
        self.kind = kind
        self.used = used
        self.budget = budget
        self.report = report or {}
        self.borrowed = borrowed


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
        # 收敛期 P1：内存事件列表有界（max_events=0 表示不裁剪）
        self._max_events = int(getattr(
            self.config.agent, "max_events", 10000) or 0)
        self._events_trimmed = False
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
            otlp_endpoint=self.config.agent.otel_endpoint,
            otlp_enabled=self.config.agent.otel_enabled,
            service_name=self.config.agent.otel_service_name,
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
        # 方案 2.4：注入自定义工具集时，从管理器找回后台任务工具，
        # 确保 close() 仍能清理由外部注册的 background_task 实例
        if getattr(self, "_background_tasks", None) is None:
            _bg = self.tools.get("background_task")
            if isinstance(_bg, BackgroundTaskTool):
                self._background_tasks = _bg
        self._tool_enabled = getattr(self, "_tool_enabled", None)
        self.planner = planner or Planner(
            llm=self.llm, config=self.config.planner,
            decision_logger=self._decision,
        )
        self.parser = parser or Parser(
            max_retries=self.config.agent.max_retries,
            mode="strict" if self.config.llm.temperature < 0.3 else "loose",
            decision_logger=self._decision,
            require_reasoning=self.config.agent.require_reasoning,
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
        # 方案 1.1：任务级重试包装（失败且有预算时 RETRYING -> READY 重跑）
        self.scheduler.set_worker(self._wrap_with_retry(worker))
        self.scheduler.set_on_task_failed(self._on_task_failed)
        if self.config.agent.snapshot_enabled:
            # 方案 1.3：每完成一个子步骤自动落盘快照（断点续跑）
            self.scheduler.set_on_snapshot(self._save_snapshot)
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
            registry_file=self.config.skills.registry_file,
            usage_log=self.config.skills.usage_log,
            require_task_intent=self.config.skills.require_task_intent,
        )
        self._project_ctx: Optional[ProjectContext] = None

        # 沙箱工作目录
        os.makedirs(self.config.sandbox.workspace, exist_ok=True)
    # ---- 默认工具集 ----
    def _default_tools(self) -> ToolManager:
        manager = ToolManager(
            policy=self.sandbox,
            default_timeout=self.config.tools.file_ops.timeout,
        )
        docker = self.docker if self.config.sandbox.docker_enabled else None
        manager.register(TerminalTool(
            default_timeout=self.config.tools.terminal_execute.timeout,
            resource_monitor=self.config.sandbox.resource_monitor,
            memory_limit_mb=self.config.sandbox.memory_limit_mb,
            poll_interval=self.config.sandbox.poll_interval,
            docker=docker,
        ))
        manager.register(FileIOTool(
            audit_store=FileAuditStore(self.config.sandbox.audit_dir),
            docker=docker,
        ))
        manager.register(TestRunnerTool(
            decision_logger=self._decision,
        ))
        # 方案 2.4：后台任务生命周期管理（start/status/logs/stop）
        self._background_tasks = BackgroundTaskTool(
            decision_logger=self._decision,
        )
        manager.register(self._background_tasks)
        # 方案 3.4：Git 版本管理（写操作走确认策略）
        manager.register(GitTool(decision_logger=self._decision))
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
        if self._max_events > 0 and len(self.events) > self._max_events:
            del self.events[: len(self.events) - self._max_events]
            if not self._events_trimmed:
                self._events_trimmed = True
                self._decision.record(
                    "events.trimmed", "agent.max_events",
                    self._max_events,
                    "事件列表超限，丢弃最旧事件（内存有界）",
                )
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

    def set_task_priority(self, task_id: str, priority: int) -> Optional[Task]:
        """调整任务优先级（TUI /priority 命令），并唤醒调度循环重新评估。"""
        task = self.scheduler.set_priority(task_id, priority)
        if task is not None:
            self._decision.record(
                "task.priority", "agent.preemption_enabled", priority,
                f"任务 {task_id} 优先级调整为 {priority}",
            )
            self._emit("priority_changed", task_id=task_id,
                       priority=priority)
        return task

    async def wake(self) -> None:
        """唤醒 WAITING 状态（后台任务完成/用户输入就绪时调用）。"""
        if hasattr(self.scheduler, "wake"):
            self.scheduler.wake()

    # ---- 主入口 ----
    async def run(self, prompt: str, resume: bool = False) -> LoopResult:
        self._current_prompt = prompt
        # 配置降级记录（决策日志/TUI 可见，去重）
        self._record_config_fallbacks()

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
        memory_text, cf_count = prepend_warnings(memory_hits, memory_text)
        if cf_count:
            self._decision.record(
                "counterfactual.injected", "agent.counterfactual_enabled",
                self.config.agent.counterfactual_enabled,
                f"注入 {cf_count} 条反事实警告（相似失败教训）",
            )
        if memory_text:
            self.prompt_builder.set_memory(memory_text)

        # 规划 -> READY（技能命中时按工作流展开，否则走 LLM 规划器；
        # resume=True 时跳过技能展开，优先从快照恢复任务树）
        plan = [] if resume else self._expand_skills(prompt)
        restored = False
        if resume and not plan:
            plan, restored = self._restore_snapshot_plan()
        if not plan:
            # 注入项目调用图与约定摘要到拆分提示（阶段一 1.2/1.3）；
            # 对不支持新参数的 Planner 实现自动降级
            plan = await self._plan_with_context(prompt)
            restored = False
        if not restored:
            # 快照恢复路径已直接把恢复的 DAG 挂到 scheduler，无需重复提交
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
            self._maybe_counterfactual(prompt, result)
            return result

        all_tasks = self.scheduler.dag.all()
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]
        skipped = [t for t in all_tasks if t.status == TaskStatus.SKIPPED]
        final = self._collect_final(completed, failed, skipped, prompt)

        phase = AgentPhase.COMPLETED if not failed else AgentPhase.FAILED
        self.state.transition(phase)
        self._emit("run_done", phase=phase.value, final_answer=final)
        total_rounds = sum(t.round_count for t in plan)
        self.metrics.set("phase", phase.value)
        self.metrics.set("rounds", total_rounds)
        self.metrics.set("tasks_total", len(plan))
        self.metrics.set("tasks_completed", len(completed))
        self.metrics.set("tasks_failed", len(failed))
        self.metrics.set("tasks_skipped", len(skipped))
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
        self._maybe_counterfactual(prompt, result)
        return result

    # ---- 断点续跑（方案 1.3）：任务快照落盘 / 读取 / 恢复 ----
    def _save_snapshot(self) -> None:
        """每完成一个子步骤后把任务树快照落盘，保留最近 N 个。"""
        if not self.config.agent.snapshot_enabled:
            return
        dag = getattr(self.scheduler, "dag", None)
        if dag is None:
            return
        try:
            snapshot_dir = Path(self.config.agent.snapshot_dir)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            data = dag.to_snapshot()
            data["prompt"] = getattr(self, "_current_prompt", "")
            ts = time.strftime("%Y%m%d-%H%M%S")
            step = sum(1 for t in dag.all()
                       if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                       TaskStatus.SKIPPED))
            path = snapshot_dir / f"task_{ts}_step{step}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, default=str),
                            encoding="utf-8")
            keep = int(getattr(self.config.agent, "snapshot_keep", 5))
            files = sorted(snapshot_dir.glob("task_*.json"),
                           key=lambda p: (p.stat().st_mtime, p.name),
                           reverse=True)
            for old in files[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            logger.exception("任务快照保存失败")

    def _latest_snapshot(self) -> Optional[Dict[str, Any]]:
        """返回最新快照 dict；无快照或读取失败返回 None。"""
        try:
            snapshot_dir = Path(self.config.agent.snapshot_dir)
            files = sorted(snapshot_dir.glob("task_*.json"),
                           key=lambda p: (p.stat().st_mtime, p.name),
                           reverse=True)
            if not files:
                return None
            data = json.loads(files[0].read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            logger.exception("读取最近快照失败")
            return None

    def _restore_snapshot_plan(self) -> tuple:
        """从最近快照恢复任务树；成功返回 (plan, True)，否则 (空, False) 触发重新规划。

        恢复策略：已终结任务保持原状（结果/错误保留），未终结任务统一回到 READY
        由调度器重新执行，避免断点处任务卡死在 RUNNING/WAITING。
        """
        snapshot = self._latest_snapshot()
        if snapshot is None:
            self._decision.record(
                "resume.no_snapshot", "agent.snapshot_enabled", True,
                "未找到可恢复的任务快照，重新规划任务",
            )
            return [], False
        try:
            dag = TaskDAG.from_snapshot(snapshot)
            terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED,
                        TaskStatus.SKIPPED)
            for t in dag.all():
                if t.status not in terminal:
                    t.status = TaskStatus.READY
                    # 进阶 2.3：清空运行期预算状态，避免跨进程时间戳误判
                    t.metadata.pop("_started_at", None)
                    t.metadata.pop("_tokens_used", None)
                    t.metadata.pop("_budget_warned", None)
                    t.metadata.pop("_budget_borrowed", None)
            self.scheduler.dag = dag
            plan = dag.all()
            done = sum(1 for t in plan if t.status in terminal)
            self._decision.record(
                "resume.restored", "agent.snapshot_enabled", True,
                f"从断点恢复任务树：共 {len(plan)} 个子任务（已完成 {done}）",
            )
            logger.info("从快照恢复任务树: %d 个子任务（已完成 %d）",
                        len(plan), done)
            return plan, True
        except Exception as e:
            logger.exception("快照恢复失败，回退重新规划: %s", e)
            self._decision.record(
                "resume.fallback", "agent.snapshot_enabled", True,
                f"快照恢复失败已回退重新规划: {str(e)[:100]}",
            )
            return [], False

    # ---- 任务级重试（方案 1.1）：immediate / backoff / retry_with_context ----
    def _wrap_with_retry(self, worker) -> Callable[[Task], Any]:
        """把任意任务 worker 包装成带重试循环的执行器。"""
        async def _run(task: Task) -> None:
            while True:
                await worker(task)
                if task.status != TaskStatus.FAILED:
                    return
                if not self._retry_available(task):
                    self._apply_criticality(task)
                    return  # 重试预算耗尽：critical 保持 FAILED，其余转 SKIPPED
                task.retry_count += 1
                strategy = task.retry_strategy
                self._decision.record(
                    "task.retry", "agent.max_retries", task.max_retries,
                    f"任务 {task.id} 第 {task.retry_count} 次重试"
                    f"（策略 {strategy}）: {str(task.error or '')[:120]}",
                )
                if strategy == "retry_with_context":
                    # 把失败原因回写进下一轮 Prompt（对应方案 1.1）
                    task.history.append({
                        "role": "user",
                        "content": f"[上一步失败原因] {task.error}",
                    })
                task.mark(TaskStatus.RETRYING)
                self._emit("task_retry", task_id=task.id,
                           retry_count=task.retry_count, strategy=strategy,
                           error=str(task.error))
                delay = self._retry_delay(task)
                if delay > 0:
                    logger.warning("任务 %s 重试前等待 %.1fs（%d/%d）",
                                   task.id, delay, task.retry_count,
                                   task.max_retries)
                    await asyncio.sleep(delay)
                task.mark(TaskStatus.READY)
        return _run

    @staticmethod
    def _retry_available(task: Task) -> bool:
        """是否还有重试预算；预算耗尽视为永久失败，不重试。"""
        if task.metadata.get("_budget_exhausted"):
            return False
        return task.retry_count < task.max_retries

    @staticmethod
    def _retry_delay(task: Task) -> float:
        """按策略计算重试等待秒数（backoff: 1s/2s/4s/8s，其余立即）。"""
        if task.retry_strategy == "backoff":
            return min(2.0 ** (task.retry_count - 1), 8.0)
        return 0.0  # immediate / retry_with_context 立即重试

    def _on_task_failure(self, task: Task) -> None:
        """任务失败但可能重试/降级：有预算时推迟失败计数与反例记忆。"""
        if self._retry_available(task):
            self._decision.record(
                "task.retry_pending", "agent.max_retries", task.max_retries,
                f"任务 {task.id} 第 {task.retry_count + 1} 次失败待重试: "
                f"{str(task.error or '')[:120]}",
            )
            return
        # 预算耗尽：normal/optional 将由 _apply_criticality 转 SKIPPED，
        # 不记失败与反例；只有 critical 才真正失败。
        if (task.criticality or "normal") != "critical":
            return
        self.metrics.inc("tasks_failed")
        self._remember_error(task)

    def _apply_criticality(self, task: Task) -> None:
        """方案 1.2：重试耗尽后按 criticality 降级/跳过。

        critical   -> 保持 FAILED 向上传播；
        normal     -> 标记 SKIPPED（后续步骤继续，汇总报告列出原因）；
        optional   -> 同样标记 SKIPPED，不产生失败告警。
        """
        crit = task.criticality or "normal"
        if crit == "critical":
            return
        reason = task.error or "执行失败（重试耗尽）"
        task.mark(TaskStatus.SKIPPED, error=reason)
        self.metrics.inc("tasks_skipped")
        self._decision.record(
            "task.skipped", "agent.criticality", crit,
            f"任务 {task.id}（criticality={crit}）失败后跳过: "
            f"{str(reason)[:120]}",
        )
        self._emit("task_skipped", task_id=task.id, criticality=crit,
                   reason=str(reason))
        logger.warning("任务 %s 已跳过（criticality=%s）: %s",
                       task.id, crit, str(reason)[:120])

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
        resumed = bool(task.metadata.pop("_resumed", False))
        task.metadata.setdefault("_started_at", time.monotonic())
        task.mark(TaskStatus.RUNNING)
        if resumed:  # 进阶 2.1：抢占后恢复执行
            self._emit("task_resumed", task_id=task.id,
                       priority=task.priority)
        self._inject_exec_env()
        self._emit(
            "task_start",
            task_id=task.id,
            instruction=task.instruction,
            skill=task.metadata.get("skill"),
            skill_step=task.metadata.get("skill_step"),
            step_index=task.metadata.get("step_index"),
            step_total=task.metadata.get("step_total"),
        )
        self.metrics.inc("tasks_started")
        task_span = self.tracer.start_span(
            f"task:{task.id}", "task", instruction=task.instruction,
        )
        self._load_task_memory(task)
        self._load_task_plugins(task)
        parse_failures = 0
        degenerate_streak = 0  # 连续空参数工具调用计数（防无效循环）

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
                msg_tokens = estimate_tokens(messages)
                self.metrics.record_token_usage(msg_tokens)
                self._add_task_tokens(task, msg_tokens)
                resp = await self.llm.complete(messages)
                resp_tokens = estimate_tokens(resp)
                self.metrics.record_token_usage(resp_tokens)
                self._add_task_tokens(task, resp_tokens)
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
                    # 决策理由显式化（进阶 1.1）：记录 Agent 为什么选择该工具
                    if parsed.reasoning:
                        self._decision.record(
                            "tool.reasoning", "agent.require_reasoning",
                            self.config.agent.require_reasoning,
                            f"{parsed.tool_name}: {parsed.reasoning[:200]}",
                        )
                    # 空参数保护：连续空/缺参调用给出明确纠正，达阈值即中止
                    if parsed.content:
                        self._emit("think", task_id=task.id,
                                   content=str(parsed.content)[:300])
                    bad = self._first_degenerate_call(calls)
                    if bad is not None:
                        degenerate_streak += 1
                        name, params, reason, example = bad
                        obs = (f"[{name}] 参数错误: {reason}。"
                               f"请输出完整参数 JSON，例如 {example}")
                        task.history.append({"role": "observation",
                                              "content": obs})
                        self._emit("tool_call", task_id=task.id, tool=name,
                                   params=params, success=False, output=obs[:300])
                        # 与 tool_call 失败事件保持一致：空参数保护也计入
                        # 工具失败指标（此前只发事件不计 metrics，导致归因盲区）
                        self.metrics.record_tool_result(False)
                        if degenerate_streak >= 3:
                            self._decision.record(
                                "degenerate_abort", "agent.max_retries",
                                degenerate_streak,
                                f"连续空参数 {degenerate_streak} 次，中止任务避免无效循环",
                            )
                            task.mark(TaskStatus.FAILED,
                                      error=f"工具调用参数持续为空（{degenerate_streak} 次），已中止以避免无效循环")
                            self.tracer.end_span(task_span, status="error",
                                                 error=task.error)
                            self._on_task_failure(task)
                            return
                        continue
                    degenerate_streak = 0
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
                        obs = await self._summarize_observation(
                            name, result, task)
                        task.history.append({"role": "observation", "content": obs})
                        self._emit("tool_call", task_id=task.id, tool=name,
                                   params=params, success=result.success,
                                   output=obs[:300], meta=result.metadata,
                                   reasoning=(
                                       parsed.reasoning
                                       if name == parsed.tool_name else ""))
                        self._index_code(params, result)
                    if any(r.metadata.get("circuit_broken") for r in results):
                        task.mark(TaskStatus.FAILED,
                                  error="工具执行连续超时，触发熔断（见上一步观察）")
                        self._emit("task_failed", task_id=task.id,
                                   reason="timeout_circuit_breaker")
                        self.tracer.end_span(task_span, status="error",
                                             error=task.error)
                        self._on_task_failure(task)
                        return
                    if any(r.metadata.get("waiting") for r in results):
                        task.mark(TaskStatus.WAITING)  # 挂起，释放控制权
                        return
                    # 进阶 2.1/2.3：工具调用返回也是安全点，先查预算再查抢占
                    self._maybe_enforce_budget(task)
                    await self._maybe_preempt(task)
                    continue

                if parsed.action_type == "final_answer":
                    # 决策理由显式化：记录任务完成的"为什么"
                    if parsed.reasoning:
                        self._decision.record(
                            "final.reasoning", "agent.require_reasoning",
                            self.config.agent.require_reasoning,
                            parsed.reasoning[:200],
                        )
                    task.mark(TaskStatus.COMPLETED, result=parsed.content)
                    self._emit("task_done", task_id=task.id, ok=True,
                               reasoning=parsed.reasoning)
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
                    self.tracer.end_span(task_span, status="error",
                                         error=task.error)
                    self._on_task_failure(task)
                    return
                task.history.append({
                    "role": "user",
                    "content": self.parser.retry_feedback(parsed, parse_failures),
                })

            task.mark(TaskStatus.FAILED, error=f"超过最大轮数 {self._max_rounds}")
            self.tracer.end_span(task_span, status="error", error=task.error)
            self._on_task_failure(task)
        except TaskBudgetExceeded as e:
            # 进阶 2.3：预算耗尽——生成预算报告并终止任务（不重试）
            task.metadata["_budget_exhausted"] = True
            task.metadata["budget_report"] = e.report
            self.metrics.inc("budget_exhausted")
            task.mark(TaskStatus.FAILED, error=str(e))
            self._emit("budget_exhausted", task_id=task.id,
                       kind=e.kind, used=e.used, budget=e.budget,
                       report=e.report)
            self.tracer.end_span(task_span, status="error", error=str(e))
            self._on_task_failure(task)
        except TaskPreempted:
            # 进阶 2.1：被更高优先级任务抢占——挂起并落盘快照，等待恢复
            task.mark(TaskStatus.PAUSED)
            self._emit("task_preempted", task_id=task.id,
                       priority=task.priority)
            self.tracer.end_span(task_span, status="paused",
                                 error="preempted")
            if self.config.agent.snapshot_enabled:
                self._save_snapshot()  # 抢占点快照，支持断点续跑
        except TaskInterrupted:
            task.mark(TaskStatus.READY)  # 中断后回到就绪，等待重新调度
            self._emit("task_interrupted", task_id=task.id)
            self.metrics.inc("interrupts")
            self.tracer.end_span(task_span, status="error", error="interrupted")
        except Exception as e:
            logger.exception("任务执行异常: %s", task.id)
            task.mark(TaskStatus.FAILED, error=str(e))
            self.tracer.end_span(task_span, status="error", error=str(e))
            self._on_task_failure(task)

    # ---- 工具 ----
    def _inject_exec_env(self) -> None:
        """按实际沙箱模式注入执行环境说明，避免模型误用 bash 语法。"""
        if getattr(self.docker, "running", False):
            text = ("沙箱为 Linux（Docker 容器）：shell 为 bash (/bin/sh)，"
                    "支持 && / || / 管道与 POSIX 工具（ls、grep、find、cat 等）。")
        else:
            text = ("本机为 Windows PowerShell 5.1：不支持 && 与 ||，命令之间请用 ; 分隔；"
                    "路径分隔符用 \\；列目录用 Get-ChildItem，读文件用 Get-Content，"
                    "搜索用 Select-String，不要使用 bash 语法（如 ls -la / find / cat）。")
        self.prompt_builder.set_exec_env(text)

    def _first_degenerate_call(
        self, calls: List[tuple]
    ) -> Optional[Tuple[str, Dict[str, Any], str, str]]:
        """返回首个缺必需参数的工具调用 (name, params, reason, example)。

        全部合法或工具 schema 未知时返回 None（未知工具交给执行器报错）。
        """
        schemas = {s["name"]: s.get("parameters", {}) for s in
                   self.tools.schemas(enabled=self._tool_enabled)}
        placeholders = {
            "command": "Get-ChildItem",
            "action": "read",
            "path": "src/main.py",
            "pattern": "TODO",
            "content": "...",
        }
        for name, params in calls:
            params = params if isinstance(params, dict) else {}
            schema = schemas.get(name)
            if schema is None:
                continue
            required = schema.get("required", [])
            missing = [
                key for key in required
                if params.get(key) is None
                or (isinstance(params.get(key), str) and not params.get(key).strip())
            ]
            if not missing:
                continue
            props = schema.get("properties", {})
            example = {"tool": name, "params": {}}
            for key in required:
                if key in params and key not in missing:
                    example["params"][key] = params[key]
                else:
                    ptype = props.get(key, {}).get("type", "string")
                    if key in placeholders:
                        example["params"][key] = placeholders[key]
                    elif ptype in ("number", "integer"):
                        example["params"][key] = 1
                    elif ptype == "boolean":
                        example["params"][key] = True
                    else:
                        example["params"][key] = "..."
            return (name, params, f"缺少必需参数 {missing}",
                    json.dumps(example, ensure_ascii=False))
        return None

    @staticmethod
    def _timeout_key(name: str, params: Dict[str, Any]) -> str:
        """熔断粒度键：terminal 按命令、其余工具按 工具名:action。"""
        if name == "terminal_execute":
            return "terminal:" + str(params.get("command", ""))[:80]
        action = params.get("action") if isinstance(params, dict) else None
        return f"{name}:{action or '?'}"

    @staticmethod
    def _tool_timeout(name: str, params: Dict[str, Any]) -> Optional[float]:
        """按工具类型给外层超时（方案 2.1：terminal 60s / file_read 5s / file_write 10s）。

        terminal_execute 自带 terminate->kill 清理，返回 None 由工具层自管；
        file_ops 按 action 区分读/写/搜索；其余工具统一 30s 兜底。
        """
        if name == "terminal_execute":
            return None
        if name == "file_ops":
            action = params.get("action") if isinstance(params, dict) else None
            return {"read": 5.0, "write": 10.0, "append": 10.0,
                    "edit": 10.0}.get(action, 30.0)
        if name == "background_task":
            return 10.0  # 管理类动作必须快速返回，不阻塞循环
        if name == "git_ops":
            return 30.0
        return 30.0

    def _track_timeout(self, name: str, params: Dict[str, Any],
                       task: Task, result: ToolResult) -> None:
        """同一任务内同命令连续超时达到阈值即熔断（方案 2.1）。

        熔断记录在任务级 metadata（_timeout_strikes），不影响其他任务；
        达到阈值后把 circuit_broken 标到结果上，由 _execute_task 中止任务。
        """
        key = self._timeout_key(name, params)
        strikes = task.metadata.setdefault("_timeout_strikes", {})
        n = int(strikes.get(key, 0)) + 1
        strikes[key] = n
        threshold = int(getattr(self.config.agent, "max_timeout_strikes", 3))
        self._decision.record(
            "timeout.strike", "agent.max_timeout_strikes", threshold,
            f"{key} 连续超时 {n}/{threshold} 次",
        )
        if n >= threshold:
            result.metadata["circuit_broken"] = True
            result.error = (result.error or "") + (
                f"（{key} 连续超时 {n} 次，触发熔断，任务中止）")

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
                timeout=self._tool_timeout(name, params),
            )
            if result.metadata.get("timed_out"):
                self._track_timeout(name, params, task, result)
            # 进阶 3.1：代码写入后自动补齐测试（无测试覆盖时生成 test_*.py）
            generated_test = None
            if result.success and self.config.agent.auto_testgen:
                generated_test = await self._maybe_auto_testgen(
                    name, params, task)
            # 进阶 3.3：回归检测——改完就测，测不过把失败回写该步骤；
            # 本步自动生成的测试不参与回归判定（生成失败属测试生成器问题，
            # 而非代码变更引入的回归）
            if result.success and self.config.agent.regression_check_enabled:
                result = await self._maybe_regression_check(
                    name, params, task, result, skip_test=generated_test)
            self.metrics.record_tool_result(result.success)
            # 方案 2.3：进度回写事件（TUI 任务面板/状态栏实时刷新）
            self._emit(
                "execution_completed",
                task_id=task.id,
                tool=name,
                success=result.success,
                elapsed_ms=result.elapsed_ms,
                timed_out=bool(result.metadata.get("timed_out")),
                circuit_broken=bool(result.metadata.get("circuit_broken")),
            )
            end_attrs = {}
            if result.output:
                end_attrs["out"] = (result.output or "")[:240]
            self.tracer.end_span(
                span,
                status="ok" if result.success else "error",
                error=result.error or "",
                **end_attrs,
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
        # file_write 规则同时覆盖精确行编辑 edit
        if (tool_name == "file_ops" and rule == "file_write"
                and params.get("action") in ("write", "edit")):
            return True
        git_map = {
            "git_status": "status", "git_diff": "diff", "git_log": "log",
            "git_branch": "branch", "git_commit": "commit",
            "git_push": "push", "git_branch_delete": "branch_delete",
        }
        if tool_name == "git_ops" and git_map.get(rule) == params.get("action"):
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
        """yield 控制点：每次 Observe 后检查中断信号与抢占请求。"""
        if self.cancel_event.is_set():
            self.cancel_event.clear()
            prompt = self.state.consume_interrupt()
            if prompt:
                task.history.append({"role": "user", "content": f"[用户中断] {prompt}"})
            raise TaskInterrupted()
        if not self._pause_event.is_set():
            await self._pause_event.wait()  # 暂停挂起，直到 resume()
        self._maybe_enforce_budget(task)  # 资源预算检查（进阶 2.3）
        await self._maybe_preempt(task)  # 高优先级任务抢占（进阶 2.1）
        await asyncio.sleep(0)  # 让出事件循环

    def _add_task_tokens(self, task: Optional[Task], delta: float) -> None:
        """累计任务级 token 消耗（进阶 2.3）。"""
        if task is None:
            return
        task.metadata["_tokens_used"] = (
            task.metadata.get("_tokens_used", 0) + max(0, int(delta)))

    def _budget_report(self, task: Task, used: int, token_budget: int,
                       elapsed: float, time_budget: float,
                       borrowed: int) -> Dict[str, Any]:
        """生成预算消耗报告（进阶 2.3）。"""
        return {
            "task_id": task.id,
            "instruction": task.instruction[:100],
            "tokens_used": used,
            "token_budget": token_budget,
            "borrowed": borrowed,
            "elapsed_s": round(elapsed, 1),
            "time_budget": time_budget,
            "rounds": task.round_count,
            "status": task.status.value,
        }

    def _borrow_budget(self, task: Task, deficit: int) -> int:
        """高优先级任务借用低优先级任务的未用 token 预算（进阶 2.3）。

        只从优先级更低的任务借用；借用额记录到双方 metadata 供审计。
        """
        if not self.config.agent.budget_borrow_enabled:
            return 0
        borrowed = 0
        candidates = [
            t for t in self.scheduler.dag.all()
            if t.id != task.id and t.priority < task.priority
        ]
        for t in sorted(candidates,
                        key=lambda x: (-x.priority, x.created_at)):
            tb = t.token_budget or self.config.agent.default_token_budget
            used = t.metadata.get("_tokens_used", 0)
            remaining = max(0, tb - used)
            if remaining <= 0:
                continue
            take = min(remaining, deficit)
            t.metadata["_budget_lent"] = (
                t.metadata.get("_budget_lent", 0) + take)
            task.metadata["_budget_borrowed"] = (
                task.metadata.get("_budget_borrowed", 0) + take)
            borrowed += take
            deficit -= take
            if deficit <= 0:
                break
        if borrowed:
            self._decision.record(
                "budget.borrow", "agent.budget_borrow_enabled",
                self.config.agent.budget_borrow_enabled,
                f"任务 {task.id} 借用 {borrowed} token"
                f"（高优先级借用低优先级未用预算）",
            )
        return borrowed

    def _maybe_enforce_budget(self, task: Task) -> None:
        """安全点预算检查：80% 告警，100% 尝试借用后终止（进阶 2.3）。

        调用方为 _checkpoint 与工具调用返回安全点；
        耗尽时抛 TaskBudgetExceeded 由 _execute_task 统一处理。
        """
        if not self.config.agent.budget_enabled:
            return
        token_budget = (task.token_budget
                        or self.config.agent.default_token_budget)
        time_budget = (task.time_budget
                       or self.config.agent.default_time_budget)
        used = task.metadata.get("_tokens_used", 0)
        elapsed = time.monotonic() - task.metadata.get(
            "_started_at", time.monotonic())
        warn_ratio = self.config.agent.budget_warn_ratio
        # 80% 告警（每任务每类一次）
        if (not task.metadata.get("_budget_warned")
                and used >= token_budget * warn_ratio):
            task.metadata["_budget_warned"] = True
            self.metrics.inc("budget_warnings")
            pct = int(used / token_budget * 100) if token_budget else 0
            self._emit("budget_warning", task_id=task.id, kind="token",
                       used=used, budget=token_budget, pct=pct)
            self._decision.record(
                "budget.warn", "agent.budget_warn_ratio", warn_ratio,
                f"任务 {task.id} token 预算 {used}/{token_budget}"
                f"（{pct}%，达到告警阈值）",
            )
        if (not task.metadata.get("_time_warned")
                and elapsed >= time_budget * warn_ratio):
            task.metadata["_time_warned"] = True
            self.metrics.inc("budget_warnings")
            pct = int(elapsed / time_budget * 100) if time_budget else 0
            self._emit("budget_warning", task_id=task.id, kind="time",
                       used=int(elapsed), budget=int(time_budget),
                       pct=pct)
            self._decision.record(
                "budget.warn", "agent.budget_warn_ratio", warn_ratio,
                f"任务 {task.id} 时间预算 {elapsed:.0f}s/{time_budget:.0f}s"
                f"（{pct}%，达到告警阈值）",
            )
        # token 耗尽：先尝试借用低优先级预算
        borrowed = task.metadata.get("_budget_borrowed", 0)
        if used > token_budget + borrowed:
            deficit = used - (token_budget + borrowed)
            got = self._borrow_budget(task, deficit)
            borrowed = task.metadata.get("_budget_borrowed", 0)
        if used > token_budget + borrowed:
            report = self._budget_report(
                task, used, token_budget, elapsed, time_budget, borrowed)
            raise TaskBudgetExceeded(
                "token", used, token_budget, report=report,
                borrowed=borrowed)
        if elapsed > time_budget:
            report = self._budget_report(
                task, used, token_budget, elapsed, time_budget, borrowed)
            raise TaskBudgetExceeded(
                "time", elapsed, time_budget, report=report,
                borrowed=borrowed)

    async def _maybe_preempt(self, task: Task) -> None:
        """安全点抢占检查：存在更高优先级的就绪任务时暂停当前任务。

        暂停任务进入 PAUSED，由调度器在高优先级任务完成后恢复（进阶 2.1）。
        """
        if not self.config.agent.preemption_enabled:
            return
        if self.scheduler.pending_higher(task):
            self.metrics.inc("preemptions")
            self._decision.record(
                "task.preempted", "agent.preemption_enabled", True,
                f"任务 {task.id}（priority={task.priority}）被更高优先级任务抢占"
                f"，暂停等待恢复",
            )
            raise TaskPreempted()

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

    async def _summarize_observation(self, tool_name: str, result,
                                     task: Optional[Task] = None) -> str:
        """工具输出三级处理（方案 2.2）：全文 / 头尾+存档 / LLM 摘要+关键行+存档。

        - <= output_truncate：全文保留；
        - <= 20_000：保留头 500 + 尾 500，完整输出存档 logs/outputs/；
        - > 20_000：LLM 生成摘要 + 正则提取错误/警告行，完整输出存档。
        """
        text = (f"[{tool_name}] 失败: {result.error}"
                if result.error else f"[{tool_name}] {result.output}")
        truncate = int(getattr(self.config.context, "output_truncate", 2000))
        if len(text) <= truncate:
            return text

        raw = result.error or result.output or ""
        lines = raw.splitlines()
        ref = self._archive_tool_output(tool_name, raw)
        key_lines = self._extract_key_output_lines(raw)
        self._decision.record(
            "output.truncated", "context.output_truncate", truncate,
            f"{tool_name} 输出 {len(text)} 字符/{len(lines)} 行已压缩"
            f"{'（原始输出: ' + ref + '）' if ref else ''}",
        )

        if len(text) > 20_000:
            summary = await self._llm_summarize_output(
                tool_name, raw, task)
            if summary:
                body = f"摘要: {summary}"
            else:
                body = ("关键行: " + ("；".join(key_lines[:12])
                                      if key_lines else "(无法提取关键行)"))
        else:
            head = text[:500]
            tail = text[-500:]
            body = (f"关键行: {'；'.join(key_lines[:8]) if key_lines else '（无）'}\n"
                    f"开头: {head}\n结尾: {tail}")
        ref_note = f"，原始输出: {ref}" if ref else "（原始输出存档失败）"
        return (f"{text[:200]}…(共 {len(text)} 字符/{len(lines)} 行，"
                f"已压缩{ref_note})\n{body}")

    def _archive_tool_output(self, tool_name: str, raw: str) -> str:
        """长工具输出存档到 logs/outputs/，返回相对路径（失败返回空串）。"""
        try:
            out_dir = Path(self.config.context.archive_dir).parent / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time())}_{tool_name.replace(':', '_')}_{len(raw)}.txt"
            (out_dir / name).write_text(raw, encoding="utf-8", errors="replace")
            return str(out_dir / name)
        except OSError as e:
            logger.warning("工具输出存档失败: %s", e)
            return ""

    @staticmethod
    def _extract_key_output_lines(raw: str, max_lines: int = 12) -> List[str]:
        """用正则提取错误/警告/关键状态行（保留文件路径与行号细节）。"""
        patterns = [
            re.compile(r"(?i)(error|traceback|exception|failed|failure|fatal|killed|timeout)"),
            re.compile(r"(?i)(warning|warn|deprecat)"),
            re.compile(r"(?i)(assert|expected|actual|got|exit\s*code|status\s*[:=])"),
        ]
        seen: set = set()
        key_lines: List[str] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s in seen:
                continue
            if any(p.search(ln) for p in patterns):
                seen.add(s)
                key_lines.append(s[:300])
            if len(key_lines) >= max_lines:
                break
        return key_lines

    async def _llm_summarize_output(self, tool_name: str, raw: str,
                                     task: Optional[Task] = None) -> str:
        """超长输出用 LLM 生成摘要；MockLLM（脚本化测试）不消费响应。"""
        try:
            from agent.llm import MockLLM
            if isinstance(self.llm, MockLLM):
                return ""
            prompt = (
                f"以下是工具 {tool_name} 的输出（可能截断）。"
                f"请提取关键信息并输出 200 字以内要点，"
                f"必须保留所有错误、警告、文件路径与行号。\n\n"
                f"{raw[:30000]}"
            )
            prompt_tokens = estimate_tokens(prompt)
            self.metrics.record_token_usage(prompt_tokens)
            self._add_task_tokens(task, prompt_tokens)
            resp = await self.llm.complete([{"role": "user", "content": prompt}])
            resp_tokens = estimate_tokens(resp)
            self.metrics.record_token_usage(resp_tokens)
            self._add_task_tokens(task, resp_tokens)
            return (resp or "").strip()[:800]
        except Exception as e:
            logger.warning("输出摘要生成失败: %s", e)
            return ""

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
                memory_text, cf_count = prepend_warnings(
                    hits, self.memory.format_context(hits))
                if cf_count:
                    self._decision.record(
                        "counterfactual.injected", "agent.counterfactual_enabled",
                        self.config.agent.counterfactual_enabled,
                        f"任务级注入 {cf_count} 条反事实警告",
                    )
                self.prompt_builder.set_memory(memory_text)
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

    async def _plan_with_context(self, prompt: str) -> List[Task]:
        """带项目上下文的规划调用：仅向支持新参数的 Planner 传入注入内容。"""
        import inspect
        try:
            params = set(inspect.signature(self.planner.plan).parameters)
        except (TypeError, ValueError):
            params = set()
        kwargs = {}
        if "call_graph" in params:
            kwargs["call_graph"] = self._project_ctx.call_graph
        if "project_context" in params:
            kwargs["project_context"] = self._project_ctx.profile_text
        return await self.planner.plan(prompt, **kwargs)

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
                    f"技能 {skill.name} v{skill.version}"
                    f"（{len(skill.steps)} 步）命中激活",
                )
                # 阶段二 2.1：记录技能使用历史（版本管理）
                self.skill_library.record_usage(
                    skill.name, skill.version, "activated",
                    note=str(prompt)[:120])
            # 阶段二 2.2：多技能按管道串联（前一个产出 -> 后一个输入），
            # 步骤 when 条件按项目上下文求值
            plan = self.skill_library.expand_pipeline(
                matched, prompt, files=ctx.files, deps=ctx.deps)
            self._emit("skills_activated",
                       skills=[s.name for s in matched], total=len(plan))
            return plan
        except Exception as e:
            logger.warning("技能工作流展开失败，回退 LLM 规划: %s", e)
            return []

    def save_skill(self, name: str, description: str,
                   tasks: Optional[List[Task]] = None) -> str:
        """把最近执行的技能步骤保存为新技能（阶段二 2.3）。

        返回落盘后的 YAML 路径；技能可被 SkillLibrary 热加载立即发现。
        """
        from agent.context.skill_author import SkillAuthor
        if tasks is None:
            tasks = list(self.scheduler.dag.all()) if self.scheduler else []
        author = SkillAuthor(
            skills_dir=self.config.skills.dir,
            registry_file=self.config.skills.registry_file,
            decision_logger=self._decision,
        )
        trajectory = SkillAuthor.trajectory_from_tasks(tasks)
        skill = author.from_trajectory(name, description, trajectory)
        return str(author.save(skill))

    async def save_skill_from_natural_language(self, name: str,
                                               description: str,
                                               prompt: str) -> str:
        """用 LLM 把自然语言/最近轨迹生成为技能并落盘（阶段二 2.3）。"""
        from agent.context.skill_author import SkillAuthor
        author = SkillAuthor(
            skills_dir=self.config.skills.dir,
            registry_file=self.config.skills.registry_file,
            llm=self.llm,
            decision_logger=self._decision,
        )
        tasks = list(self.scheduler.dag.all()) if self.scheduler else []
        trajectory = SkillAuthor.trajectory_from_tasks(tasks)
        skill = await author.from_llm(name, description, prompt, trajectory)
        return str(author.save(skill))

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
        """释放 MCP 连接、Docker 沙箱与后台任务等资源。"""
        await self.mcp.disconnect_all()
        self._mcp_connected = False
        if self.docker.running:
            await self.docker.stop()
        bg = getattr(self, "_background_tasks", None)
        if bg is not None:
            try:
                await bg.manager.shutdown_all(graceful=False)
            except Exception:
                logger.exception("后台任务清理失败")

    def _collect_final(self, completed: List[Task], failed: List[Task],
                       skipped: Optional[List[Task]] = None,
                       prompt: str = "") -> str:
        """汇总最终答案；被跳过的步骤（方案 1.2）附在报告末尾并说明原因。"""
        answers = [t.result for t in completed if isinstance(t.result, str) and t.result]
        skipped = skipped or []
        note = ""
        if skipped:
            note = "\n\n[已跳过步骤]\n" + "\n".join(
                f"- {t.instruction}: {t.error or '重试耗尽'}"
                for t in skipped[:5]
            )
        if answers:
            return "\n".join(answers) + note
        if failed:
            return (f"任务失败: {'; '.join(t.error or t.instruction for t in failed[:3])}"
                    + note)
        return f"（未产生最终结果）: {prompt}" + note

    def _record_config_fallbacks(self) -> None:
        """把配置加载阶段的降级信息写入决策日志（跨 run 去重）。

        对应方案 1.4：配置容错 —— 降级原因要能被用户与分析脚本看见。
        """
        try:
            from agent.config import CONFIG_FALLBACKS
        except Exception:
            return
        seen = getattr(self, "_config_fallbacks_recorded", set())
        for note in CONFIG_FALLBACKS:
            key = (note.get("module"), note.get("path"), note.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            self._decision.record(
                "config.fallback", f"{note.get('module')}.path",
                note.get("path", ""),
                f"配置降级为默认值: {note.get('reason', '')}",
            )
        self._config_fallbacks_recorded = seen

    # ---- 进阶 1.2：反事实分析（失败后归因 + 写长期记忆） ----
    def _maybe_counterfactual(self, prompt: str, result: LoopResult) -> None:
        """任务失败后做反事实分析并写入长期记忆（非阻塞）。

        分析结果供后续相似任务检索时以 [反事实警告] 注入 Prompt。
        """
        if not self.config.agent.counterfactual_enabled:
            return
        if result.phase != AgentPhase.FAILED:
            return
        try:
            from agent.counterfactual import analyze_failure, store_lesson
            doc = self.archive.build(
                prompt, self.events, self.tracer.snapshot(),
                self._decision.records(), self.metrics.snapshot(), result,
            )
            analysis = analyze_failure(doc)
            stored = store_lesson(self.memory, analysis, prompt=prompt)
            self._decision.record(
                "counterfactual.stored", "agent.counterfactual_enabled",
                self.config.agent.counterfactual_enabled,
                f"反事实分析: 归因={analysis['category']}，"
                f"转折点={str(analysis.get('turning_point', ''))[:80]}，"
                f"{'写入长期记忆' if stored else '已存在（去重跳过）'}"
                f"（{len(analysis.get('alternatives', []))} 个备选方案）",
            )
            self._emit("counterfactual_stored",
                       category=analysis.get("category"),
                       stored=stored,
                       turning_point=analysis.get("turning_point", ""))
        except Exception as e:
            logger.warning("反事实分析/存储失败（不影响任务）: %s", e)

    # ---- 进阶 3.1：自动测试生成（改动无测试覆盖时补齐 test_*.py） ----
    async def _maybe_auto_testgen(self, name: str, params: Dict[str, Any],
                                  task: Task) -> Optional[str]:
        """file_ops 写入 .py 且无测试覆盖时自动生成测试（非阻塞）。

        返回生成测试的工作区相对路径（未生成返回 None），供回归检测
        排除“本步自动生成”的测试，避免把生成器自身的缺陷误判为代码回归。
        """
        if name != "file_ops":
            return
        action = str(params.get("action", ""))
        if action not in ("write", "edit", "append"):
            return
        path = str(params.get("path", "")).strip()
        if not path.endswith(".py"):
            return
        try:
            from agent.testgen import generate_and_write, needs_tests
            workspace = self.config.sandbox.workspace
            if not needs_tests(workspace, path):
                self._decision.record(
                    "testgen.skip", "agent.auto_testgen", True,
                    f"已有测试覆盖或非源码文件: {path}",
                )
                return
            content = str(params.get("content", ""))
            if not content.strip() and action in ("edit", "append"):
                target = Path(workspace) / path
                if target.exists():
                    content = target.read_text(encoding="utf-8")
            written = generate_and_write(workspace, path, content)
            if written is None:
                self._decision.record(
                    "testgen.skip", "agent.auto_testgen", True,
                    f"未生成测试（无公开符号）: {path}",
                )
                return
            test_path, targets = written
            root = Path(workspace).resolve()
            try:
                rel_test = str(Path(test_path).resolve().relative_to(root))
            except ValueError:
                rel_test = str(test_path)
            rel_test = rel_test.replace("\\", "/")
            self._decision.record(
                "testgen.generated", "agent.auto_testgen", True,
                f"为 {path} 生成 {targets} 个测试目标: {test_path}",
            )
            self._emit("testgen_generated", module=path,
                       test_file=test_path, targets=targets)
            if self.config.agent.mutation_check_enabled:
                await self._maybe_mutation_analysis(
                    path, test_path, content, task)
            if self.config.agent.auto_testgen_verify:
                asyncio.create_task(self._verify_testgen(path, test_path))
            return rel_test
        except Exception as e:
            logger.warning("自动测试生成失败（不影响任务）: %s", e)
            try:
                self._decision.record(
                    "testgen.fallback", "agent.auto_testgen", True,
                    f"自动测试生成降级: {str(e)[:100]}",
                )
            except Exception:
                pass

    async def _verify_testgen(self, module_path: str,
                              test_path: str) -> None:
        """生成后立即运行 pytest 验证（可选开关，失败只记录决策点）。"""
        try:
            from agent.testgen import verify_generated
            result = await verify_generated(
                self.config.sandbox.workspace, test_path)
            self._decision.record(
                "testgen.verify", "agent.auto_testgen_verify", True,
                f"生成的测试{'通过' if result and result.success else '失败'}: "
                f"{module_path} -> {test_path}",
            )
        except Exception as e:
            logger.warning("测试生成验证失败: %s", e)

    async def _maybe_regression_check(self, name: str,
                                      params: Dict[str, Any],
                                      task: Task,
                                      result: ToolResult,
                                      skip_test: Optional[str] = None,
                                      ) -> ToolResult:
        """阶段三 3.3：运行受影响测试，回归失败时把失败回写为该步骤结果。

        返回替换后的 ToolResult；无对应测试、测试无法运行（skip）或受影响
        测试恰为本步自动生成（skip_test 命中）时保持原结果，只记录回归决策
        点。回归失败通过结果回写触发"修复-重测"，重试耗尽后由 criticality
        决定失败/跳过，依赖步骤自动停止。
        """
        if name != "file_ops":
            return result
        action = str(params.get("action", ""))
        if action not in ("write", "edit", "append"):
            return result
        path = str(params.get("path", "")).strip()
        if not path.endswith(".py"):
            return result
        try:
            from agent.regression import (affected_test_path,
                                          classify_test_result)
            from agent.code.test_runner import run_tests
            workspace = self.config.sandbox.workspace
            test_path = affected_test_path(workspace, path)
            if not test_path:
                self._decision.record(
                    "regression.skip", "agent.regression_check_enabled", True,
                    f"无对应测试文件，跳过回归检测: {path}",
                )
                return result
            if skip_test and test_path == skip_test.replace("\\", "/"):
                self._decision.record(
                    "regression.skip", "agent.regression_check_enabled", True,
                    f"受影响测试由本步自动生成，不作为回归基线: {test_path}",
                )
                return result
            test_result = await run_tests(
                "pytest", test_path, workspace,
                timeout=self.config.agent.regression_timeout)
            signal = classify_test_result(test_result)
            if signal == "clean":
                self._decision.record(
                    "regression.clean", "agent.regression_check_enabled", True,
                    f"改动 {path} 后受影响测试通过: {test_path}",
                )
                self._emit("regression_clean", task_id=task.id,
                           module=path, test_file=test_path)
                return result
            if signal == "skip":
                self._decision.record(
                    "regression.skip", "agent.regression_check_enabled", True,
                    f"测试无法运行，跳过回归检测: "
                    f"{str(test_result.output or '')[:100]}",
                )
                self._emit("regression_skip", task_id=task.id,
                           module=path, test_file=test_path)
                return result
            summary = test_result.summary
            self._decision.record(
                "regression.detected", "agent.regression_check_enabled", True,
                f"回归检测失败: {path} -> {test_path}（{summary[:160]}）",
            )
            self._emit("regression_detected", task_id=task.id,
                       module=path, test_file=test_path,
                       summary=summary[:400])
            return ToolResult(
                success=False,
                output=f"[回归检测] 改动 {path} 后受影响测试失败：\n{summary}",
                error=f"[回归检测] 改动 {path} 后受影响测试失败：{summary[:200]}",
                elapsed_ms=test_result.duration_ms,
                metadata={"regression": True, "module": path,
                          "test_file": test_path},
            )
        except Exception as e:
            logger.warning("回归检测失败（不阻断任务）: %s", e)
            try:
                self._decision.record(
                    "regression.skip", "agent.regression_check_enabled", True,
                    f"回归检测降级跳过: {str(e)[:100]}",
                )
            except Exception:
                pass
            return result

    async def _maybe_mutation_analysis(self, module_path: str,
                                       test_path: str, content: str,
                                       task: Task) -> None:
        """阶段三 3.2：对刚生成的测试做变异检测，验证测试有效性。

        只对自动生成的测试运行（成本可控）；基线无法运行或无变异点时
        记录 mutation.skip，不阻断任务。
        """
        try:
            from agent.mutation import run_mutation_analysis
            workspace = self.config.sandbox.workspace
            analysis = await run_mutation_analysis(
                workspace, module_path, content, test_path,
                timeout=self.config.agent.regression_timeout,
                max_mutations=self.config.agent.mutation_max_ops,
            )
            if analysis.get("skipped"):
                self._decision.record(
                    "mutation.skip", "agent.mutation_check_enabled", True,
                    f"变异检测跳过: {str(analysis.get('reason', ''))[:100]}",
                )
                return
            score = float(analysis.get("score", 0.0))
            target = self.config.agent.mutation_target_rate
            survivors = analysis.get("survivors", []) or []
            passed = score >= target
            self._decision.record(
                "mutation.analyzed", "agent.mutation_check_enabled", True,
                f"变异检测率 {score:.0%}（{analysis.get('killed', 0)}/"
                f"{analysis.get('total', 0)}）"
                f"{'达标' if passed else '未达标，需补强测试'}："
                f"存活变异: {', '.join(survivors) if survivors else '无'}",
            )
            self._emit("mutation_analyzed", module=module_path,
                       total=analysis.get("total", 0),
                       killed=analysis.get("killed", 0),
                       score=score, survivors=survivors)
        except Exception as e:
            logger.warning("变异检测失败（不阻断任务）: %s", e)
