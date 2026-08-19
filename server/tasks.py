# -*- coding: utf-8 -*-
"""任务队列：异步子进程运行 Agent + 事件总线。

MVP 采用进程内 asyncio 队列（无外部依赖）：
- 每个任务一个子进程（``python -m agent run ...``），互相隔离；
- max_concurrency 限制并发数，超出排队；
- 支持取消：排队任务直接标记；运行中任务 terminate 子进程；
- 生命周期事件（queued/running/completed/failed/timeout/budget/cancelled）
  发布到 EventBus，供 SSE 推送。

后续可替换为 Celery/arq + Redis（见 docs/06 部署文档）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from server.config import ServerConfig
from server.store import Store, TaskRecord

logger = logging.getLogger("server.tasks")

DONE_EVENT = "done"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EventBus:
    """按 task_id 的事件订阅分发（asyncio.Queue）。"""

    _subscribers: Dict[str, List[asyncio.Queue]] = field(
        default_factory=dict)

    async def publish(self, task_id: str, event_type: str,
                      data: Dict[str, Any]) -> None:
        for q in list(self._subscribers.get(task_id, [])):
            q.put_nowait({"type": event_type, "data": data})
        if event_type in (DONE_EVENT, "error"):
            await self._close(task_id)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(task_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subscribers.pop(task_id, None)

    async def _close(self, task_id: str) -> None:
        for q in self._subscribers.pop(task_id, []):
            q.put_nowait({"type": DONE_EVENT, "data": {}})


def build_agent_command(record: TaskRecord, config: ServerConfig) -> List[str]:
    python = config.agent_python or sys.executable
    cmd = [python, "-X", "utf8", "-m", "agent", "run",
           record.instruction,
           "--config", record.config_path or config.config_path,
           "--workspace", record.workspace,
           "--output", "json",
           "--timeout", str(record.timeout)]
    if record.max_cost:
        cmd += ["--max-cost", str(record.max_cost)]
    if record.max_tokens:
        cmd += ["--max-tokens", str(record.max_tokens)]
    if not config.docker:
        cmd += ["--disable-docker"]
    return cmd


def extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class TaskQueue:
    """异步任务队列 + 事件总线。"""

    def __init__(self, store: Store, config: ServerConfig,
                 runner: Optional[Callable[[TaskRecord], Any]] = None):
        self.store = store
        self.config = config
        self.bus = EventBus()
        self._runner = runner or self._run_subprocess
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._procs: Dict[str, subprocess.Popen] = {}
        self._running: set = set()
        self._started = False

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        n = max(1, self.config.max_concurrency)
        self._workers = [asyncio.create_task(self._worker(i))
                         for i in range(n)]
        logger.info("任务队列启动，并发数=%d", n)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except Exception:
                pass
        self._procs.clear()

    # ---------- 提交 / 取消 ----------
    def submit(self, task_id: str) -> None:
        self._queue.put_nowait(task_id)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._procs

    async def cancel(self, task_id: str) -> bool:
        record = self.store.get_task(task_id)
        if record is None:
            return False
        if task_id in self._procs or task_id in self._running:
            proc = self._procs.pop(task_id, None)
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.store.mark_cancelled(task_id)
            await self.bus.publish(task_id, "cancelled",
                                   {"id": task_id, "reason": "用户取消"})
            await self.bus.publish(task_id, DONE_EVENT, {})
            return True
        if record.status == "queued":
            self.store.mark_cancelled(task_id)
            await self.bus.publish(task_id, "cancelled",
                                   {"id": task_id, "reason": "排队中取消"})
            await self.bus.publish(task_id, DONE_EVENT, {})
            return True
        return False

    # ---------- 工作线程 ----------
    async def _worker(self, idx: int) -> None:
        logger.info("worker-%d 启动", idx)
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("任务执行异常: %s", task_id)
                self.store.update_task(
                    task_id, status="failed", error=f"队列异常: {e}",
                    finished_at=_now())
                await self.bus.publish(task_id, "error",
                                       {"id": task_id, "error": str(e)})
            finally:
                self._queue.task_done()

    async def _run_one(self, task_id: str) -> None:
        record = self.store.get_task(task_id)
        if record is None:
            await self.bus.publish(task_id, "error",
                                   {"error": "任务不存在"})
            return
        if record.cancelled or record.status == "cancelled":
            return
        self.store.update_task(task_id, status="running",
                               started_at=_now())
        self._running.add(task_id)
        await self.bus.publish(task_id, "running",
                               {"id": task_id,
                                "instruction": record.instruction[:200]})
        try:
            payload, error, status = await self._runner(record)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            payload, error, status = {}, f"运行异常: {e}", "failed"
        finally:
            self._running.discard(task_id)

        # 取消后不覆盖终态
        fresh = self.store.get_task(task_id)
        if fresh is not None and fresh.cancelled:
            return

        if payload:
            self.store.update_task(
                task_id, status=status or "completed", error=error,
                exit_code=int(payload.get("exit_code") or 0),
                result_json=json.dumps(payload, ensure_ascii=False),
                finished_at=_now())
        else:
            self.store.update_task(
                task_id, status=status or "failed", error=error,
                exit_code=1, finished_at=_now())
        await self.bus.publish(task_id, status or "completed",
                               {"id": task_id, "error": error,
                                "payload": payload})
        await self.bus.publish(task_id, DONE_EVENT, {})

    # ---------- 默认子进程 runner ----------
    async def _run_subprocess(self, record: TaskRecord
                              ) -> tuple[Dict[str, Any], str, str]:
        cmd = build_agent_command(record, self.config)
        logger.info("运行任务 %s: %s", record.id, " ".join(cmd))
        cfg_path = Path(self.config.config_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cfg_path.resolve().parent) if cfg_path.exists() else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._procs[record.id] = proc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=record.timeout + 60)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
            self._procs.pop(record.id, None)
            return {}, f"Agent 执行超时（>{record.timeout:g}s）", "timeout"
        finally:
            self._procs.pop(record.id, None)
        out = (stdout or b"").decode("utf-8", "replace")
        err = (stderr or b"").decode("utf-8", "replace")
        payload = extract_json_payload(out) or {}
        exit_code = int(proc.returncode or 1)
        if payload:
            status = str(payload.get("status") or
                         ("completed" if exit_code == 0 else "failed"))
            error = str(payload.get("error") or "") or None
            return payload, error, status
        if exit_code != 0:
            return {}, (err or f"退出码 {exit_code}").strip()[:2000], "failed"
        return {}, "Agent 未返回 JSON 结果", "failed"
