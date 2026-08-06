"""状态机测试。"""
import pytest

from agent.core.state import AgentPhase, StateMachine


class FakeEvent:
    def __init__(self):
        self.set_count = 0

    def set(self):
        self.set_count += 1


def test_valid_flow():
    sm = StateMachine()
    sm.transition(AgentPhase.PLANNING)
    sm.transition(AgentPhase.READY)
    sm.transition(AgentPhase.RUNNING)
    sm.transition(AgentPhase.WAITING)
    sm.transition(AgentPhase.READY)
    sm.transition(AgentPhase.RUNNING)
    sm.transition(AgentPhase.COMPLETED)
    assert sm.phase == AgentPhase.COMPLETED


def test_fail_transition():
    sm = StateMachine()
    sm.transition(AgentPhase.PLANNING)
    sm.transition(AgentPhase.READY)
    sm.transition(AgentPhase.RUNNING)
    with pytest.raises(ValueError):
        sm.transition(AgentPhase.PLANNING)  # RUNNING -> PLANNING 非法


def test_interrupt_inject_and_consume():
    sm = StateMachine()
    event = FakeEvent()
    sm.inject_interrupt("用户新指令", event)
    assert event.set_count == 1
    assert sm.interrupt_prompt == "用户新指令"
    assert sm.consume_interrupt() == "用户新指令"
    assert sm.interrupt_prompt is None