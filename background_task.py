"""第三关：Background Tasks 异步非阻塞管理
使用 ThreadPoolExecutor 实现后台任务执行，支持轮询状态检查、超时强制 kill、僵尸进程清理。
"""
import threading
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("alpha-swe.background")


@dataclass
class BackgroundTask:
    """后台任务"""
    task_id: str
    name: str
    future: Optional[Future] = None
    status: str = "running"  # running/completed/failed/timeout/cancelled
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    result: Any = None
    error: Optional[str] = None
    timeout: float = 600.0
    start_time: float = field(default_factory=time.time)


class BackgroundTaskManager:
    """后台任务管理器——支持异步执行、超时强制 kill、僵尸清理"""

    def __init__(self, max_workers: int = 4, default_timeout: float = 600.0,
                 cleanup_interval: float = 10.0):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.tasks: Dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._running = True
        self.default_timeout = default_timeout

        # 启动清理线程（定期检查超时和僵尸任务）
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_interval = cleanup_interval
        self._cleanup_thread.start()

    def submit(self, fn: Callable, task_name: str = "", timeout: float = None) -> str:
        """提交后台任务，返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        task = BackgroundTask(
            task_id=task_id,
            name=task_name or f"task_{task_id}",
            timeout=timeout or self.default_timeout
        )
        # 先注册再提交，避免瞬时完成的任务丢失状态更新（竞态）
        with self._lock:
            self.tasks[task_id] = task
        try:
            task.future = self.executor.submit(self._wrapped_execute, task_id, fn)
        except Exception:
            with self._lock:
                self.tasks.pop(task_id, None)
            raise

        logger.info(f"[Background] Task {task_id} submitted: {task.name} (timeout={task.timeout}s)")
        return task_id

    def _wrapped_execute(self, task_id: str, fn: Callable) -> Any:
        """包装执行，捕获异常"""
        try:
            result = fn()
            with self._lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = "completed"
                    self.tasks[task_id].result = result
            logger.info(f"[Background] Task {task_id} completed")
            return result
        except Exception as e:
            with self._lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = "failed"
                    self.tasks[task_id].error = str(e)
            logger.error(f"[Background] Task {task_id} failed: {e}")
            raise

    def get_status(self, task_id: str) -> Optional[str]:
        """获取任务状态"""
        with self._lock:
            task = self.tasks.get(task_id)
            return task.status if task else None

    def get_result(self, task_id: str, timeout: float = 0) -> Optional[Any]:
        """获取任务结果（阻塞）"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return None

        if task.future is None:
            return None
        try:
            return task.future.result(timeout=timeout)
        except Exception:
            return None

    def wait(self, task_id: str, poll_interval: float = 3.0, timeout: float = 600.0) -> Optional[Any]:
        """轮询等待任务完成"""
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_status(task_id)
            if status == "completed":
                return self.get_result(task_id)
            elif status == "failed":
                with self._lock:
                    task = self.tasks.get(task_id)
                    if task:
                        logger.error(f"[Background] Task {task_id} failed: {task.error}")
                return None
            elif status in ("timeout", "cancelled"):
                with self._lock:
                    task = self.tasks.get(task_id)
                    detail = task.error if task and task.error else status
                logger.warning(f"[Background] Task {task_id} {status}: {detail}")
                return None
            elif status is None:
                logger.warning(f"[Background] Task {task_id} not found")
                return None

            # 轮询间隔
            time.sleep(poll_interval)
            logger.debug(f"[Background] Task {task_id} is running... (polling)")

        logger.warning(f"[Background] Task {task_id} timeout after {timeout}s")
        return None

    def cancel(self, task_id: str) -> bool:
        """取消任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task:
                if task.future is not None:
                    task.future.cancel()
                task.status = "cancelled"
                return True
        return False

    def list_tasks(self) -> list:
        """列出所有任务"""
        with self._lock:
            return [
                {
                    "task_id": t.task_id,
                    "name": t.name,
                    "status": t.status,
                    "created_at": t.created_at
                }
                for t in self.tasks.values()
            ]

    def shutdown(self, wait: bool = True):
        """关闭线程池"""
        self._running = False
        self.executor.shutdown(wait=wait)

    def _cleanup_loop(self):
        """定期清理超时和僵尸任务"""
        while self._running:
            time.sleep(self._cleanup_interval)
            self._check_timeouts()
            self._cleanup_zombies()

    def _check_timeouts(self):
        """检查并标记超时任务"""
        now = time.time()
        with self._lock:
            for task_id, task in list(self.tasks.items()):
                if task.status == "running":
                    elapsed = now - task.start_time
                    if elapsed > task.timeout:
                        # 尝试取消 Future
                        cancelled = task.future.cancel() if task.future is not None else False
                        task.status = "timeout"
                        task.error = f"任务超时 ({elapsed:.0f}s > {task.timeout}s)"
                        logger.warning(
                            f"[Background] Task {task_id} timeout after {elapsed:.0f}s "
                            f"(cancelled={cancelled})"
                        )

    def _cleanup_zombies(self):
        """清理已完成/失败超过 5 分钟的僵尸任务记录"""
        now = time.time()
        with self._lock:
            stale = [
                tid for tid, t in self.tasks.items()
                if t.status in ("completed", "failed", "timeout", "cancelled")
                and now - t.start_time > 300  # 5 分钟
            ]
            for tid in stale:
                del self.tasks[tid]
                logger.debug(f"[Background] 清理僵尸任务: {tid}")

    def get_active_count(self) -> int:
        """获取活跃任务数"""
        with self._lock:
            return sum(1 for t in self.tasks.values() if t.status == "running")
