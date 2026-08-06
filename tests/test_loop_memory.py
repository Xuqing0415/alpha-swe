"""主循环与长期记忆集成测试：代码索引、经验摘要、错误记忆。"""
import pytest

from agent.config import (MCPOptions, AgentConfig, AppConfig,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp, **mem_kw):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"), **mem_kw),
        mcp=MCPOptions(enabled=False),  # 基础循环测试不连接 MCP 服务器
    )


@pytest.mark.asyncio
async def test_code_indexed_after_file_write(ws_tmp):
    cfg = make_config(ws_tmp)
    # 注意 JSON 中的 \n 需要写成字面转义（\\n），否则解析器会判为非法 JSON
    llm = ScriptedLLM(
        '{"tool": "file_ops", "params": {"action": "write", "path": "src/app.py", '
        '"content": "def helper():\\n    return 42"}}',
        '{"final_answer": "done"}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("写一个 app.py")
    assert result.ok
    hits = loop.memory.search("helper", kinds=["code"])
    assert hits and hits[0]["metadata"]["path"] == "src/app.py"
    assert "helper" in hits[0]["metadata"]["symbols"]


@pytest.mark.asyncio
async def test_experience_summary_written_on_completion(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(
        '{"final_answer": "完成"}',
        # 经验摘要响应（脚本化 LLM 按顺序消费）
        '{"problem": "写测试", "steps": ["跑 pytest"], "solution": "修正断言", '
        '"outcome": "success", "key_files": []}',
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    await loop.run("写测试")
    hits = loop.memory.search("写测试", kinds=["experience"])
    assert hits and "修正断言" in hits[0]["text"]


@pytest.mark.asyncio
async def test_error_memory_written_on_failure(ws_tmp):
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("会失败的任务")
    assert result.ok is False
    hits = loop.memory.search("输出解析失败", kinds=["error"])
    assert hits and hits[0]["kind"] == "error"


@pytest.mark.asyncio
async def test_experience_fallback_without_extra_llm_response(ws_tmp):
    """LLM 脚本耗尽时经验摘要回退规则提取，不中断主流程。"""
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"final_answer": "done"}')  # 无额外摘要响应
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = await loop.run("记忆回退测试")
    assert result.ok
    hits = loop.memory.search("记忆回退测试", kinds=["experience"])
    assert hits and "任务: 记忆回退测试" in hits[0]["text"]