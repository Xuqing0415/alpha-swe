# -*- coding: utf-8 -*-
"""hypothesis 属性/模糊测试（边界打磨第 6 步）。

对关键解析入口喂任意输入，验证「绝不崩溃」与核心不变量，而非只测手写
样例：
- 输出解析器（loose/strict）：任意文本不抛异常，action_type 合法；
- 沙箱策略终端命令识别：任意命令串返回 (bool, str)，不抛异常；
- 沙箱策略文件路径校验：任意路径串不抛未预期异常；
- 工作区路径解析：只允许 PermissionError/ValueError/OSError，返回值必须
  锚定在工作区内；
- 配置加载：任意 YAML 内容 load_config 永不抛异常（三层降级契约）。

derandomize=True 固定种子保证 CI 与本地行为一致；max_examples 有界保证
混沌阶段耗时可控。运行于 chaos.yml（Ubuntu）。

运行：python -X utf8 -m pytest tests/test_property_fuzz.py -q
"""
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent.config import AppConfig, load_config
from agent.parser.parser import Parser, ParsedAction
from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext
from agent.tools.fileio import resolve_workspace_path

pytestmark = pytest.mark.chaos

_ACTION_TYPES = {"tool_call", "think", "final_answer", "error"}


@settings(max_examples=150, deadline=None, derandomize=True)
@given(raw=st.text(max_size=400))
def test_parser_loose_arbitrary_text_never_raises(raw):
    """宽松模式：任意文本都能得到一个合法 ParsedAction。"""
    act = Parser(mode="loose").parse(raw)
    assert isinstance(act, ParsedAction)
    assert act.action_type in _ACTION_TYPES
    if act.action_type == "error":
        assert act.error, "error 动作必须携带原因"


@settings(max_examples=120, deadline=None, derandomize=True)
@given(raw=st.text(max_size=400))
def test_parser_strict_arbitrary_text_never_raises(raw):
    """严格模式：无法识别时显式 error（可重试），而不是崩溃。"""
    act = Parser(mode="strict").parse(raw)
    assert isinstance(act, ParsedAction)
    assert act.action_type in _ACTION_TYPES
    if act.action_type == "error":
        assert act.error


@settings(max_examples=120, deadline=None, derandomize=True,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cmd=st.text(max_size=200))
def test_policy_terminal_command_never_raises(ws_tmp, cmd):
    """任意终端命令串：check 返回 (bool, reason)，不抛异常。"""
    policy = SandboxPolicy(workspace=str(ws_tmp))
    ctx = ExecutionContext(workspace=str(ws_tmp))
    ok, reason = policy.check(
        "terminal_execute", {"command": cmd}, ctx)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)


@settings(max_examples=120, deadline=None, derandomize=True,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(path=st.text(max_size=120))
def test_policy_file_path_never_raises_unexpected(ws_tmp, path):
    """任意文件路径串：file_ops check 不抛未预期异常（穿越被拒绝为 False）。"""
    policy = SandboxPolicy(workspace=str(ws_tmp))
    ctx = ExecutionContext(workspace=str(ws_tmp))
    ok, reason = policy.check(
        "file_ops", {"action": "read", "path": path}, ctx)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)


@settings(max_examples=150, deadline=None, derandomize=True,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(path=st.text(max_size=120))
def test_resolve_workspace_path_stays_in_root_or_documented_raise(ws_tmp, path):
    """路径解析：只允许文档化异常；成功结果必须锚定在工作区内。"""
    root = Path(ws_tmp).resolve()
    try:
        resolved = resolve_workspace_path(str(ws_tmp), path)
    except (ValueError, OSError):  # PermissionError 是 OSError 子类：越界即文档化拒绝
        return
    assert resolved == root or root in resolved.parents


@settings(max_examples=60, deadline=None, derandomize=True,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(content=st.text(max_size=400))
def test_load_config_never_raises_on_arbitrary_yaml(ws_tmp, content):
    """配置加载契约：任意 YAML 内容都不会让 load_config 抛异常。"""
    cfg_path = ws_tmp / "agent_fuzz.yaml"
    cfg_path.write_text(content, encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert isinstance(cfg, AppConfig)
