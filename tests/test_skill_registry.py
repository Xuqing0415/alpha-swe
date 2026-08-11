"""阶段二 2.1 技能注册表测试：元数据合并/校验/发现/使用记录/展开元数据，
以及阶段一 1.2 Planner 调用图注入拆分提示。"""
import json

import pytest

from agent.code.call_graph import build_call_graph
from agent.config import PlannerConfig
from agent.context.skill import SkillLibrary
from agent.llm import MockLLM
from agent.planner.planner import Planner


class _DecisionLogger:
    def __init__(self):
        self.rows = []

    def record(self, name, key, value, decision):
        self.rows.append(name)


def write_skill(d, name: str, body: str):
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


REST_SKILL = """name: add-rest-endpoint
version: "1.0.0"
description: 添加 REST 端点
priority: 8
requires: []
permissions: [file_read, file_write, terminal_execute]
params:
  method:
    type: string
    required: false
    default: GET
  path:
    type: string
    required: true
triggers:
  keywords: [rest, api, endpoint]
steps:
  - name: route
    instruction: 定义 REST 端点路由
    dependencies: []
  - name: validation
    instruction: 编写请求参数校验
    dependencies: [route]
  - name: controller
    instruction: 实现端点控制器逻辑
    dependencies: [validation]
"""


# ---- 注册表元数据加载与合并 ----
def test_registry_metadata_loaded_and_merged(ws_tmp):
    d = ws_tmp / "skills"
    # YAML 不声明 requires/permissions 时由注册表补缺省
    write_skill(d, "add-rest-endpoint",
                REST_SKILL.replace("requires: []\n", "")
                .replace("permissions: [file_read, file_write, terminal_execute]\n", ""))
    reg = ws_tmp / "registry.json"
    reg.write_text(json.dumps({
        "skills": {
            "add-rest-endpoint": {
                "requires": ["python"],
                "permissions": ["file_read"],
                "author": "team-a",
            }
        }
    }), encoding="utf-8")
    lib = SkillLibrary(skills_dir=str(d), registry_file=str(reg),
                       usage_log=str(ws_tmp / "usage.jsonl"))
    skill = lib.get("add-rest-endpoint")
    assert skill is not None
    # 注册表补缺省 requires/permissions/author
    assert skill.requires == ["python"]
    assert skill.permissions == ["file_read"]
    assert skill.author == "team-a"
    # params 从 YAML 加载
    assert [(p.name, p.required, p.default) for p in skill.params] == [
        ("method", False, "GET"), ("path", True, None),
    ]
    ctx = skill.to_context()
    assert "所需权限" in ctx and "依赖技能" in ctx and "参数" in ctx


def test_registry_only_fills_missing_fields(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "plain", """name: plain
triggers:
  keywords: [plain]
steps:
  - name: s1
    instruction: 一步
""")
    reg = ws_tmp / "registry.json"
    reg.write_text(json.dumps({
        "skills": {
            "plain": {"requires": ["base"], "permissions": ["file_read"]},
        }
    }), encoding="utf-8")
    lib = SkillLibrary(skills_dir=str(d), registry_file=str(reg))
    skill = lib.get("plain")
    assert skill.requires == ["base"]
    assert skill.permissions == ["file_read"]
    assert skill.version == "0.0.0"  # 未声明保持默认


# ---- 校验 ----
def test_validate_catches_issues(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "bad", """name: bad
triggers:
  keywords: [bad]
steps:
  - name: s1
    instruction: 第一步
    dependencies: [missing]
  - name: s1
    instruction: 重复步骤名
    on_failure: nope
""")
    lib = SkillLibrary(skills_dir=str(d),
                       registry_file=str(ws_tmp / "nope.json"))
    issues = lib.validate()
    assert "bad" in issues
    text = " ".join(issues["bad"])
    assert "重复" in text and "missing" in text and "nope" in text
    # 合法技能无问题
    write_skill(d, "good", """name: good
triggers:
  keywords: [good]
steps:
  - name: s1
    instruction: 一步
""")
    assert "good" not in lib.validate()


# ---- 发现（含 requires 闭包） ----
def test_discover_includes_requires_closure(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "base", """name: base
triggers:
  keywords: [base]
steps:
  - name: s1
    instruction: 基础步骤
""")
    write_skill(d, "top", """name: top
requires: [base]
triggers:
  keywords: [special]
steps:
  - name: s1
    instruction: 特殊步骤
""")
    lib = SkillLibrary(skills_dir=str(d))
    # match 只返回命中的技能
    assert [s.name for s in lib.match("special 任务")] == ["top"]
    # discover 返回命中 + requires 闭包
    names = [s.name for s in lib.discover("special 任务")]
    assert "top" in names and "base" in names


# ---- 使用记录 / 版本管理 ----
def test_usage_log_and_summary(ws_tmp):
    lib = SkillLibrary(skills_dir=str(ws_tmp / "skills"),
                       registry_file="",
                       usage_log=str(ws_tmp / "usage.jsonl"))
    lib.record_usage("add-rest-endpoint", "1.0.0", "completed")
    lib.record_usage("add-rest-endpoint", "1.0.0", "failed")
    lib.record_usage("python", "1.1.0", "completed")
    summary = lib.usage_summary()
    assert summary["add-rest-endpoint"] == {"completed": 1, "failed": 1}
    assert summary["python"] == {"completed": 1}


# ---- 展开元数据 ----
def test_expand_metadata_includes_registry_fields(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "add-rest-endpoint", REST_SKILL)
    lib = SkillLibrary(skills_dir=str(d),
                       registry_file=str(ws_tmp / "nope.json"))
    skill = lib.get("add-rest-endpoint")
    tasks = lib.expand(skill, "为订单模块添加一个 REST API 端点")
    t = tasks[0]
    assert t.metadata["skill"] == "add-rest-endpoint"
    assert t.metadata["skill_version"] == "1.0.0"
    assert t.metadata["permissions"] == ["file_read", "file_write", "terminal_execute"]
    assert t.metadata["requires"] == []


# ---- Planner 调用图注入拆分提示 ----
class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_planner_injects_call_graph_and_project_context(ws_tmp):
    (ws_tmp / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
    (ws_tmp / "b.py").write_text("def bar():\n    return foo()\n",
                                 encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    dl = _DecisionLogger()
    llm = ScriptedLLM(
        '[{"instruction": "修改 foo 并同步调用方", "dependencies": [], "priority": 0}]')
    planner = Planner(llm=llm, config=PlannerConfig(), decision_logger=dl)
    tasks = await planner.plan(
        "修改 foo 函数", call_graph=cg,
        project_context="## 项目约定\n- 技术栈: Python")
    assert tasks
    assert "planner.call_graph.injected" in dl.rows
    user_msg = llm.calls[0][-1]["content"]
    assert "影响范围提示" in user_msg
    assert "foo" in user_msg
    assert "项目约定" in user_msg


@pytest.mark.asyncio
async def test_planner_no_call_graph_skips_injection():
    dl = _DecisionLogger()
    llm = ScriptedLLM('[{"instruction": "任意", "dependencies": [], "priority": 0}]')
    planner = Planner(llm=llm, config=PlannerConfig(), decision_logger=dl)
    tasks = await planner.plan("任意任务")
    assert tasks
    assert "planner.call_graph.injected" not in dl.rows
    user_msg = llm.calls[0][-1]["content"]
    assert "影响范围提示" not in user_msg
