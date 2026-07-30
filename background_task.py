"""第三关：Background Tasks 异步非阻塞管理
使用 ThreadPoolExecutor 实现后台任务执行，支持轮询状态检查。
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
    future: Future
    status: str = "running"  # running/completed/failed
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    result: Any = None
    error: Optional[str] = None


class BackgroundTaskManager:
    """后台任务管理器——支持异步执行和轮询"""

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.tasks: Dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._running = True

    def submit(self, fn: Callable, task_name: str = "", **kwargs) -> str:
        """提交后台任务，返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        future = self.executor.submit(self._wrapped_execute, task_id, fn)
        task = BackgroundTask(
            task_id=task_id,
            name=task_name or f"task_{task_id}",
            future=future
        )
        with self._lock:
            self.tasks[task_id] = task

        logger.info(f"[Background] Task {task_id} submitted: {task.name}")
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