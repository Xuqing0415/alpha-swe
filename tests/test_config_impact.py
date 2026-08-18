"""配置 -> 运行时行为数据流测试：决策日志 + 各决策点 + default/aggressive A/B 对比。"""
import json
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, LLMConfig,
                          LLMProvider, MCPOptions, MemoryConfig, PlannerConfig,
                          SandboxConfig, load_config)
from agent.context.manager import ContextManager
from agent.core.decision_logger import DecisionLogger
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.memory.factory import build_memory
from agent.memory.store import NoopMemoryStore
from agent.parser.parser import Parser
from agent.planner.planner import Planner
from agent.prompt.builder import PromptBuilder
from agent.sandbox.docker_sandbox import DockerSandbox
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext

ROOT = Path(__file__).resolve().parent.parent


def make_config(ws_tmp: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="hybrid", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt)]


class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        super().__init__()
        self._responses = list(responses)

    async def complete(self, messages):
        system = messages[0].get("content", "") if messages else ""
        if "经验总结器" in system:
            return "{}"
        assert self._responses, "LLM 脚本响应已耗尽"
        return self._responses.pop(0)


# ---- 决策日志 ----

def test_decision_logger_file_and_summary(tmp_path, monkeypatch):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("DECISION_LOG_PATH", str(log))
    logger = DecisionLogger()
    logger.record("a", "llm.temperature", 0.2, "解析器模式: strict")
    logger.record("b", "llm.temperature", 0.8, "解析器模式: loose")
    logger.record("c", "memory.backend", "none", "跳过记忆检索")
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["config_key"] == "llm.temperature"
    summary = logger.summary()
    assert summary["llm.temperature"] == ["解析器模式: strict", "解析器模式: loose"]
    assert logger.find(name="c")[0]["config_key"] == "memory.backend"
    logger.clear()
    assert logger.records() == []


# ---- LLM 配置决策点 ----

def test_prompt_style_by_provider():
    logger = DecisionLogger()
    builder = PromptBuilder(
        tool_schemas=[],
        llm_config=LLMConfig(provider=LLMProvider.ANTHROPIC, model="claude-3-haiku"),
        decision_logger=logger,
    )
    msgs = builder.build(Task(id="t1", instruction="x"))
    assert "<system-role>" in msgs[0]["content"]
    assert any(d["name"] == "system_prompt_style" and "Anthropic" in d["decision"]
               for d in logger.records())

    openai_logger = DecisionLogger()
    openai_builder = PromptBuilder(
        tool_schemas=[],
        llm_config=LLMConfig(provider=LLMProvider.OPENAI, model="gpt-4o"),
        decision_logger=openai_logger,
    )
    msgs2 = openai_builder.build(Task(id="t1", instruction="x"))
    assert "<system-role>" not in msgs2[0]["content"]
    assert any(d["name"] == "parser_strictness" and "strict" in d["decision"]
               for d in openai_logger.records())


def test_parser_strict_vs_loose():
    strict = Parser(mode="strict")
    assert strict.parse("纯文本回复").action_type == "error"
    loose = Parser(mode="loose")
    assert loose.parse("纯文本回复").action_type == "final_answer"


def test_parser_tool_calls_list():
    out = ('{"tool_calls": [{"tool": "file_ops", '
           '"params": {"action": "write", "path": "a.txt", "content": "A"}}, '
           '{"tool": "file_ops", '
           '"params": {"action": "write", "path": "b.txt", "content": "B"}}]}')
    act = Parser().parse(out)
    assert act.action_type == "tool_call"
    assert act.tool_name == "file_ops"
    assert len(act.extra_tool_calls) == 1
    assert act.extra_tool_calls[0]["params"]["path"] == "b.txt"


# ---- Agent 循环决策点 ----

@pytest.mark.asyncio
async def test_repeat_tool_call_guard(ws_tmp):
    """相同工具+参数连续重复调用达阈值时被拦截并注入纠偏，任务仍能继续完成。"""
    read = ('{"tool_calls": [{"tool": "file_ops", '
            '"params": {"action": "read", "path": "a.txt"}}]}')
    cfg = make_config(ws_tmp)
    cfg.agent.tool_repeat_limit = 2
    # 3 次相同 read（第 3 次被拦截），第 4 次 final_answer
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(read, read, read, '{"final_answer": "完成"}'),
                     planner=StubPlanner())
    result = await loop.run("读文件 a.txt 并报告")
    assert result.ok
    assert any(d["name"] == "tool.repeat_guard" for d in loop._decision.records())


@pytest.mark.asyncio
async def test_syntax_check_after_broken_write(ws_tmp):
    """write 出语法错误的 .py 时立即被语法校验拦截并反馈，修复后可继续完成。"""
    bad = ('{"tool_calls": [{"tool": "file_ops", '
           '"params": {"action": "write", "path": "broken.py", '
           '"content": "def f(:"}}]}')
    good = ('{"tool_calls": [{"tool": "file_ops", '
            '"params": {"action": "write", "path": "broken.py", '
            '"content": "def f():\\n    return 1"}}]}')
    cfg = make_config(ws_tmp)
    cfg.agent.syntax_check_enabled = True
    cfg.agent.regression_check_enabled = False
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(bad, good, '{"final_answer": "完成"}'),
                     planner=StubPlanner())
    result = await loop.run("写一个 python 文件并修复语法错误")
    assert result.ok
    calls = [e for e in loop.events if e.get("type") == "tool_call"]
    assert calls[0]["data"]["success"] is False
    assert "语法校验" in calls[0]["data"]["output"]
    assert calls[1]["data"]["success"] is True


@pytest.mark.asyncio
async def test_parallel_tool_calls_config(ws_tmp):
    tool_calls = ('{"tool_calls": [{"tool": "file_ops", '
                  '"params": {"action": "write", "path": "a.txt", "content": "A"}}, '
                  '{"tool": "file_ops", '
                  '"params": {"action": "write", "path": "b.txt", "content": "B"}}]}')
    # 并行（默认开启）
    cfg = make_config(ws_tmp)
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(tool_calls, '{"final_answer": "完成"}'),
                     planner=StubPlanner())
    result = await loop.run("并行写两个文件")
    assert result.ok
    assert (ws_tmp / "ws" / "a.txt").exists()
    assert (ws_tmp / "ws" / "b.txt").exists()
    assert any(d["name"] == "parallel_execution" for d in loop._decision.records())

    # 串行（禁用并行）
    cfg2 = make_config(ws_tmp)
    cfg2.agent.parallel_tool_calls = False
    loop2 = AgentLoop(config=cfg2, llm=ScriptedLLM(tool_calls, '{"final_answer": "完成"}'),
                      planner=StubPlanner())
    result2 = await loop2.run("串行写两个文件")
    assert result2.ok
    assert any(d["name"] == "sequential_execution" for d in loop2._decision.records())


@pytest.mark.asyncio
async def test_require_confirmation_rejects_write(ws_tmp):
    write = ('{"tool": "file_ops", "params": {"action": "write", '
             '"path": "secret.txt", "content": "x"}}')

    async def reject(name, params):
        return False

    cfg = make_config(ws_tmp)
    cfg.agent.auto_approve = []  # 不豁免
    loop = AgentLoop(config=cfg, llm=ScriptedLLM(write, '{"final_answer": "完成"}'),
                     planner=StubPlanner(), confirmation_callback=reject)
    result = await loop.run("写文件")
    assert result.ok
    assert not (ws_tmp / "ws" / "secret.txt").exists()
    names = [d["name"] for d in loop._decision.records()]
    assert "require_confirmation" in names and "user_rejected" in names

    # auto_approve 命中 -> 文件被写入
    cfg2 = make_config(ws_tmp)
    cfg2.agent.require_confirmation = []
    cfg2.agent.auto_approve = ["file_write"]
    loop2 = AgentLoop(config=cfg2, llm=ScriptedLLM(write, '{"final_answer": "完成"}'),
                      planner=StubPlanner())
    result2 = await loop2.run("写文件")
    assert result2.ok
    assert (ws_tmp / "ws" / "secret.txt").exists()
    assert any(d["name"] == "auto_approve" for d in loop2._decision.records())


# ---- 上下文压缩决策点 ----

def test_context_compression_config_driven():
    logger = DecisionLogger()
    history = [{"role": "assistant", "content": "B" * 1500} for _ in range(4)]

    agg = ContextManager(max_tokens=2000, compression_threshold=0.5,
                         compression_method="vector_retrieval",
                         decision_logger=logger)
    assert agg.should_compact(history)
    marker = agg.compact(history)
    assert "已归档(可向量检索)" in marker
    assert any(d["name"] == "trigger_compression" for d in logger.records())
    assert any(d["name"] == "compression_method" for d in logger.records())

    default_cm = ContextManager(max_tokens=8000, compression_threshold=0.8,
                                compression_method="summary",
                                decision_logger=DecisionLogger())
    assert not default_cm.should_compact(history)


# ---- Planner 决策点 ----

PLAN_4 = [
    {"instruction": "实现接口", "role": "coder", "dependencies": []},
    {"instruction": "补充单元测试", "dependencies": []},
    {"instruction": "更新文档", "dependencies": []},
    {"instruction": "运行测试", "dependencies": []},
]
SHORT_TASK = "实现 add 函数"
MEDIUM_TASK = "为 utils 模块补充单元测试，覆盖 add、subtract 两个函数，并同步更新 README 使用文档"


@pytest.mark.asyncio
async def test_planner_split_threshold():
    llm = MockLLM(responder=lambda msgs: json.dumps(PLAN_4, ensure_ascii=False))
    default_logger = DecisionLogger()
    default = Planner(llm=llm, config=PlannerConfig(
        split_threshold_complexity=0.4, max_subtasks=5, allow_parallel=True),
        decision_logger=default_logger)
    tasks = await default.plan(MEDIUM_TASK)
    assert len(tasks) == 4
    assert any(d["name"] == "execute_split" for d in default_logger.records())

    agg_logger = DecisionLogger()
    agg = Planner(llm=llm, config=PlannerConfig(
        split_threshold_complexity=0.9, max_subtasks=2, allow_parallel=False),
        decision_logger=agg_logger)
    tasks_skip = await agg.plan(SHORT_TASK)
    assert len(tasks_skip) == 1
    assert any(d["name"] == "skip_split" for d in agg_logger.records())


@pytest.mark.asyncio
async def test_planner_max_subtasks_and_sequential():
    llm = MockLLM(responder=lambda msgs: json.dumps(PLAN_4, ensure_ascii=False))
    logger = DecisionLogger()
    agg = Planner(llm=llm, config=PlannerConfig(
        split_threshold_complexity=0.0, max_subtasks=2, allow_parallel=False),
        decision_logger=logger)
    tasks = await agg.plan(MEDIUM_TASK)
    assert len(tasks) == 2
    assert any(d["name"] == "truncate_subtasks" for d in logger.records())
    assert any(d["name"] == "force_sequential" for d in logger.records())
    assert tasks[1].dependencies == [tasks[0].id]


# ---- 记忆决策点 ----

@pytest.mark.asyncio
async def test_memory_backend_none_skips_retrieval(ws_tmp):
    cfg = make_config(ws_tmp)
    cfg.memory.backend = "none"
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "完成"}'),
                     planner=StubPlanner())
    result = await loop.run("记忆禁用测试")
    assert result.ok
    assert isinstance(loop.memory, NoopMemoryStore)
    assert any(d["name"] == "retrieval_skip" for d in loop._decision.records())


@pytest.mark.asyncio
async def test_similarity_threshold_filters_memory(ws_tmp):
    store = build_memory(MemoryConfig(backend="hybrid", db_path=str(ws_tmp / "m.db")))
    store.remember("experience", "如何修复内存泄漏：检查循环引用并释放不再使用的连接", {"k": 1})

    cfg = make_config(ws_tmp)
    cfg.memory.similarity_threshold = 0.99
    loop = AgentLoop(config=cfg, llm=ScriptedLLM('{"final_answer": "完成"}'),
                     planner=StubPlanner(), memory=store)
    await loop.run("内存泄漏修复")
    hits = [d for d in loop._decision.records()
            if d["name"] == "retrieval_result"]
    assert hits and "保留 0 项" in hits[0]["decision"]


# ---- 沙箱决策点 ----

def test_sandbox_network_policy_config():
    logger = DecisionLogger()
    policy = SandboxPolicy(network_enabled=False, decision_logger=logger)
    ok, reason = policy.check(
        "terminal_execute", {"command": "curl https://example.com"},
        ExecutionContext(workspace="."),
    )
    assert ok is False and "网络已禁用" in reason
    assert any(d["name"] == "block_network_command" for d in logger.records())

    open_policy = SandboxPolicy(network_enabled=True)
    ok2, _ = open_policy.check(
        "terminal_execute", {"command": "curl https://example.com"},
        ExecutionContext(workspace="."),
    )
    assert ok2 is True


def test_docker_sandbox_spec_config_driven():
    logger = DecisionLogger()
    default = DockerSandbox(SandboxConfig(network_enabled=False, memory_limit="2g",
                                          cpu_limit=2.0), logger)
    spec = default.build_container_spec("./workspace")
    assert spec["network_mode"] == "none"
    assert spec["mem_limit"] == "2g"
    assert spec["nano_cpus"] == 2_000_000_000
    assert any(d["name"] == "network_mode" for d in logger.records())

    agg = DockerSandbox(SandboxConfig(network_enabled=True, memory_limit="512m",
                                      cpu_limit=0.5), DecisionLogger())
    spec2 = agg.build_container_spec("./workspace")
    assert spec2["network_mode"] == "bridge"
    assert spec2["mem_limit"] == "512m"
    assert spec2["nano_cpus"] == 500_000_000


# ---- 配置文件 A/B ----

def test_config_files_drive_different_values():
    default = load_config(str(ROOT / "config" / "default.yaml"))
    aggressive = load_config(str(ROOT / "config" / "aggressive.yaml"))
    assert default.llm.provider == LLMProvider.OPENAI
    assert aggressive.llm.provider == LLMProvider.ANTHROPIC
    assert aggressive.llm.temperature == 0.8
    assert default.context.compression_method == "summary"
    assert aggressive.context.compression_method == "vector_retrieval"
    assert aggressive.memory.backend == "none"
    assert aggressive.planner.allow_parallel is False
    assert aggressive.planner.max_subtasks == 2
    assert aggressive.agent.parallel_tool_calls is False
    assert aggressive.agent.max_loop_iterations == 5
    assert default.sandbox.is_network_enabled is False
    assert aggressive.sandbox.is_network_enabled is True
    assert default.sandbox.network_mode == "none"
    assert aggressive.sandbox.network_mode == "bridge"


@pytest.mark.asyncio
async def test_ab_compare_configs_change_decisions(ws_tmp, monkeypatch):
    base = load_config(str(ROOT / "config" / "default.yaml"))
    agg = load_config(str(ROOT / "config" / "aggressive.yaml"))
    base.sandbox.workspace = str(ws_tmp / "ws_base")
    agg.sandbox.workspace = str(ws_tmp / "ws_agg")
    base.memory.db_path = str(ws_tmp / "m_base.db")
    # 关闭 MCP，避免连接外部服务器拖慢 A/B 对比
    base.mcp.enabled = False
    agg.mcp.enabled = False
    # 决策日志写入独立文件
    monkeypatch.setenv("DECISION_LOG_PATH", str(ws_tmp / "base.jsonl"))
    loop_base = AgentLoop(config=base, llm=ScriptedLLM('{"final_answer": "完成"}'),
                          planner=StubPlanner())
    await loop_base.run("简单任务")
    assert (ws_tmp / "base.jsonl").exists()

    monkeypatch.setenv("DECISION_LOG_PATH", str(ws_tmp / "agg.jsonl"))
    loop_agg = AgentLoop(config=agg, llm=ScriptedLLM('{"final_answer": "完成"}'),
                         planner=StubPlanner())
    await loop_agg.run("简单任务")

    base_map = {d["name"]: d for d in loop_base._decision.records()}
    agg_map = {d["name"]: d for d in loop_agg._decision.records()}
    # 同配置键产生不同决策值
    assert base_map["memory_backend"]["decision"] != agg_map["memory_backend"]["decision"]
    assert base_map["system_prompt_style"]["decision"] != agg_map["system_prompt_style"]["decision"]
    assert "retrieval_skip" in agg_map
    assert "retrieval_skip" not in base_map
    assert base_map["sandbox_network"]["decision"] != agg_map["sandbox_network"]["decision"]