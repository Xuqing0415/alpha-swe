# -*- coding: utf-8 -*-
"""跨进程项目锁 —— 多实例部署互斥（方向一·阶段 2）。

同一项目目录同一时刻只允许一个 Agent 实例操作：以原子 O_CREAT|O_EXCL
创建 `<project>/.swe-agent/project.lock`，锁内容含 pid/holder/时间戳；
冲突时读取锁文件，若持有者 PID 已不存在则视为残留锁自动回收。
- acquire(): 成功返回 True；超时或锁被存活进程持有时返回 False（不阻塞）。
- release(): 仅持有者释放（校验 pid），幂等。
- 跨平台：Windows（psutil.pid_exists）/ POSIX 通用，不依赖 fcntl。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("alpha-swe.project_lock")

try:
    import psutil
    _pid_exists = lambda pid: bool(psutil.pid_exists(int(pid)))
except Exception:  # pragma: no cover - psutil 缺失时退化为自身进程检查
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


class ProjectLockError(RuntimeError):
    """项目锁获取失败（另一实例正持有）。"""


class ProjectLock:
    """基于原子创建 + PID 存活探测的项目级互斥锁。"""

    LOCK_RELPATH = ".swe-agent/project.lock"

    def __init__(self, project_dir: str, holder: str = "",
                 stale_after_seconds: float = 300.0) -> None:
        self.project_dir = str(project_dir)
        self.holder = holder or f"pid-{os.getpid()}"
        self.stale_after_seconds = float(stale_after_seconds)
        self.lock_path = Path(self.project_dir) / self.LOCK_RELPATH
        self._fd: Optional[int] = None
        self._acquired = False

    # ---- 查询 ----
    @property
    def locked(self) -> bool:
        return self.lock_path.exists()

    def holder_info(self) -> Dict[str, object]:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data

    def is_held_by_alive_process(self) -> bool:
        info = self.holder_info()
        pid = info.get("pid")
        if not pid:
            return False
        return _pid_exists(int(pid))

    # ---- 获取 / 释放 ----
    def acquire(self, timeout: float = 0.0) -> bool:
        """获取项目锁。timeout>0 时轮询等待；超时或冲突返回 False。"""
        if self._acquired:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                payload = json.dumps({
                    "pid": os.getpid(),
                    "holder": self.holder,
                    "acquired_at": time.time(),
                }, ensure_ascii=False).encode("utf-8")
                os.write(fd, payload)
                os.close(fd)
                self._fd = -1  # 已关闭；标记持有
                self._acquired = True
                logger.info("项目锁已获取: %s (holder=%s)", self.lock_path,
                            self.holder)
                return True
            except FileExistsError:
                if self._reclaim_stale():
                    continue
                if time.time() >= deadline:
                    info = self.holder_info()
                    logger.warning(
                        "项目锁被其他实例持有: %s holder=%s pid=%s",
                        self.lock_path, info.get("holder"), info.get("pid"))
                    return False
                time.sleep(0.1)

    def _reclaim_stale(self) -> bool:
        """锁文件存在但持有者 PID 已不存在 -> 回收；返回是否可重试。"""
        info = self.holder_info()
        pid = info.get("pid")
        created = float(info.get("acquired_at") or 0)
        age = time.time() - created
        if not pid or not _pid_exists(int(pid)) and age > 5.0:
            # 防御：锁刚创建(<5s)且读不到 pid 时暂不回收，避免误删
            if pid or age > self.stale_after_seconds:
                try:
                    self.lock_path.unlink()
                    logger.warning("回收残留项目锁: %s (pid=%s age=%.1fs)",
                                   self.lock_path, pid, age)
                    return True
                except FileNotFoundError:
                    return True
                except OSError as e:
                    logger.warning("回收残留锁失败: %s", e)
        return False

    def release(self) -> None:
        """释放项目锁（校验是本进程持有的锁）。"""
        if not self._acquired:
            return
        self._acquired = False
        try:
            info = self.holder_info()
            if int(info.get("pid") or -1) == os.getpid():
                self.lock_path.unlink(missing_ok=True)
                logger.info("项目锁已释放: %s", self.lock_path)
            else:
                logger.warning("锁持有者不是本进程，跳过释放: %s", info)
        except Exception as e:
            logger.warning("释放项目锁异常: %s", e)

    def __enter__(self) -> "ProjectLock":
        if not self.acquire():
            info = self.holder_info()
            raise ProjectLockError(
                f"项目 {self.project_dir} 正被实例 {info.get('holder', '?')} "
                f"(pid={info.get('pid', '?')}) 锁定，请等待其退出或进入只读模式")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


__all__ = ["ProjectLock", "ProjectLockError"]
