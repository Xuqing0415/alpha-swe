"""TUI 测试：事件格式化、事件订阅、终端实时输出桥接、Textual 无头运行。"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.tools.base import ExecutionContext
from agent.tools.terminal import TerminalTool
from tui.formatting import format_event
from tui.app import AlphaSWEApp


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, **kw):
    kw.setdefault("enabled", False)
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(**kw),
    )


# ---- 事件格式化（纯函数） ----
def test_format_event_think():
    text = format_event({"type": "think", "data": {"content": "分析中"}})
    assert "思考: 分析中" in text.plain


def test_format_event_tool_call():
    text = format_event({
        "type": "tool_call",
        "data": {"tool": "terminal_execute", "params": {"command": "ls"},
                 "success": True, "output": "a.txt"},
    })
    assert "terminal_execute" in text.plain
    assert "成功" in text.plain


def test_format_event_plan_created():
    text = format_event({"type": "plan_created",
                         "data": {"total": 2, "tasks": ["a", "b"]}})
    assert "规划 2 个子任务" in text.plain


def test_format_event_unknown_type():
    text = format_event({"type": "weird", "data": {"x": 1}})
    assert "weird" in text.plain


# ---- AgentLoop 事件订阅 ----
@pytest.mark.asyncio
async def test_loop_subscribe_receives_events(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    seen = []
    loop.subscribe(lambda record: seen.append(record["type"]))
    await loop.run("订阅事件")
    assert "run_start" in seen
    assert "task_start" in seen
    assert "run_done" in seen


# ---- 终端实时输出回调 ----
@pytest.mark.asyncio
async def test_terminal_tool_streams_output(ws_tmp):
    lines = []
    (ws_tmp / "ws").mkdir(parents=True, exist_ok=True)
    tool = TerminalTool(default_timeout=15)
    ctx = ExecutionContext(
        workspace=str(ws_tmp / "ws"),
        output_callback=lambda line: lines.append(line),
    )
    result = await tool.execute(
        {"command": 'python -c "print(\'hello tui\'); print(\'line2\')"'},
        ctx,
    )
    assert result.success
    assert any("hello tui" in line for line in lines)
    assert any("line2" in line for line in lines)


# ---- Textual 无头运行：事件流入左栏并正常结束 ----
@pytest.mark.asyncio
async def test_tui_streams_events_and_finishes(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"think": "先看看"}',
        '{"final_answer": "TUI 完成"}',
    )
    app = AlphaSWEApp(
        "TUI 测试",
        config=cfg,
        llm=llm,
        planner=StubPlanner(),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        # 轮询等待 Agent 完成（worker 跑在测试事件循环内）
        for _ in range(100):
            await pilot.pause(0.05)
            if app._finished is not None:
                break
        assert app._finished is not None, "Agent 未在预期时间内完成"
        assert app._finished.ok
        # 左栏应包含 think 与 run_done 事件渲染
        log = app.query_one("#thought-log")
        rendered = "".join(str(line) for line in log.lines)
        assert "TUI 完成" in rendered or "任务结束" in rendered
        # Ctrl+P 暂停不应抛错（loop 已结束，为空操作）
        await pilot.press("ctrl+p")
        await pilot.pause()
