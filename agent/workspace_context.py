"""会话间工作流连续性（主线一 1.2）：跨会话记住"正在做什么"。

记录当前项目的活跃工作流：
- active_branch / current_task_id / task_phase；
- pending_actions / uncommitted_changes；
- next_session_hint（会话结束自动生成，供下次续接）。

新会话启动时检测到未完成上下文，TUI 显示"上次你在做 X，处于 Y 阶段，
建议继续 Z"；继续时上下文摘要注入初始 Prompt。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.workspace")

_CONTEXT_FILE = ".swe-agent/context.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _git(*args: str) -> Optional[List[str]]:
    """best-effort git 只读查询；不可用时静默降级。"""
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            return r.stdout.splitlines()
    except Exception:
        pass
    return None


class WorkspaceContext:
    """项目级工作流上下文，持久化到 .swe-agent/context.json。"""

    def __init__(self, workspace: str,
                 context_file: Optional[str] = None) -> None:
        self.workspace = os.path.abspath(workspace)
        self.context_file = Path(context_file) if context_file else (
            Path(self.workspace) / _CONTEXT_FILE)
        self.data: Dict[str, Any] = self._load()

    # ---- 加载 / 保存 ----
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.context_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self) -> None:
        try:
            self.context_file.parent.mkdir(parents=True, exist_ok=True)
            self.context_file.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            logger.warning("工作流上下文写入失败: %s", e)

    # ---- 状态查询 ----
    def is_active(self) -> bool:
        """是否存在未完成的工作流（有待办或未完成任务）。"""
        if not self.data:
            return False
        if self.data.get("status") == "completed":
            return False
        return bool(self.data.get("current_task_id")
                    or self.data.get("pending_actions")
                    or self.data.get("next_session_hint"))

    def summarize(self) -> str:
        """TUI 提示文本：上次你在做 X，处于 Y 阶段，建议继续 Z。"""
        if not self.is_active():
            return ""
        parts: List[str] = []
        prompt = str(self.data.get("prompt") or "").strip()
        phase = str(self.data.get("task_phase") or "unknown")
        hint = str(self.data.get("next_session_hint") or "").strip()
        if prompt:
            parts.append(f"上次你在做：{prompt[:120]}")
        parts.append(f"当前阶段：{phase}")
        if hint:
            parts.append(f"建议继续：{hint[:160]}")
        return "；".join(parts)

    def prompt_text(self) -> str:
        """注入初始 Prompt 的上下文摘要。"""
        if not self.is_active():
            return ""
        lines = ["## 上次会话的未完成任务"]
        prompt = str(self.data.get("prompt") or "").strip()
        if prompt:
            lines.append(f"- 任务: {prompt[:200]}")
        if self.data.get("task_phase"):
            lines.append(f"- 进度阶段: {self.data['task_phase']}")
        pending = self.data.get("pending_actions") or []
        if pending:
            lines.append("- 待办: " + "; ".join(str(p)[:100]
                                                for p in pending[:5]))
        hint = str(self.data.get("next_session_hint") or "").strip()
        if hint:
            lines.append(f"- 上次提示: {hint[:200]}")
        return "\n".join(lines)

    # ---- 会话生命周期 ----
    def begin(self, prompt: str) -> None:
        """新会话开始：继承未完成工作流的任务标识与待办。"""
        prev = self.data
        unfinished = prev.get("status") != "completed" and bool(
            prev.get("current_task_id") or prev.get("pending_actions")
            or prev.get("next_session_hint"))
        self.data = {
            "active_branch": self._detect_branch(),
            "current_task_id": prev.get("current_task_id", "") if unfinished else "",
            "task_phase": "planning",
            "prompt": prompt,
            "pending_actions": prev.get("pending_actions", []) if unfinished else [],
            "uncommitted_changes": self._uncommitted(),
            "next_session_hint": prev.get("next_session_hint", "") if unfinished else "",
            "status": "active",
            "updated_at": _now(),
        }
        self.save()

    def finalize(self, prompt: str, result: Any = None) -> None:
        """会话结束：根据结果生成 next_session_hint 并落盘。

        result=None 表示会话被中断/异常退出。
        """
        if result is None:
            phase = "interrupted"
        else:
            phase = "completed" if getattr(result, "ok", False) else "failed"
        hint = self._build_hint(prompt, result, phase)
        pending: List[str] = []
        tasks = getattr(result, "tasks", None) if result is not None else None
        if tasks:
            from agent.core.task import TaskStatus
            terminal = {TaskStatus.COMPLETED, TaskStatus.SKIPPED}
            pending = [str(t.instruction) for t in tasks
                       if getattr(t, "status", None) not in terminal]
        self.data.update({
            "active_branch": self._detect_branch(),
            "task_phase": phase,
            "prompt": prompt,
            "pending_actions": pending[:10],
            "uncommitted_changes": self._uncommitted(),
            "next_session_hint": hint,
            "status": "completed" if phase == "completed" else "active",
            "updated_at": _now(),
        })
        self.save()

    def _build_hint(self, prompt: str, result: Any, phase: str) -> str:
        if phase == "completed":
            final = str(getattr(result, "final_answer", "") or "").strip()
            return f"任务已完成：{final[:100]}" if final else "任务已完成"
        if phase == "failed":
            failed = []
            for t in getattr(result, "tasks", []) or []:
                status = getattr(t, "status", None)
                if status is not None and getattr(status, "value", "") == "failed":
                    failed.append(t)
            if failed:
                err = str(failed[0].error or failed[0].instruction)[:120]
                return f"上次任务失败（{err}），建议从失败步骤继续"
            return "上次任务失败，建议检查失败原因后重试"
        return "上次会话被中断，建议先检查当前进度再继续"

    def _detect_branch(self) -> str:
        out = _git("-C", self.workspace, "rev-parse", "--abbrev-ref", "HEAD")
        return out[0].strip() if out else "unknown"

    def _uncommitted(self) -> List[str]:
        out = _git("-C", self.workspace, "status", "--porcelain")
        if not out:
            return []
        return [ln.strip()[:120] for ln in out[:20]]