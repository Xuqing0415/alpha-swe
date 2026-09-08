# -*- coding: utf-8 -*-
"""Phase-Barrier 深化：模板预设加载 / 记忆联动 / 状态快照。

覆盖：
- config/agent.yaml 写 phase_barrier.template 时，config/phase_barrier/<name>.yaml
  覆盖段合并生效（走 load_config 真实路径）；
- 会话收尾把门禁结论写入长期记忆（kind=phase_barrier_outcome）；
- AgentLoop.pb_status() 提供门禁状态快照（供 CLI/TUI/Web 可视化）；
- 未启用门禁时 pb_status 返回 enabled=False，不触发任何 SDK 调用。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from agent.config import load_config
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


def _think(text):
    return json.dumps({"think": text}, ensure_ascii=False)


def _tool(**params):
    return json.dumps({"tool": "file_ops", "params": params},
                      ensure_ascii=False)


def _final(text):
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _write_cfg(root: Path, template: str) -> str:
    """写一份带 phase_barrier.template 的离线 YAML（memory=sqlite / llm=mock）。"""
    ws = root / "pb_ws"
    ws.mkdir(parents=True, exist_ok=True)
    parts = [
        "agent:\n",
        "  max_rounds: 10\n",
        "  max_retries: 0\n",
        "  auto_testgen: false\n",
        "  regression_check_enabled: false\n",
        "  mutation_check_enabled: false\n",
        "  counterfactual_enabled: false\n",
        "  self_improve_enabled: false\n",
        "  state_tracker_enabled: false\n",
        "  workspace_context_enabled: false\n",
        "sandbox:\n",
        '  workspace: "%s"\n' % ws.as_posix(),
        '  audit_dir: "%s/logs/audit"\n' % root.as_posix(),
        "  docker_enabled: false\n",
        "memory:\n",
        "  backend: sqlite\n",
        '  db_path: "%s/mem.db"\n' % root.as_posix(),
        "  auto_experience: false\n",
        "llm:\n",
        "  provider: mock\n",
        "mcp:\n",
        "  enabled: false\n",
        "skills:\n",
        "  enabled: false\n",
        "  workflow_enabled: false\n",
        "plugin:\n",
        "  enabled: false\n",
        "context:\n",
        '  archive_dir: "%s/logs/archives"\n' % root.as_posix(),
        "  max_tokens: 400\n",
    ]
    body = "".join(parts)
    if template:
        body += "phase_barrier:\n  template: %s\n" % template
    cfg_path = root / "agent_pb.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    return str(cfg_path)


def test_phase_barrier_template_merge_from_yaml(ws_tmp):
    """引用 template 时加载 config/phase_barrier/<name>.yaml 覆盖段。"""
    cfg = load_config(_write_cfg(ws_tmp, template="bug-fix"))
    assert cfg.phase_barrier.enabled is True, "bug-fix 模板应开启门禁"
    assert cfg.phase_barrier.template == "bug-fix"
    assert cfg.phase_barrier.timeout == 10
    for name in ("bug-fix", "feature-add", "refactor"):
        assert (Path("config") / "phase_barrier" / (name + ".yaml")).exists()


@pytest.mark.asyncio
async def test_pb_memory_linkage_and_pb_status(ws_tmp):
    """门禁结论在会话收尾写入长期记忆；pb_status 返回稳定快照。"""
    cfg = load_config(_write_cfg(ws_tmp, template="bug-fix"))
    # 只用 think + final，避免工具写入被真实阶段门禁拦截（CI 装有 anti_shortcut）
    llm = ScriptedLLM(
        _think("分析任务并收尾"),
        _final("门禁任务完成"),
    )
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())
    try:
        result = await loop.run("bug-fix 门禁任务")
    finally:
        await loop.close()
    assert result.ok

    status = loop.pb_status()
    assert status["enabled"] is True
    assert status["template"] == "bug-fix"

    conn = sqlite3.connect(ws_tmp / "mem.db")
    rows = conn.execute(
        "SELECT kind, text, metadata FROM memories "
        "WHERE kind='phase_barrier_outcome' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    conn.close()
    assert rows, "门禁结论应写入长期记忆"
    meta = json.loads(rows[0][2])
    assert meta.get("gate") == "phase_barrier"
    assert "门禁" in rows[0][1]


def test_pb_status_disabled_returns_empty(ws_tmp):
    cfg = load_config(_write_cfg(ws_tmp, template=""))
    loop = AgentLoop(config=cfg)
    try:
        assert loop.pb_status() == {"enabled": False, "available": False}
    finally:
        loop._barrier_bridge = None
