"""Worker Agents —— 对应设计第 8 节。

每个 Worker 是独立的 Agent 实例：拥有自己的 AgentLoop（上下文/记忆）、角色化
System Prompt 与受限工具集；执行后把产出（文件、报告）发布到黑板。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from agent.config import AppConfig, WorkerRoleConfig
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop, LoopResult
from agent.core.task import Task
from agent.llm import BaseLLM, MockLLM
from agent.mcp.manager import MCPManager
from agent.multiagent.blackboard import Artifact, Blackboard
from agent.prompt.builder import PromptBuilder
from agent.prompt import templates
from agent.tools.fileio import FileIOTool
from agent.tools.manager import ToolManager
from agent.tools.terminal import TerminalTool

logger = logging.getLogger("alpha-swe.multiagent.workers")


class _SingleTaskPlanner:
    """Worker 的 AgentLoop 只执行一个任务：不再消耗一次 LLM 响应做规划。"""

    async def plan(self, prompt: str, context: str = "") -> List[Task]:
        return [Task(id="t0", instruction=prompt)]


@dataclass
class WorkerResult:
    """Worker 单次任务执行结果。"""
    ok: bool
    output: str = ""
    error: str = ""
    artifacts: Artifact = field(default_factory=dict)
    rounds: int = 0
    task_id: str = ""


class WorkerAgent:
    """角色化 Worker：用受限工具集 + 角色 Prompt 运行一个 AgentLoop。"""

    def __init__(
        self,
        role: WorkerRoleConfig,
        *,
        config: Optional[AppConfig] = None,
        llm: Optional[BaseLLM] = None,
        blackboard: Optional[Blackboard] = None,
        decision_logger: Optional[DecisionLogger] = None,
    ) -> None:
        self.role = role
        self.config = config or AppConfig()
        self.llm = llm or MockLLM()
        self.blackboard = blackboard or Blackboard()
        self.decision_logger = decision_logger
        self.history: List[WorkerResult] = []

    # ---- 执行 ----
    async def execute_task(self, task: Task, extra_context: str = "") -> WorkerResult:
        instruction = task.instruction
        if extra_context:
            instruction = f"{instruction}\n\n{extra_context}"
        loop = self._build_loop()
        try:
            result = await loop.run(instruction)
        finally:
            await loop.close()

        artifact = self._collect_artifact(loop, result)
        self.blackboard.publish(f"task:{task.id}", artifact)
        wr = WorkerResult(
            ok=result.ok,
            output=result.final_answer,
            error="",
            artifacts=artifact,
            rounds=result.total_rounds,
            task_id=task.id,
        )
        self.history.append(wr)
        logger.info("[worker:%s] task=%s ok=%s", self.role.name, task.id, result.ok)
        return wr

    # ---- 内部 ----
    def _build_loop(self) -> AgentLoop:
        system_template = (
            self.role.system_prompt.strip() + "\n\n" + templates.SYSTEM_TEMPLATE
            if self.role.system_prompt.strip()
            else templates.SYSTEM_TEMPLATE
        )
        tools = self._role_tools()
        prompt_builder = PromptBuilder(
            tool_schemas=tools.schemas(),
            system_template=system_template,
        )
        config = self.config
        try:
            # Worker 是单任务执行器：团队级工作流由 Orchestrator 展开，这里禁用
            config = config.model_copy(deep=True)
            config.skills.enabled = False
            config.plugin.enabled = False
        except Exception:
            pass
        return AgentLoop(
            config=config,
            llm=self.llm,
            planner=_SingleTaskPlanner(),
            prompt_builder=prompt_builder,
            tools=tools,
            mcp_manager=MCPManager(servers=[]),  # Worker 不直连 MCP
            decision_logger=self.decision_logger,
        )

    def _role_tools(self) -> ToolManager:
        """按角色构建工具集：read_only 角色只暴露只读文件操作 + 只读命令白名单。"""
        manager = ToolManager()
        wanted = set(self.role.tools)
        read_only = bool(self.role.read_only)
        if "terminal_execute" in wanted:
            manager.register(TerminalTool(read_only=read_only))
        if "file_ops" in wanted or "file_search" in wanted:
            manager.register(FileIOTool(read_only=read_only))
        return manager

    def _collect_artifact(self, loop: AgentLoop, result: LoopResult) -> Artifact:
        """扫描循环事件中的 file_ops 写入，记录最终文件内容。"""
        files: Dict[str, str] = {}
        for ev in loop.events:
            if ev.get("type") != "tool_call":
                continue
            data = ev.get("data", {})
            if data.get("tool") != "file_ops":
                continue
            params = data.get("params") or {}
            if params.get("action") not in ("write", "append", "edit"):
                continue
            path = str(params.get("path", "")).strip()
            if not path:
                continue
            full = Path(self.config.sandbox.workspace) / path
            try:
                files[path] = full.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                files[path] = "(读取失败)"
        return {
            "files": files,
            "output": result.final_answer,
            "ok": result.ok,
            "rounds": result.total_rounds,
        }


__all__ = ["WorkerAgent", "WorkerResult"]
