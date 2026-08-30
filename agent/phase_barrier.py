"""phase-barrier 阶段门禁桥接（轻量 SDK 方式，编排器钩子）。

职责分离：alpha-swe 只做调用，校验逻辑全部在
phase-barrier 仓库维护（https://github.com/Xuqing0415/phase-barrier）。
约束：
- 未启用 / 依赖缺失 / 调用异常 / 超时 → 返回 skip=True 的放行结果，不影响既有行为；
- 所有方法返回稳定的 dict，编排器可直接透传 / 落日志 / 回传 Agent。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("alpha-swe.phase_barrier")


class PhaseBarrierBridge:
    """封装 anti_shortcut.PhaseBarrier 的轻量桥接。

    :param config: PhaseBarrierConfig 实例（必须已启用）
    :param workspace: 门禁工作区路径（证据文件 spec.md / test_*.py 所在目录）
    :param user_request: 阶段 0 证据：用户需求原文
    """

    def __init__(self, config, workspace: str, user_request: str = "") -> None:
        self.config = config
        self.workspace = str(workspace)
        self.user_request = user_request or ""
        self._barrier: Optional[Any] = None
        self._init_error = ""
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    @property
    def available(self) -> bool:
        self._ensure()
        return self._barrier is not None

    def set_user_request(self, text: str) -> None:
        """任务启动时设置阶段 0 证据（用户需求原文），
        在首次初始化前调用有效。"""
        if text:
            self.user_request = text

    def _ensure(self) -> None:
        if self._barrier is not None:
            return
        with self._lock:
            if self._barrier is not None:
                return
            try:
                from anti_shortcut import PhaseBarrier

                self._barrier = PhaseBarrier(
                    workspace=self.workspace,
                    user_request=self.user_request,
                    console_log=False,
                )
            except Exception as exc:  # noqa: BLE001
                self._init_error = str(exc)
                logger.warning(
                    "phase-barrier 初始化失败，门禁降级放行: %s",
                    exc,
                )

    def _call(self, fn: Callable[[], Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """带超时保护的同步调用；异常 / 超时返回 None。"""
        self._ensure()
        if self._barrier is None:
            return None
        timeout = float(getattr(self.config, "timeout", 10.0) or 10.0)
        result: Dict[str, Any] = {}
        error: list = []

        def _run() -> None:
            try:
                result.update(fn())
            except Exception as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            logger.warning(
                "phase-barrier 调用超时（>%ss），门禁降级放行",
                timeout,
            )
            return None
        if error:
            logger.warning(
                "phase-barrier 调用失败，门禁降级放行: %s",
                error[0],
            )
            return None
        return result

    def close(self) -> None:
        if self._barrier is not None:
            try:
                self._barrier.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 状态查询 ----------

    def inspect(self) -> Dict[str, Any]:
        res = self._call(lambda: self._barrier.inspect())
        if res is None:
            return {
                "skip": True,
                "workspace": self.workspace,
                "current_stage": None,
                "stage_name": "",
                "completed_stages": [],
                "complete": False,
            }
        return {**res, "skip": False}

    # ---------- 钩子校验 ----------

    def check_stage(self, stage: int) -> Dict[str, Any]:
        """编排器钩子：Agent 声称进入 / 处于 ``stage``，返回是否放行及约束提示。"""
        res = self._call(lambda: self._barrier.check(int(stage)))
        if res is None:
            return {
                "skip": True,
                "allowed": True,
                "message": "phase-barrier 不可用，门禁降级放行",
                "violations": [],
            }
        return {**res, "skip": False}

    def advance_stage(self, to_stage: int) -> Dict[str, Any]:
        """编排器钩子：校验当前阶段证据并推进到 ``to_stage``。"""
        res = self._call(lambda: self._barrier.advance(int(to_stage)))
        if res is None:
            return {
                "skip": True,
                "success": False,
                "stage": None,
                "message": "",
                "error": "phase-barrier 不可用，门禁降级放行",
            }
        return {**res, "skip": False}

    def record_test_run(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """编排器钩子：登记一次测试运行结果（``{exit_code, output}``）。"""
        res = self._call(lambda: self._barrier.record_test_run(result))
        if res is None:
            return {
                "skip": True,
                "exit_code": result.get("exit_code"),
                "passed": None,
                "summary": "",
            }
        return {**res, "skip": False}

    def verify_evidence(self) -> Dict[str, Any]:
        res = self._call(lambda: self._barrier.verify_evidence())
        if res is None:
            return {"skip": True, "ok": True, "violations": [], "signed": False}
        return {**res, "skip": False}

    # ---------- 工具级拦截（可选） ----------

    def _guard(self, fn: Callable[[], None], label: str) -> Dict[str, Any]:
        """拦截检查：无异常 = 放行，PermissionError = 拦截。"""
        self._ensure()
        if self._barrier is None:
            return {"skip": True, "allowed": True,
                    "message": "phase-barrier 不可用，放行"}
        timeout = float(getattr(self.config, "timeout", 10.0) or 10.0)
        error: list = []

        def _run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return {"skip": True, "allowed": True,
                    "message": "phase-barrier 调用超时，放行"}
        if error:
            return {"skip": False, "allowed": False, "message": str(error[0])}
        return {"skip": False, "allowed": True, "message": "放行"}

    def check_write(self, path: str) -> Dict[str, Any]:
        """写文件前校验：返回 ``{allowed, message}``，不允许时不写入。"""
        return self._guard(lambda: self._barrier.skill.check_write_permission(path),
                           "write")

    def check_exec(self, command: str) -> Dict[str, Any]:
        """执行命令前校验：返回 ``{allowed, message}``。"""
        return self._guard(lambda: self._barrier.skill.check_exec_permission(command),
                           "exec")
