"""Alpha-SWE 七层集成测试（pytest 版）

运行方式:
    python -X utf8 -m pytest test_all.py -v
（无需 --basetemp；测试数据写入 gitignored 的 test_workspace/）
"""
import json
import logging
import os
import time

import pytest

from memory_bank import MemoryBank
from background_task import BackgroundTaskManager
from plugin_loader import PluginLoader
from compressor import ContextCompressor
from sandbox import Sandbox
from executor import Executor
from mcp_config import MCPConfigLoader
from loop import Loop
from event_bus import event_bus, publish_event
from recovery import ErrorRecovery, RetryConfig
from critic_agent import CriticAgent
from structured_log import new_trace, setup_structured_logging
from tools.base import ToolResult


@pytest.fixture(scope="session", autouse=True)
def ensure_test_workspace():
    """确保测试工作区存在（含 src/*.ts 夹具文件）"""
    import main
    main.create_test_workspace("./test_workspace")
    yield


def test_memory_bank_dedupe_and_context():
    """第一关：MemoryBank 去重、中文检索与压缩快照"""
    db = os.path.join("test_workspace", ".mem_test.db")
    mem = MemoryBank(db_path=db)
    try:
        mem.add_entity("file", "src/app.ts", {"step": "搜索文件"})
        mem.add_entity("file", "src/app.ts", {"step": "搜索文件"})  # 重复添加应被去重
        mem.add_entity("file", "src/utils.ts", {"step": "搜索文件"})
        mem.add_entity("class", "Header", {"step": "解析组件"})
        mem.persist()

        assert mem.get_stats()["total_entities"] == 3
        assert "src/app.ts" in mem.get_context("搜索 app")
        compact = mem.compact()
        assert "长期记忆压缩" in compact
    finally:
        mem.close()
        if os.path.exists(db):
            os.remove(db)


def test_background_task_status_and_result():
    """第三关：后台任务状态/结果（含瞬时任务的竞态回归）"""
    bg = BackgroundTaskManager(max_workers=2)
    try:
        tid = bg.submit(lambda: 42, task_name="quick")
        assert bg.wait(tid, poll_interval=0.1, timeout=5) == 42
        assert bg.get_status(tid) == "completed"

        # 瞬时完成的任务也不能丢失状态更新
        tid2 = bg.submit(lambda: "done", task_name="instant")
        time.sleep(0.2)
        assert bg.get_status(tid2) == "completed"
    finally:
        bg.shutdown(wait=False)


def test_plugin_loader():
    """第四关：技能热加载与触发匹配"""
    loader = PluginLoader(skills_dir="./skills")
    assert "react" in loader.list_skills()
    ctx = loader.load_for_context("React 项目")
    assert "React" in ctx


def test_compressor():
    """第五关：上下文压缩"""
    compressor = ContextCompressor(max_token_limit=1000, threshold=0.5)
    history = [{"step": f"step_{i}", "action": "test", "result": "x" * 100} for i in range(20)]
    estimated = len(json.dumps(history))
    assert compressor.should_compress(estimated)
    compressed = compressor.compress(history)
    assert "COMPRESSED_SUMMARY" in compressed
    assert compressor.compression_count == 1


def test_sandbox_blocking():
    """第六关：沙箱拦截与安全写入"""
    sandbox = Sandbox(workspace="./test_workspace")
    executor = Executor(sandbox=sandbox)

    r = executor.execute("file_ops", {"action": "write", "path": "/etc/passwd", "content": "hack"})
    assert not r.success

    r = executor.execute("terminal_execute", {"command": "sudo rm -rf /"})
    assert not r.success

    r = executor.execute("file_ops", {"action": "write", "path": "safe.txt", "content": "safe"})
    assert r.success
    assert sandbox.violation_count == 2
    # 相对路径应落在沙箱工作区内
    assert os.path.exists(os.path.join("test_workspace", "safe.txt"))

    os.remove(os.path.join("test_workspace", "safe.txt"))


def test_mcp_config_isolated():
    """第七关：MCP 配置加载（load() 返回副本，不污染内部配置）"""
    mcp = MCPConfigLoader("config.yaml")
    config = mcp.load()
    assert mcp.is_tool_enabled("terminal_execute") is True
    assert mcp.is_tool_enabled("git") is False
    assert config["agent"]["max_rounds"] == 30

    config["agent"]["max_rounds"] = 1
    assert mcp.load()["agent"]["max_rounds"] == 30


def test_event_bus_publish_consume():
    """事件总线发布与消费"""
    event_bus.clear()
    publish_event("test_event", msg="hello", value=42)
    publish_event("step_start", step_id="1", description="测试步骤")
    publish_event("tool_call", tool="file_ops", params={"path": "test.txt"})

    events = event_bus.consume_all()
    assert len(events) == 3
    assert events[0].event_type == "test_event"
    assert events[0].data["value"] == 42


def test_error_recovery_retry():
    """错误恢复：失败重试直到成功"""
    recovery = ErrorRecovery(config=RetryConfig(max_retries=2, delay=0.1))
    calls = [0]

    def flaky():
        calls[0] += 1
        if calls[0] < 3:
            return ToolResult(success=False, output="", error="临时失败")
        return ToolResult(success=True, output="第三次成功")

    result = recovery.execute_with_retry(flaky)
    assert result.success
    assert calls[0] == 3
    assert recovery.retry_count == 2


def test_error_recovery_fallback():
    """错误恢复：fallback 策略"""
    recovery = ErrorRecovery()
    fallback = recovery.apply_fallback("terminal_execute", {"command": "grep -r test ."}, "grep not found")
    assert fallback is not None and fallback["action"] == "terminal_execute"

    fallback2 = recovery.apply_fallback("file_ops", {"path": "/tmp/nonexistent.txt"}, "No such file or directory")
    assert fallback2 is not None and fallback2["action"] == "terminal_execute"


def test_critic_agent_verdicts():
    """Critic 评审：pass/retry/revert"""
    critic = CriticAgent()
    v1 = critic.review({"step_id": "1", "description": "读取文件"},
                       {"success": True, "output": "hello", "error": ""})
    assert v1.verdict == "pass"

    v2 = critic.review({"step_id": "2", "description": "写系统文件"},
                       {"success": False, "output": "", "error": "permission denied: /etc/passwd"})
    assert v2.verdict == "revert"

    v3 = critic.review({"step_id": "3", "description": "读取配置"},
                       {"success": False, "output": "", "error": "No such file: config.yaml"})
    assert v3.verdict == "retry"

    v4 = critic.review({"step_id": "4", "description": "执行命令"},
                       {"success": True, "output": "", "error": ""})
    assert v4.verdict == "retry"  # 空输出应触发重试

    assert critic.get_stats()["total_reviews"] == 4


def test_structured_logging():
    """结构化日志：JSON Lines 写入与幂等初始化"""
    log_file = setup_structured_logging(log_dir="./logs", console=False)
    assert new_trace()
    logging.getLogger("alpha-swe.test").info("结构化日志测试消息")
    assert "结构化日志测试消息" in open(log_file, encoding="utf-8").read()


def test_full_loop_demo():
    """全流程集成：搜索 console.log -> 生成报告 -> 读取报告（Windows 可跑通）"""
    loop = Loop(config_path="config.yaml", skills_dir="./skills")
    try:
        result = loop.run(
            "请帮我读取 src/ 下所有 .ts 文件，找出所有的 console.log，"
            "并生成一个 report.txt，但注意不要读取 node_modules"
        )
        step_lines = [l for l in result.splitlines() if l.startswith("- ")]
        assert len(step_lines) == 3
        assert all("✓" in l for l in step_lines), f"存在失败步骤:\n{result}"

        report = os.path.join("test_workspace", "report.txt")
        assert os.path.exists(report)
        assert "console.log" in open(report, encoding="utf-8").read()
    finally:
        loop.memory.close()
        os.remove(os.path.join("test_workspace", "report.txt"))


def test_multi_agent_mode():
    """Multi-Agent 模式（Planner + Executor + Critic + Recovery）"""
    loop = Loop(config_path="config.yaml", skills_dir="./skills")
    try:
        result = loop.run_with_multi_agent("请列出当前目录的所有文件")
        assert "success" in result
    finally:
        loop.memory.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))