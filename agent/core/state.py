"""任务级有限状态机 —— 对应设计第 2.1 节。

    IDLE -> PLANNING -> READY -> RUNNING -> WAITING -> COMPLETED / FAILED
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Set


class AgentPhase(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


# 合法转移表
TRANSITIONS: dict[AgentPhase, Set[AgentPhase]] = {
    AgentPhase.IDLE: {AgentPhase.PLANNING},
    AgentPhase.PLANNING: {AgentPhase.READY, AgentPhase.FAILED},
    AgentPhase.READY: {AgentPhase.RUNNING, AgentPhase.FAILED},
    AgentPhase.RUNNING: {AgentPhase.WAITING, AgentPhase.READY, AgentPhase.COMPLETED, AgentPhase.FAILED},
    AgentPhase.WAITING: {AgentPhase.READY, AgentPhase.FAILED},
    AgentPhase.COMPLETED: set(),
    AgentPhase.FAILED: set(),
}


@dataclass
class StateMachine:
    """Agent 会话级状态机。非法转移抛 ValueError。"""
    phase: AgentPhase = AgentPhase.IDLE
    interrupt_event: Optional[object] = None  # asyncio.Event，注入后由主循环消费
    interrupt_prompt: Optional[str] = None

    def can_transition(self, target: AgentPhase) -> bool:
        return target in TRANSITIONS[self.phase]

    def transition(self, target: AgentPhase) -> AgentPhase:
        if not self.can_transition(target):
            raise ValueError(f"非法状态转移: {self.phase.value} -> {target.value}")
        self.phase = target
        return self.phase

    def inject_interrupt(self, prompt: str, event) -> None:
        """注入高优先级指令：置 interrupt_prompt 并触发事件。"""
        self.interrupt_prompt = prompt
        if event is not None:
            event.set()

    def consume_interrupt(self) -> Optional[str]:
        prompt, self.interrupt_prompt = self.interrupt_prompt, None
        return prompt