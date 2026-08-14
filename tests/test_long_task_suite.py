"""收敛期 P1 剩余项：长任务端到端验证（阶段二 2.3）。

目标：验证系统能稳定完成需要多轮工具调用的复杂任务（L3/L4），并在执行
期间提供可观测性——上下文压缩是否过早、任务进度是否及时更新、是否有不
必要的重试、会话档案是否完整可供事后复盘。

设计（与基准集同一思路，为接入真实 LLM 预留）：
- 每个长任务用 ScriptedLLM 驱动 AgentLoop 走完「思考 -> 读文件 -> 写文件
  -> 写测试/重构 -> 最终答复」的完整轨迹；
- 完成标准由 verify 谓词自动判定（跑真实代码断言），而非只看 final_answer；
- 断言四类观察点：真实完成 / 进度事件 / 无重试 / 会话档案复盘摘要。

运行：python -X utf8 -m pytest tests/test_long_task_suite.py -q
"""
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, ContextConfig, MCPOptions,
                          MemoryConfig, SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.observability.archive import (SessionArchive, SessionReplay,
                                         summarize_session)


# ---- 验证辅助（可自动判定的完成标准）----

def write_files(root: Path, files: Dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def run_py(root: Path, code: str, timeout: int = 30) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def grep(root: Path, pattern: str, *rels: str) -> bool:
    rx = re.compile(pattern)
    for rel in rels or ("",):
        p = root / rel
        if p.is_dir():
            for f in p.rglob("*.py"):
                if rx.search(f.read_text(encoding="utf-8", errors="ignore")):
                    return True
        elif p.exists():
            if rx.search(p.read_text(encoding="utf-8", errors="ignore")):
                return True
    return False


def no_grep(root: Path, pattern: str, *rels: str) -> bool:
    return not grep(root, pattern, *rels)


# ---- 脚本化 LLM / Planner ----

class ScriptedLLM(MockLLM):
    """按脚本依次返回响应；调用次数超出即失败（暴露未脚本化的调用点）。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class StubPlanner:
    """固定返回单个 critical 任务，避免消耗脚本化 LLM 的响应。"""

    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


def _think(text: str) -> str:
    return json.dumps({"think": text}, ensure_ascii=False)


def _tool(**params) -> str:
    return json.dumps({"tool": "file_ops", "params": params},
                      ensure_ascii=False)


def _final(text: str) -> str:
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _summary(task: str, files: List[str]) -> str:
    return json.dumps({
        "problem": task, "solution": "按规范解法完成长任务",
        "steps": ["分析", "定位", "修改", "验证"],
        "key_files": files,
    }, ensure_ascii=False)


# ---- 长任务用例（L3/L4，多文件/多步骤）----

@dataclass
class LongTaskCase:
    name: str
    files: Dict[str, str]
    task: str
    difficulty: int
    tech: str
    responses: List[str]
    expected_files: List[str]
    verify: Callable[[Path], bool]


_TODO_APP = """class TodoStore:
    \"\"\"内存版 Todo 存储。\"\"\"

    def __init__(self):
        self._items = {}
        self._next = 1

    def list(self):
        return list(self._items.values())

    def get(self, tid):
        return self._items.get(tid)

    def create(self, title):
        tid = self._next
        self._next += 1
        item = {"id": tid, "title": title, "done": False}
        self._items[tid] = item
        return item

    def delete(self, tid):
        return self._items.pop(tid, None)
"""

_TODO_APP_GOLDEN = """class TodoStore:
    \"\"\"内存版 Todo 存储（含完整 CRUD）。\"\"\"

    def __init__(self):
        self._items = {}
        self._next = 1

    def list(self):
        return list(self._items.values())

    def get(self, tid):
        return self._items.get(tid)

    def create(self, title):
        tid = self._next
        self._next += 1
        item = {"id": tid, "title": title, "done": False}
        self._items[tid] = item
        return item

    def update(self, tid, title=None, done=None):
        item = self._items.get(tid)
        if item is None:
            return None
        if title is not None:
            item["title"] = title
        if done is not None:
            item["done"] = bool(done)
        return item

    def delete(self, tid):
        return self._items.pop(tid, None)
"""

_TODO_TEST_GOLDEN = """from app import TodoStore


def test_crud_roundtrip():
    store = TodoStore()
    item = store.create("买菜")
    assert store.get(item["id"])["title"] == "买菜"
    updated = store.update(item["id"], done=True)
    assert updated["done"] is True
    assert store.list()[0]["done"] is True
    assert store.delete(item["id"]) is not None
    assert store.get(item["id"]) is None


def test_update_missing_returns_none():
    store = TodoStore()
    assert store.update(999) is None
"""

_CASE_A_TASK = "为 TodoStore 补齐 update 方法（支持改标题/标记完成，不存在返回 None），并补集成测试 test_todo.py"

_APP_LOG = """import math


def area(radius):
    print(f"计算半径 {radius} 的面积")
    result = math.pi * radius ** 2
    print(f"面积结果: {result}")
    return result


def greet(name):
    print(f"你好，{name}")
    return f"hello {name}"
"""

_APP_LOG_GOLDEN = """import logging
import math

logger = logging.getLogger(__name__)


def area(radius):
    logger.info("计算半径 %s 的面积", radius)
    result = math.pi * radius ** 2
    logger.info("面积结果: %s", result)
    return result


def greet(name):
    logger.info("你好，%s", name)
    return f"hello {name}"
"""

_CASE_B_TASK = "把 app_log.py 里的 print 日志输出全部迁移到 logging 模块（logger.info），保持函数行为不变"

_UTILS = """def parse_amount(text):
    # TODO: 处理 None 输入，当前会崩溃
    return float(text)


def safe_div(a, b):
    # TODO: b 为 0 时应返回 None
    return a / b
"""

_UTILS_GOLDEN = """def parse_amount(text):
    if text is None or str(text).strip() == "":
        return 0.0
    return float(text)


def safe_div(a, b):
    if b == 0:
        return None
    return a / b
"""

_MAIN = """from utils import parse_amount, safe_div


def main():
    print(parse_amount("12.5"))
    print(safe_div(10, 0))
"""

_CASE_C_TASK = "修复 utils.py 中的所有 TODO 缺陷：parse_amount 对 None/空串返回 0.0，safe_div 除零返回 None"

_TRANSFORMS = """def double_evens(numbers):
    out = []
    for n in numbers:
        if n % 2 == 0:
            out.append(n * 2)
    return out


def square_odds(numbers):
    result = []
    for n in numbers:
        if n % 2 == 1:
            result.append(n * n)
    return result
"""

_TRANSFORMS_GOLDEN = """def double_evens(numbers):
    return [n * 2 for n in numbers if n % 2 == 0]


def square_odds(numbers):
    return [n * n for n in numbers if n % 2 == 1]
"""

_CASE_D_TASK = "把 transforms.py 中的显式循环重构为列表推导式，保持函数行为完全一致"


LONG_TASK_CASES: List[LongTaskCase] = [
    LongTaskCase(
        name="todo-crud",
        files={"app.py": _TODO_APP},
        task=_CASE_A_TASK,
        difficulty=4, tech="python",
        responses=[
            _think("分析需求：TodoStore 缺少 update，需要补 CRUD 集成测试"),
            _tool(action="read", path="app.py"),
            _tool(action="write", path="app.py", content=_TODO_APP_GOLDEN),
            _tool(action="write", path="test_todo.py",
                  content=_TODO_TEST_GOLDEN),
            _final("已完成 TodoStore CRUD 补全与集成测试"),
            _summary(_CASE_A_TASK, ["app.py", "test_todo.py"]),
        ],
        expected_files=["app.py", "test_todo.py"],
        verify=lambda ws: (
            run_py(ws,
                   "from app import TodoStore; "
                   "s = TodoStore(); i = s.create('x'); "
                   "assert s.get(i['id'])['title'] == 'x'; "
                   "u = s.update(i['id'], done=True); "
                   "assert u['done'] is True; "
                   "assert s.update(999) is None; "
                   "assert s.delete(i['id']) is not None; "
                   "assert s.get(i['id']) is None")
            and grep(ws, r"def test_", "test_todo.py")
            and grep(ws, r"def update", "app.py")),
    ),
    LongTaskCase(
        name="print-to-logging",
        files={"app_log.py": _APP_LOG},
        task=_CASE_B_TASK,
        difficulty=3, tech="python/logging",
        responses=[
            _think("把 print 迁移到 logging：保留函数行为，仅替换输出方式"),
            _tool(action="read", path="app_log.py"),
            _tool(action="write", path="app_log.py", content=_APP_LOG_GOLDEN),
            _final("已完成 print 到 logging 迁移"),
            _summary(_CASE_B_TASK, ["app_log.py"]),
        ],
        expected_files=["app_log.py"],
        verify=lambda ws: (
            run_py(ws,
                   "from app_log import area, greet; "
                   "assert area(2) > 0; assert greet('x') == 'hello x'")
            and no_grep(ws, r"\bprint\(", "app_log.py")
            and grep(ws, r"logging\.getLogger", "app_log.py")),
    ),
    LongTaskCase(
        name="fix-todos",
        files={"utils.py": _UTILS, "main.py": _MAIN},
        task=_CASE_C_TASK,
        difficulty=3, tech="python",
        responses=[
            _think("先搜索全部 TODO，再逐个修复 utils.py 中的缺陷"),
            _tool(action="search", path=".", pattern="TODO"),
            _tool(action="read", path="utils.py"),
            _tool(action="write", path="utils.py", content=_UTILS_GOLDEN),
            _final("已修复 utils.py 中的全部 TODO 缺陷"),
            _summary(_CASE_C_TASK, ["utils.py"]),
        ],
        expected_files=["utils.py"],
        verify=lambda ws: (
            run_py(ws,
                   "from utils import parse_amount, safe_div; "
                   "assert parse_amount(None) == 0.0; "
                   "assert parse_amount('') == 0.0; "
                   "assert parse_amount('3.5') == 3.5; "
                   "assert safe_div(10, 0) is None; "
                   "assert safe_div(6, 2) == 3.0")
            and no_grep(ws, "TODO", "utils.py", "main.py")),
    ),
    LongTaskCase(
        name="loop-to-listcomp",
        files={"transforms.py": _TRANSFORMS},
        task=_CASE_D_TASK,
        difficulty=3, tech="python",
        responses=[
            _think("把两个显式循环分别改为带条件的列表推导式"),
            _tool(action="read", path="transforms.py"),
            _tool(action="write", path="transforms.py",
                  content=_TRANSFORMS_GOLDEN),
            _final("重构完成：循环已改为列表推导式"),
            _summary(_CASE_D_TASK, ["transforms.py"]),
        ],
        expected_files=["transforms.py"],
        verify=lambda ws: (
            run_py(ws,
                   "from transforms import double_evens, square_odds; "
                   "assert double_evens([1, 2, 3, 4]) == [4, 8]; "
                   "assert square_odds([1, 2, 3, 4]) == [1, 9]")
            and grep(ws, r"for n in numbers if", "transforms.py")),
    ),
]


def make_config(ws: Path, max_tokens: int = 8000,
                compression_threshold: float = 0.8) -> AppConfig:
    """离线长任务配置：sqlite 记忆 + 关闭 docker/mcp + 会话档案落盘。"""
    return AppConfig(
        agent=AgentConfig(
            max_rounds=15, max_retries=2, max_concurrency=1,
            keep_recent_rounds=3,
            session_archive_dir=str(ws / "sessions"),
            archive_enabled=True,
            trace_dir=str(ws / "traces"), trace_enabled=True,
            metrics_enabled=True,
        ),
        sandbox=SandboxConfig(workspace=str(ws / "ws"), docker_enabled=False),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws / "mem.db")),
        mcp=MCPOptions(enabled=False),
        context=ContextConfig(
            max_tokens=max_tokens,
            compression_threshold=compression_threshold,
            archive_dir=str(ws / "archives"),
        ),
    )


async def _run_and_close(loop: AgentLoop, prompt: str):
    try:
        return await loop.run(prompt)
    finally:
        try:
            await loop.close()
        finally:
            closer = getattr(loop.memory, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass


def _load_latest_archive(ws: Path) -> dict:
    sessions = sorted((ws / "sessions").glob("session_*.json"))
    assert sessions, "长任务应生成会话档案"
    return SessionArchive.load(str(sessions[-1]))


# ---- 长任务端到端：真实完成 + 四类观察点 ----

@pytest.mark.parametrize("case", LONG_TASK_CASES, ids=lambda c: c.name)
def test_long_task_completes_with_observations(ws_tmp, case):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    write_files(ws, case.files)

    cfg = make_config(ws_tmp)
    llm = ScriptedLLM(*case.responses)
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, case.task))

    # 1) 真实完成：verify 谓词通过（而非只看 final_answer）
    assert result.ok, f"{case.name} 主循环应成功"
    assert case.verify(ws), f"{case.name} 完成标准应通过"

    # 2) 进度更新：每个成功工具调用都有 execution_completed 事件
    tool_ok = [e for e in loop.events
               if e.get("type") == "tool_call" and e["data"].get("success")]
    progress = [e for e in loop.events
                if e.get("type") == "execution_completed"
                and e["data"].get("success")]
    assert len(progress) == len(tool_ok) >= 2
    run_done = [e for e in loop.events if e.get("type") == "run_done"]
    assert run_done and run_done[0]["data"]["phase"] == "completed"

    # 3) 无不必要的重试/失败
    snap = loop.metrics.snapshot()
    assert snap["counters"].get("retries", 0) == 0
    assert snap["counters"].get("tool_failures", 0) == 0
    assert snap["gauges"]["tasks_completed"] == 1

    # 4) 记忆写入闭环：任务完成后生成了经验摘要
    assert any(d["name"] == "memory.write"
               for d in loop._decision.records()), "应记录 memory.write 决策"

    # 5) 会话档案：schema 完整 + 复盘摘要（默认阈值不应过早压缩）
    doc = _load_latest_archive(ws_tmp)
    assert doc["schema"] == "alpha-swe-session-v1"
    assert doc["result"]["ok"] is True
    assert doc["events"] and doc["decisions"] and doc["metrics"]
    summary = summarize_session(doc)
    assert summary["ok"] is True
    assert summary["tool_success_rate"] == 1.0
    assert summary["compressions"] == 0, "常规长任务不应过早触发压缩"
    assert summary["files_modified"] == case.expected_files
    assert SessionReplay(doc).timeline(), "档案应可回放"


# ---- 压缩时机：历史累积后才触发，不是开局即压 ----

def test_long_task_compression_mid_task(ws_tmp):
    case = next(c for c in LONG_TASK_CASES if c.name == "todo-crud")
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    write_files(ws, case.files)

    cfg = make_config(ws_tmp, max_tokens=400, compression_threshold=0.5)
    # 首轮思考内容超长，几轮后历史超过压缩阈值
    llm = ScriptedLLM(
        _think("分析需求并规划 CRUD 补全步骤。" * 60),
        *case.responses[1:],
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, case.task))
    assert result.ok

    # 压缩确实发生，且发生在若干决策点之后（非开局即压）
    snap = loop.metrics.snapshot()
    assert snap["counters"].get("compressions", 0) >= 1
    assert loop.context.compression_count >= 1
    assert any(d["name"] == "compression_level"
               for d in loop._decision.records())

    doc = _load_latest_archive(ws_tmp)
    summary = summarize_session(doc)
    assert summary["compressions"] >= 1
    assert summary["compression_first_after_events"] >= 3, (
        "首次压缩应发生在历史累积之后（过早压缩会丢关键信息）")


# ---- 失败模式：清晰失败原因而非卡死/崩溃 ----

def test_long_task_parse_failure_reports_clear_error(ws_tmp):
    ws = ws_tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg = make_config(ws_tmp)
    llm = ScriptedLLM('{"hello": 1}', '{"world": 2}')
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    result = asyncio.run(_run_and_close(loop, "触发解析失败的长任务"))

    assert result.ok is False
    assert "解析失败" in result.final_answer, "失败原因应清晰可读"

    doc = _load_latest_archive(ws_tmp)
    assert doc["result"]["ok"] is False
    summary = summarize_session(doc)
    assert summary["ok"] is False
    assert summary["errors"], "失败任务复盘应包含错误清单"
    assert summary["retries"] >= 1


# ---- 会话档案分析：summarize_session 与复盘脚本 ----

def test_summarize_session_fields():
    doc = {
        "schema": "alpha-swe-session-v1",
        "session_id": "abc123",
        "result": {
            "ok": True, "phase": "completed",
            "final_answer": "完成", "total_rounds": 4,
        },
        "events": [
            {"type": "task_start", "ts": 1.0,
             "data": {"task_id": "t0"}},
            {"type": "think", "ts": 1.1, "data": {"content": "思考"}},
            {"type": "tool_call", "ts": 1.2,
             "data": {"tool": "file_ops", "success": True,
                      "params": {"action": "write", "path": "a.py"}}},
            {"type": "execution_completed", "ts": 1.3,
             "data": {"tool": "file_ops", "success": True}},
            {"type": "tool_call", "ts": 1.4,
             "data": {"tool": "file_ops", "success": False,
                      "params": {"action": "write", "path": "b.py"}}},
            {"type": "run_done", "ts": 2.0,
             "data": {"phase": "completed"}},
        ],
        "decisions": [
            {"name": "trigger_compression", "timestamp": 3.0,
             "config_key": "context.compression_threshold",
             "config_value": "0.5", "decision": "触发压缩"},
            {"name": "compression_level", "timestamp": 3.0,
             "config_key": "context.compression_threshold",
             "config_value": "light", "decision": "压缩级别: light"},
        ],
        "metrics": {
            "counters": {"token_usage": 1000, "llm_calls": 4,
                         "tool_calls": 2, "tool_failures": 1,
                         "retries": 0, "compressions": 1},
            "gauges": {"rounds": 4, "tasks_total": 1,
                       "tasks_completed": 1, "tasks_failed": 0,
                       "tasks_skipped": 0},
        },
        "spans": [],
    }
    summary = summarize_session(doc)
    assert summary["ok"] is True
    assert summary["rounds"] == 4
    assert summary["tool_calls"] == 2
    assert summary["tool_success_rate"] == 0.5
    assert summary["compressions"] == 1
    # 压缩决策前有 task_start/think/tool_call×2 共 4 个决策点
    assert summary["compression_first_after_events"] == 4
    assert summary["files_modified"] == ["a.py"]
    assert summary["errors"] == []  # 失败工具调用无 output 字段，不产生占位错误

    # 失败任务：errors 回退到最终答复
    doc2 = dict(doc)
    doc2["result"] = {"ok": False, "phase": "failed",
                      "final_answer": "解析失败 2 次", "total_rounds": 1}
    assert summarize_session(doc2)["errors"] == ["解析失败 2 次"]


def test_analyze_session_script(ws_tmp, capsys, monkeypatch):
    arch = SessionArchive(str(ws_tmp / "sessions"), enabled=True)
    path = arch.write(
        "任务", [{"type": "run_done", "ts": 1.0,
                  "data": {"phase": "completed"}}],
        [], [{"name": "x", "timestamp": 1.0,
              "config_key": "k", "config_value": "v", "decision": "d"}],
        {"counters": {"token_usage": 50}, "gauges": {}},
        None)
    assert path is not None

    import runpy
    monkeypatch.setattr(sys, "argv",
                        ["analyze_session.py", str(path), "--json"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_path("scripts/analyze_session.py", run_name="__main__")
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["ok"] is False

    monkeypatch.setattr(sys, "argv",
                        ["analyze_session.py", str(path), "--text"])
    with pytest.raises(SystemExit) as ei2:
        runpy.run_path("scripts/analyze_session.py", run_name="__main__")
    assert ei2.value.code == 0
    text = capsys.readouterr().out
    assert "会话:" in text and "结果:" in text
