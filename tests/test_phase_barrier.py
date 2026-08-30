# -*- coding: utf-8 -*-
"""phase-barrier 阶段门禁集成测试（alpha-swe#1 B 组任务）。

覆盖：
- PhaseBarrierBridge 桥接：依赖缺失降级放行、check/advance 全流程、写文件拦截；
- AgentLoop 端到端：Agent 尝试跳过 spec 直接写实现被拦截，随后按 SOP 推进到交付。
"""
import json
from typing import Any, Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                          MemoryConfig, PhaseBarrierConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.phase_barrier import PhaseBarrierBridge

SPEC_BODY = """## 需求分析
实现一个计算斐波那契数列的函数 fib(n)，返回第 n 个斐波那契数，n 从 0 开始计数。
## 设计方案
采用迭代算法避免递归指数复杂度，处理 n=0/1 边界与负数输入。
## 接口定义
def fib(n: int) -> int
"""

TEST_BODY = """from fib import fib

def test_fib_base():
    assert fib(0) == 0
    assert fib(1) == 1

def test_fib_sequence():
    assert fib(5) == 5
    assert fib(10) == 55
"""

IMPL_BODY = """def fib(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
"""


def _write(rel: str, body: str) -> str:
    return json.dumps({"tool": "file_ops", "params": {
        "action": "write", "path": rel, "content": body}},
        ensure_ascii=False)


def _gate(action: str, stage: int = 0) -> str:
    return json.dumps({"tool": "phase_barrier_gate", "params": {
        "action": action, "stage": stage}}, ensure_ascii=False)


def _run_tests() -> str:
    return json.dumps({"tool": "run_tests", "params": {"framework": "pytest"}},
                      ensure_ascii=False)


def _final(text: str) -> str:
    return json.dumps({"final_answer": text}, ensure_ascii=False)


class LenientScriptedLLM(MockLLM):
    """脚本结束后默认 final_answer，避免计数敏感。"""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)

    async def complete(self, messages: List[Dict[str, Any]]) -> str:
        if self._responses:
            return self._responses.pop(0)
        return _final("已完成（脚本耗尽）")


class StubPlanner:
    async def plan(self, prompt: str, context: str = ""):
        return [Task(id="t0", instruction=prompt, max_retries=1,
                     criticality="critical")]


def _config(ws, enabled: bool) -> AppConfig:
    return AppConfig(
        phase_barrier=PhaseBarrierConfig(
            enabled=enabled,
            workdir=str(ws),
            user_request="实现一个计算斐波那契数列的函数",
        ),
        agent=AgentConfig(
            max_rounds=20, max_retries=1, max_concurrency=1,
            trace_enabled=False, archive_enabled=False,
            metrics_enabled=True, snapshot_enabled=False,
            regression_check_enabled=False, auto_testgen=False,
            mutation_check_enabled=False, counterfactual_enabled=False,
        ),
        sandbox=SandboxConfig(workspace=str(ws),
                              audit_dir=str(ws / "logs" / "audit")),
        memory=MemoryConfig(db_path=str(ws / "mem.db"),
                            backend="none", auto_experience=False),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(archive_dir=str(ws / "logs" / "archives"),
                              max_tokens=400),
    )


# ---------- 桥接单元测试 ----------


def test_bridge_full_flow(ws_tmp):
    """完整阶段流：跳步写实现被拦截 -> spec/测试/实现 -> 交付。"""
    cfg = PhaseBarrierConfig(enabled=True, workdir=str(ws_tmp),
                             user_request="实现 fib")
    bridge = PhaseBarrierBridge(cfg, workspace=str(ws_tmp),
                                user_request="实现 fib")
    try:
        assert bridge.check_stage(1)["allowed"] is True
        assert bridge.check_stage(3)["allowed"] is False
        assert bridge.check_write("fib.py")["allowed"] is False
        assert bridge.check_write("spec.md")["allowed"] is True

        # 初始状态为阶段 1（需求已记录）；写 spec 后推进到阶段 2
        (ws_tmp / "spec.md").write_text(SPEC_BODY, encoding="utf-8")
        assert bridge.advance_stage(2)["success"] is True

        (ws_tmp / "test_fib.py").write_text(TEST_BODY, encoding="utf-8")
        assert bridge.advance_stage(3)["success"] is True

        (ws_tmp / "fib.py").write_text(IMPL_BODY, encoding="utf-8")
        assert bridge.check_write("fib.py")["allowed"] is True
        assert bridge.advance_stage(4)["success"] is True

        bridge.record_test_run({"exit_code": 0, "output": "2 passed"})
        r = bridge.advance_stage(5)
        assert r["success"] is True
        assert r["stage"] == 6
        assert bridge.inspect()["complete"] is True
    finally:
        bridge.close()


def test_bridge_skips_when_dependency_missing(monkeypatch, ws_tmp):
    """依赖缺失 / 初始化失败时门禁降级放行，不影响既有行为。"""
    import anti_shortcut

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("package unavailable")

    monkeypatch.setattr(anti_shortcut, "PhaseBarrier", boom)
    bridge = PhaseBarrierBridge(PhaseBarrierConfig(enabled=True), str(ws_tmp))
    try:
        gate = bridge.check_stage(1)
        assert gate["skip"] is True
        assert gate["allowed"] is True
        assert bridge.check_write("fib.py")["allowed"] is True
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_loop_disabled_has_no_bridge(ws_tmp):
    """默认关闭：不注册门禁工具、不构建桥接。"""
    loop = AgentLoop(config=_config(ws_tmp, enabled=False), llm=MockLLM())
    try:
        assert loop._barrier_bridge is None
        assert "phase_barrier_gate" not in loop.tools.names()
    finally:
        await loop.close()


# ---------- 端到端测试 ----------


@pytest.mark.asyncio
async def test_e2e_shortcut_blocked_then_sop(ws_tmp):
    """Agent 尝试跳过 spec 直接写实现，被 barrier 拦截；随后按规范流程推进到交付。"""
    script = [
        _write("fib.py", IMPL_BODY),          # ① 跳步写实现 -> 应被拦截；初始阶段 1（需求已记录）
        _write("spec.md", SPEC_BODY),
        _gate("advance", 2),                  # ③ spec 校验 -> 阶段 2
        _write("test_fib.py", TEST_BODY),
        _gate("advance", 3),                  # ④ 测试校验 -> 阶段 3
        _write("fib.py", IMPL_BODY),
        _gate("advance", 4),                  # ⑤ 实现校验 -> 阶段 4
        _run_tests(),                          # ⑥ 运行测试
        _gate("advance", 5),                  # ⑦ 测试全部通过 -> 跳过修复直达交付
        _final("已完成 fib 实现"),
    ]
    llm = LenientScriptedLLM(*script)
    loop = AgentLoop(config=_config(ws_tmp, enabled=True), llm=llm,
                     planner=StubPlanner())
    try:
        await loop.run("实现一个计算斐波那契数列的函数")
    finally:
        await loop.close()

    # 拦截发生：第一次写 fib.py 被门禁拦截
    blocked = [
        e for e in loop.events
        if e.get("type") == "tool_call"
        and e["data"].get("tool") == "file_ops"
        and (e["data"].get("params") or {}).get("action") == "write"
        and (e["data"].get("params") or {}).get("path") == "fib.py"
        and e["data"].get("success") is False
    ]
    assert blocked, "跳步写实现应被 phase-barrier 拦截"
    assert blocked[0]["data"].get("meta", {}).get("gate") == "write"

    # 任务启动钩子事件存在且放行阶段 1
    starts = [e for e in loop.events if e.get("type") == "phase_barrier_task_start"]
    assert starts and starts[0]["data"].get("allowed") is True

    # 最终状态：已交付（阶段 6）
    state_file = ws_tmp / ".agent_gate" / "state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state.get("current_stage") == 6
    assert 4 in state.get("completed_stages", [])

    # 证据文件都存在
    assert (ws_tmp / "spec.md").exists()
    assert (ws_tmp / "test_fib.py").exists()
    assert (ws_tmp / "fib.py").exists()
