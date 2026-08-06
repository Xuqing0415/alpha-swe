"""经验摘要器测试：LLM 解析、垃圾响应回退、LLM 失败回退。"""
import pytest

from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM
from agent.memory.summarizer import ExperienceSummarizer


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)

    async def complete(self, messages):
        return self._responses.pop(0)


def make_task(instruction="写一个接口", status=TaskStatus.COMPLETED):
    task = Task(id="t0", instruction=instruction, status=status)
    task.history = [
        {"role": "assistant", "content": '{"think": "分析需求"}'},
        {"role": "observation", "content": "[file_ops] 读取 app.py 成功"},
        {"role": "assistant", "content": '{"final_answer": "接口已实现"}'},
    ]
    task.result = "接口已实现"
    return task


@pytest.mark.asyncio
async def test_llm_summary_parsed():
    llm = ScriptedLLM(
        '```json\n{"problem": "缺少 REST 接口", "steps": ["生成路由", "加测试"], '
        '"solution": "新增 /api/items 路由", "outcome": "success", '
        '"key_files": ["app.py"]}\n```'
    )
    summarizer = ExperienceSummarizer(llm=llm)
    summary = await summarizer.summarize_task(make_task())
    assert summary["problem"] == "缺少 REST 接口"
    assert summary["solution"] == "新增 /api/items 路由"
    assert summary["key_files"] == ["app.py"]


@pytest.mark.asyncio
async def test_fallback_when_llm_garbage():
    llm = ScriptedLLM("这不是 JSON")
    summarizer = ExperienceSummarizer(llm=llm)
    summary = await summarizer.summarize_task(make_task())
    assert summary["problem"] == "写一个接口"
    assert summary["outcome"] == "success"


@pytest.mark.asyncio
async def test_fallback_when_llm_fails():
    class BrokenLLM(MockLLM):
        async def complete(self, messages):
            raise RuntimeError("api down")

    summarizer = ExperienceSummarizer(llm=BrokenLLM())
    summary = await summarizer.summarize_task(make_task(status=TaskStatus.FAILED))
    assert summary["outcome"] == "failed"
    assert summary["problem"] == "写一个接口"