"""调度器 —— 基于 TaskDAG 的优先级拓扑调度。

对应设计第 3.2 节：
- 就绪任务按优先级弹出；
- 支持并发（max_concurrency 内 asyncio.gather）；
- 动态 spawn 新子任务实时合并。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

from agent.core.task import Task, TaskDAG, TaskStatus

logger = logging.getLogger("alpha-swe.scheduler")


class Scheduler:
    """包装 TaskDAG 的调度入口。"""

    def __init__(self, dag: Optional[TaskDAG] = None, max_concurrency: int = 1,
                 on_task_failed: Optional[Callable[[Task], None]] = None):
        self.dag = dag or TaskDAG()
        self.max_concurrency = max(max_concurrency, 1)
        self._worker: Optional[Callable[[Task], Awaitable[None]]] = None
        self._on_task_failed = on_task_failed
        self._on_snapshot: Optional[Callable[[], None]] = None

    def set_on_snapshot(self, callback: Callable[[], None]) -> None:
        """注册任务终结后的快照回调（方案 1.3：每步完成后落盘）。"""
        self._on_snapshot = callback

    def set_on_task_failed(self, callback: Callable[[Task], None]) -> None:
        """注册任务失败回调（技能步骤 fallback / 升级介入用）。"""
        self._on_task_failed = callback

    def set_worker(self, worker: Callable[[Task], Awaitable[None]]) -> None:
        """注册任务执行协程（由 AgentLoop 提供）。"""
        self._worker = worker

    # ---- 任务提交 ----
    def submit(self, task: Task) -> Task:
        """提交新任务（外部可直接用 Task）。"""
        if task.status == TaskStatus.IDLE:
            task.mark(TaskStatus.READY)
        return self.dag.add(task)

    def spawn(self, instruction: str, parent_id: Optional[str] = None,
              dependencies: Optional[List[str]] = None, priority: int = 0) -> Task:
        """Agent 在执行过程中动态拆分出子任务。"""
        task = self.dag.create_task(
            instruction=instruction,
            dependencies=dependencies,
            priority=priority,
            parent_id=parent_id,
        )
        task.mark(TaskStatus.READY)
        logger.info("spawn task=%s parent=%s deps=%s", task.id, parent_id, dependencies)
        return task

    def submit_plan(self, tasks: List[Task]) -> None:
        """批量提交规划结果：无依赖的任务直接 READY，有依赖的等待 promote。"""
        for t in tasks:
            self.dag.add(t)
        for t in tasks:
            if self.dag.dependencies_satisfied(t):
                t.mark(TaskStatus.READY)

    # ---- 调度 ----
    def ready(self) -> List[Task]:
        return self.dag.ready_tasks()

    def on_task_done(self, task: Task) -> None:
        """任务终结后提升依赖者；失败时触发失败回调（技能步骤决策点）。

        SKIPPED 视为终结且不阻塞后续步骤（方案 1.2）。
        """
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                           TaskStatus.SKIPPED):
            self.dag.promote_dependents(task.id)
            if self._on_snapshot is not None:
                try:
                    self._on_snapshot()
                except Exception:
                    logger.exception("快照回调异常")
            if task.status == TaskStatus.FAILED and self._on_task_failed is not None:
                try:
                    self._on_task_failed(task)
                except Exception:
                    logger.exception("任务失败回调异常: %s", task.id)

    async def run_to_completion(self) -> None:
        """主调度循环：弹出就绪任务并并行执行，直到 DAG 终结。

        等待状态：所有未终结任务都在 WAITING 时，等待 wake() 或新任务。
        """
        assert self._worker is not None, "必须先 set_worker()"
        wake_event = asyncio.Event()

        def _wake() -> None:
            wake_event.set()

        self.wake = _wake  # 供外部（后台任务/用户输入）唤醒

        while self.dag.pending():
            ready = self.ready()
            if ready:
                batch = ready[: self.max_concurrency]
                for t in batch:
                    t.mark(TaskStatus.RUNNING)
                await asyncio.gather(*(self._worker(t) for t in batch))
                for t in batch:
                    self.on_task_done(t)
                continue

            if not self.dag.pending():
                break
            if self.dag.has_waiting():
                # 挂起等待唤醒（用户输入、后台任务完成、中断注入）
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning("等待唤醒超时，重新检查就绪任务")
                for t in self.dag.all():
                    if t.status == TaskStatus.WAITING:
                        t.mark(TaskStatus.READY)
                wake_event.clear()
                continue

            # 依赖形成死环（理论上被规划器避免）
            logger.warning("调度停滞: 无就绪任务且无等待任务")
            break

    def wake(self) -> None:  # pragma: no cover - 占位，run_to_completion 会替换
        raise NotImplementedError