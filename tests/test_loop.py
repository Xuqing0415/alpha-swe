"""AgentLoop 端到端测试：脚本化 LLM、解析重试、中断注入。"""
import asyncio
from pathlib import Path

import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    """固定返回单个任务，避免消耗脚本化 LLM 的响应。

    max_retries 默认 0：既有用例只验证单次执行语义；
    任务级重试用例显式传入 >0 的预算。
    """

    def __init__(self, max_retries: int = 0):
        self.max_retries = max_retries

    async def plan(self, prompt, context=""):
        # 单任务代表整个会话：默认 critical，失败即会话失败
        return [Task(id="t0", instruction=prompt,
                     max_retries=self.max_retries,
                     criticality="critical")]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class GatedLLM(MockLLM):
    """首次调用阻塞，直到测试放行（用于确定性中断测试）。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.gate = asyncio.Event()
        self.first_call = asyncio.Event()

    async def complete(self, messages):
        self.first_call.set()
        await self.gate.wait()
        return self._responses.pop(0)


def make_config(ws_tmp: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),  # 基础循环测试不连接 MCP 服务器
    )


@pytest.mark.asyncio
async def test_full_react_flow_writes_file(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"think": "开始写文件"}',
        '{"tool": "file_ops", "params": {"action": "write", '
        '"path": "hello.txt", "content": "hi"}}',
        '{"final_answer": "文件已写入"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 hello.txt")
    assert result.ok
    assert result.final_answer == "文件已写入"
    content = (ws_tmp / "ws" / "hello.txt").read_text(encoding="utf-8")
    assert content == "hi"


@pytest.mark.asyncio
async def test_parse_retry_feedback(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"hello": 1}',          # 无法识别 -> 重试反馈
        '{"final_answer": "ok"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("测试解析重试")
    assert result.ok
    assert result.final_answer == "ok"
    # 轨迹里应包含重试反馈
    assert any("无法解析" in h.get("content", "")
               for h in loop.scheduler.dag.get("t0").history)


@pytest.mark.asyncio
async def test_parse_retry_exhausted_fails(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("解析持续失败")
    assert result.ok is False
    assert "解析失败" in result.final_answer


@pytest.mark.asyncio
async def test_interrupt_injects_high_priority_task(ws_tmp):
    cfg = make_config(ws_tmp)
    # 消费顺序: t0 思考 -> 中断任务 final -> 中断任务摘要 ->
    #           t0 恢复 final -> t0 摘要
    llm = GatedLLM(
        '{"think": "被中断前的思考"}',
        '{"final_answer": "高优先级任务完成"}',
        '{"problem": "x", "solution": "y"}',
        '{"final_answer": "原任务恢复完成"}',
        '{"problem": "x", "solution": "y"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())

    run_task = asyncio.create_task(loop.run("原任务"))
    await llm.first_call.wait()          # 等第一个 LLM 调用挂起
    loop.interrupt("新指令：优先处理")      # 注入高优先级任务
    llm.gate.set()                        # 放行
    result = await run_task

    assert result.ok
    assert "高优先级任务完成" in result.final_answer
    assert "原任务恢复完成" in result.final_answer


@pytest.mark.asyncio
async def test_pause_resume_suspends_loop(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = GatedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    run_task = asyncio.create_task(loop.run("暂停测试"))
    await llm.first_call.wait()          # 等第一个 LLM 调用挂起
    assert not loop.paused
    loop.pause()
    assert loop.paused
    loop.resume()
    assert not loop.paused
    llm.gate.set()                        # 放行
    result = await run_task
    assert result.ok
    assert result.final_answer == "完成"


@pytest.mark.asyncio
async def test_memory_remembered_on_completion(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    await loop.run("记忆测试")
    hits = loop.memory.retrieve("记忆测试", top_k=3)
    assert any("任务: 记忆测试" in h["text"] for h in hits)

@pytest.mark.asyncio
async def test_degenerate_empty_tool_calls_abort(ws_tmp):
    """连续 3 次空参数工具调用应立即中止，而不是烧光所有轮次。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"tool": "file_ops"}',
        '{"tool": "file_ops"}',
        '{"tool": "file_ops"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("空参数测试")
    assert result.ok is False
    assert "参数持续为空" in result.final_answer


@pytest.mark.asyncio
async def test_think_with_tool_executes_and_emits_think(ws_tmp):
    """同一 JSON 中 think+tool：先展示思考再执行工具。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"think": "先写文件", "tool": "file_ops", '
        '"params": {"action": "write", "path": "a.txt", "content": "x"}}',
        '{"final_answer": "完成"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("组合调用测试")
    assert result.ok
    assert (ws_tmp / "ws" / "a.txt").read_text(encoding="utf-8") == "x"
    assert any(e["type"] == "think" for e in loop.events)


@pytest.mark.asyncio
async def test_exec_env_injected_on_windows_fallback(ws_tmp):
    """非 Docker 回退时应向提示词注入 Windows PowerShell 环境说明。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "完成"}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    await loop.run("环境说明测试")
    assert loop.prompt_builder.exec_env
    assert "PowerShell" in loop.prompt_builder.exec_env

