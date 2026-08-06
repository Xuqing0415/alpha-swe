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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.config import AppConfig, load_config, load_mcp_config
from agent.context.manager import ContextManager
from agent.core.scheduler import Scheduler
from agent.core.state import AgentPhase, StateMachine
from agent.core.task import Task, TaskDAG, TaskStatus
from agent.llm import BaseLLM, build_llm
from agent.mcp.manager import MCPManager
from agent.memory.factory import build_memory
from agent.memory.store import MemoryStore
from agent.memory.summarizer import ExperienceSummarizer
from agent.parser.parser import Parser
from agent.planner.planner import Planner
from agent.prompt.builder import PromptBuilder
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext
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
    ):
        self.config = config or load_config()
        self.state = StateMachine()
        self.cancel_event = asyncio.Event()
        self.events: List[Dict[str, Any]] = []
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._output_callback = output_callback
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 默认运行态；pause() 后清空

        # 组件装配（缺省即用默认实现，便于测试注入）
        self.llm = llm or build_llm(self.config.llm)
        self.sandbox = sandbox or SandboxPolicy(workspace=self.config.sandbox.workspace)
        self.tools = tools or self._default_tools()
        self._tool_enabled = getattr(self, "_tool_enabled", None)
        self.planner = planner or Planner(llm=self.llm)
        self.parser = parser or Parser(max_retries=self.config.agent.max_retries)
        self.context = context_manager or ContextManager(
            keep_recent_rounds=self.config.agent.keep_recent_rounds,
            token_threshold=self.config.agent.token_threshold,
            max_token_limit=self.config.agent.max_token_limit,
        )
        self.memory = memory or build_memory(self.config.memory)
        self.summarizer = summarizer or ExperienceSummarizer(
            llm=self.llm, enabled=self.config.memory.auto_experience
        )
        self.mcp = mcp_manager or MCPManager.from_config(
            load_mcp_config(), self.config.mcp
        )
        self._mcp_connected = False
        self.prompt_builder = prompt_builder or PromptBuilder(
            tool_schemas=self.tools.schemas(enabled=self._tool_enabled)
        )
        dag = TaskDAG()
        self.scheduler = scheduler or Scheduler(
            dag=dag, max_concurrency=self.config.agent.max_concurrency
        )
        self.scheduler.set_worker(self._execute_task)

        # 沙箱工作目录
        os.makedirs(self.config.sandbox.workspace, exist_ok=True)

    # ---- 默认工具集 ----
    def _default_tools(self) -> ToolManager:
        manager = ToolManager(policy=self.sandbox)
        manager.register(TerminalTool())
        manager.register(FileIOTool())
        raw = self.config.tools.model_dump()
        self._tool_enabled = {name: cfg["enabled"] for name, cfg in raw.items()}
        return manager

    # ---- 事件 ----
    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """注册实时事件回调（TUI/观测面板用）。callback 在事件循环内被同步调用。"""
        self._subscribers.append(callback)

    def _emit(self, event_type: str, **data: Any) -> None:
        record = {"type": event_type, "data": data}
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

        # MCP 集成：连接服务器 -> 工具合并 -> 资源注入（失败容忍）
        if self.config.mcp.enabled and not self._mcp_connected:
            connected = await self.mcp.connect_all()
            if connected:
                await self._register_mcp_tools()
                await self._load_mcp_resources(prompt)
            self._mcp_connected = True

        # 技能/插件上下文 + 长期记忆注入
        skill = self.context.build_skill_context(prompt)
        if skill:
            self.prompt_builder.set_skill(skill)
        memory_hits = self.memory.retrieve(prompt, top_k=self.config.memory.top_k)
        memory_text = self.memory.format_context(memory_hits)
        if memory_text:
            self.prompt_builder.set_memory(memory_text)

        # 规划 -> READY
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
            return LoopResult(phase=AgentPhase.FAILED, tasks=plan, events=self.events)

        failed = [t for t in self.scheduler.dag.all() if t.status == TaskStatus.FAILED]
        completed = [t for t in self.scheduler.dag.all() if t.status == TaskStatus.COMPLETED]
        final = self._collect_final(completed, failed, prompt)

        phase = AgentPhase.COMPLETED if not failed else AgentPhase.FAILED
        self.state.transition(phase)
        self._emit("run_done", phase=phase.value, final_answer=final)
        return LoopResult(
            final_answer=final,
            phase=phase,
            tasks=plan,
            total_rounds=sum(t.round_count for t in plan),
            events=self.events,
        )

    # ---- 单任务 ReAct ----
    async def _execute_task(self, task: Task) -> None:
        """每个任务的内部 ReAct 循环：Think -> Act -> Observe -> Parse。"""
        task.mark(TaskStatus.RUNNING)
        self._emit("task_start", task_id=task.id, instruction=task.instruction)
        self._load_task_memory(task)
        parse_failures = 0

        try:
            while task.round_count < self.config.agent.max_rounds:
                await self._checkpoint(task)
                task.round_count += 1

                # 上下文自动压缩（对应设计第 11 节）
                if self.context.should_compact(task.history):
                    summary = self.context.compact(task.history)
                    if summary:
                        task.history.insert(0, {"role": "system", "content": summary})

                upstream = self._upstream_tasks(task)
                messages = self.prompt_builder.build(task, upstream)

                resp = await self.llm.complete(messages)
                parsed = self.parser.parse(resp)
                task.history.append({"role": "assistant", "content": resp})

                if parsed.action_type == "think":
                    self._emit("think", task_id=task.id, content=parsed.content[:300])
                    continue

                if parsed.action_type == "tool_call":
                    result = await self.tools.execute(
                        parsed.tool_name, parsed.params,
                        ExecutionContext(
                            workspace=self.config.sandbox.workspace,
                            task_id=task.id,
                            instruction=task.instruction,
                            interrupt_event=self.cancel_event,
                            output_callback=self._output_callback,
                        ),
                    )
                    obs = self._summarize_observation(parsed.tool_name, result)
                    task.history.append({"role": "observation", "content": obs})
                    self._emit("tool_call", task_id=task.id, tool=parsed.tool_name,
                               params=parsed.params, success=result.success,
                               output=obs[:300])
                    self._index_code(parsed.params, result)
                    if result.metadata.get("waiting"):
                        task.mark(TaskStatus.WAITING)  # 挂起，释放控制权
                        return
                    continue

                if parsed.action_type == "final_answer":
                    task.mark(TaskStatus.COMPLETED, result=parsed.content)
                    self._emit("task_done", task_id=task.id, ok=True)
                    await self._remember_experience(task)
                    return

                # 解析失败：反馈重试（最多 max_retries 次）
                parse_failures += 1
                if parse_failures >= self.parser.max_retries:
                    task.mark(TaskStatus.FAILED,
                              error=f"输出解析失败 {parse_failures} 次: {parsed.error}")
                    self._remember_error(task)
                    return
                task.history.append({
                    "role": "user",
                    "content": self.parser.retry_feedback(parsed, parse_failures),
                })

            task.mark(TaskStatus.FAILED, error=f"超过最大轮数 {self.config.agent.max_rounds}")
            self._remember_error(task)
        except TaskInterrupted:
            task.mark(TaskStatus.READY)  # 中断后回到就绪，等待重新调度
            self._emit("task_interrupted", task_id=task.id)
        except Exception as e:
            logger.exception("任务执行异常: %s", task.id)
            task.mark(TaskStatus.FAILED, error=str(e))
            self._remember_error(task)

    # ---- 工具 ----
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
        """任务完成后生成经验摘要并写入长期记忆（设计 7.2 节）。"""
        try:
            summary = await self.summarizer.summarize_task(task)
            if summary:
                self.memory.remember_experience(summary)
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
                          "instruction": task.instruction[:200]},
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
        """按任务指令检索相关记忆并注入 Prompt（设计 7.3 节）。"""
        try:
            hits = self.memory.search(task.instruction, top_k=self.config.memory.top_k)
            if hits:
                self.prompt_builder.set_memory(self.memory.format_context(hits))
        except Exception as e:
            logger.warning("任务记忆检索失败: %s", e)

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
        """释放 MCP 连接等资源。"""
        await self.mcp.disconnect_all()
        self._mcp_connected = False

    def _collect_final(self, completed: List[Task], failed: List[Task], prompt: str) -> str:
        answers = [t.result for t in completed if isinstance(t.result, str) and t.result]
        if answers:
            return "\n".join(answers)
        if failed:
            return f"任务失败: {'; '.join(t.error or t.instruction for t in failed[:3])}"
        return f"（未产生最终结果）: {prompt}"