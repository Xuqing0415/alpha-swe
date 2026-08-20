"""排查方案 4.3：多模块同时降级的整链路集成测试。

在真实 AgentLoop 中同时启用 memory / sandbox / observability / tools，
注入可观测性故障（档案/决策/追踪目录不可写、span/事件含不可序列化内容），
断言任务仍正常完成并返回结果，而不是整体崩溃或吞掉 final_answer。

覆盖的模块错误边界（对应方案 2.x / 4.3）：
- observability.archive：OSError 与 TypeError（不可序列化）都降级为警告；
- observability.trace：导出/落盘失败降级；
- core.decision_logger：写入失败降级；
- core.loop.run()：任何可观测性异常都不允许吞掉任务结果。
"""
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, **agent_kw) -> AppConfig:
    agent_kw.setdefault("max_rounds", 10)
    agent_kw.setdefault("max_retries", 0)
    agent_kw.setdefault("max_concurrency", 1)
    agent_kw.setdefault("snapshot_enabled", False)
    # 关闭易受干扰的进阶流程，聚焦可观测性降级
    agent_kw.setdefault("auto_testgen", False)
    agent_kw.setdefault("regression_check_enabled", False)
    agent_kw.setdefault("mutation_check_enabled", False)
    agent_kw.setdefault("counterfactual_enabled", False)
    agent_kw.setdefault("state_tracker_enabled", False)
    agent_kw.setdefault("capability_enabled", False)
    agent_kw.setdefault("benchmark_extraction_enabled", False)
    agent_kw.setdefault("self_improve_enabled", False)
    return AppConfig(
        agent=AgentConfig(**agent_kw),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="none", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


def _blocker(ws_tmp: Path) -> Path:
    """在目标路径位置上放一个文件，让 mkdir 触发 NotADirectoryError。"""
    blocker = ws_tmp / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return blocker


def _make_loop(cfg: AppConfig, llm: MockLLM) -> AgentLoop:
    return AgentLoop(config=cfg, llm=llm, planner=StubPlanner())


@pytest.mark.asyncio
async def test_loop_completes_with_unwritable_archive_and_decision(ws_tmp):
    """档案目录/决策日志路径不可写（OSError）时，任务照常完成。"""
    blocker = _blocker(ws_tmp)
    cfg = make_config(
        ws_tmp,
        archive_enabled=True,
        trace_enabled=True,
        session_archive_dir=str(blocker / "sessions"),
        trace_dir=str(ws_tmp / "traces"),
    )
    cfg.decision_log_path = str(blocker / "dec.jsonl")
    loop = _make_loop(cfg, ScriptedLLM(
        '{"think": "先分析"}',
        '{"final_answer": "降级完成"}',
    ))
    try:
        result = await loop.run("可观测性 OSError 降级测试")
        assert result.phase.value == "completed"
        assert result.final_answer == "降级完成"
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_loop_completes_with_non_serializable_event(ws_tmp):
    """事件含不可序列化内容（TypeError）时，档案写入降级而任务不崩。"""
    cfg = make_config(
        ws_tmp,
        archive_enabled=True,
        trace_enabled=False,
        session_archive_dir=str(ws_tmp / "sessions"),
    )
    loop = _make_loop(cfg, ScriptedLLM(
        '{"think": "先分析"}',
        '{"final_answer": "事件脏数据降级完成"}',
    ))
    # 注入一个携带 object 的事件（json.dump 会抛 TypeError）
    loop.events.append({"type": "weird", "data": {"obj": object()}, "ts": 0})
    try:
        result = await loop.run("不可序列化事件降级测试")
        assert result.phase.value == "completed"
        assert result.final_answer == "事件脏数据降级完成"
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_loop_completes_with_non_serializable_span(ws_tmp):
    """span 属性含不可序列化内容（TypeError）时，trace 导出降级而任务不崩。"""
    cfg = make_config(
        ws_tmp,
        archive_enabled=True,
        trace_enabled=True,
        trace_dir=str(ws_tmp / "traces"),
        session_archive_dir=str(ws_tmp / "sessions"),
    )
    loop = _make_loop(cfg, ScriptedLLM(
        '{"think": "先分析"}',
        '{"final_answer": "span 脏数据降级完成"}',
    ))
    # 注入一个带 object 属性的 span（export 时 json.dumps 抛 TypeError）
    span = loop.tracer.start_span("weird", "tool", task_id="t0",
                                  obj=object())
    loop.tracer.end_span(span, status="ok")
    try:
        result = await loop.run("不可序列化 span 降级测试")
        assert result.phase.value == "completed"
        assert result.final_answer == "span 脏数据降级完成"
    finally:
        await loop.close()


@pytest.mark.asyncio
async def test_loop_completes_with_everything_broken(ws_tmp):
    """档案+决策+追踪目录全部不可写 + 脏事件：任务仍然完成。"""
    blocker = _blocker(ws_tmp)
    cfg = make_config(
        ws_tmp,
        archive_enabled=True,
        trace_enabled=True,
        session_archive_dir=str(blocker / "sessions"),
        trace_dir=str(blocker / "traces"),
    )
    cfg.decision_log_path = str(blocker / "dec.jsonl")
    loop = _make_loop(cfg, ScriptedLLM(
        '{"think": "先分析"}',
        '{"final_answer": "全故障降级完成"}',
    ))
    loop.events.append({"type": "weird", "data": {"obj": object()}, "ts": 0})
    try:
        result = await loop.run("全故障降级测试")
        assert result.phase.value == "completed"
        assert result.final_answer == "全故障降级完成"
    finally:
        await loop.close()
