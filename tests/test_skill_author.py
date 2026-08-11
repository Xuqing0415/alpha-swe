"""阶段二 2.3 自然语言创建技能测试：SkillAuthor 轨迹转换 / LLM 生成 / 落盘热加载。"""
from pathlib import Path

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, SkillConfig, PluginConfig)
from agent.context.skill import SkillLibrary
from agent.context.skill_author import SkillAuthor
from agent.core.loop import AgentLoop
from agent.core.task import Task, TaskStatus
from agent.llm import MockLLM


def write_skill(d: Path, name: str, body: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class _DecisionLogger:
    def __init__(self):
        self.rows = []

    def record(self, name, key, value, decision):
        self.rows.append(name)


# ---- 确定性：轨迹 -> 技能 ----
def test_from_trajectory_builds_sequential_dag():
    author = SkillAuthor()
    skill = author.from_trajectory(
        "fix-login-bug", "修复登录失败问题",
        [
            ("reproduce", "复现登录失败", "completed"),
            ("diagnose", "定位根因", "completed"),
            ("patch", "实施修复", "failed"),
        ],
    )
    steps = skill.steps
    assert [s.name for s in steps] == ["reproduce", "diagnose", "patch"]
    # 顺序依赖
    assert steps[0].dependencies == []
    assert steps[1].dependencies == ["reproduce"]
    assert steps[2].dependencies == ["diagnose"]
    # 失败步骤 -> on_failure=fallback + 重试指令
    assert steps[2].on_failure == "fallback"
    assert "重试" in steps[2].fallback
    assert steps[0].on_failure == "abort"
    # 触发器从名称/描述提取（含中文关键词）
    kws = skill.triggers.get("keywords", [])
    assert "fix" in kws and "bug" in kws and "登录" in kws


def test_trajectory_from_tasks_uses_metadata_and_status():
    tasks = [
        Task(id="t1", instruction="第一步",
             metadata={"skill_step": "step-a"},
             status=TaskStatus.COMPLETED),
        Task(id="t2", instruction="第二步",
             metadata={"skill_step": "step-b"},
             status=TaskStatus.FAILED),
        Task(id="t3", instruction="第三步",
             status=TaskStatus.COMPLETED),
    ]
    traj = SkillAuthor.trajectory_from_tasks(tasks)
    assert traj == [
        ("step-a", "第一步", "completed"),
        ("step-b", "第二步", "failed"),
        ("t3", "第三步", "completed"),
    ]


def test_yaml_text_roundtrip_loads_in_library(ws_tmp):
    author = SkillAuthor()
    skill = author.from_trajectory(
        "gen-migration", "生成数据库迁移",
        [("analyze", "分析变更", "completed"),
         ("migrate", "生成迁移脚本", "completed")],
    )
    yaml_text = SkillAuthor.yaml_text(skill)
    assert "name: gen-migration" in yaml_text
    d = ws_tmp / "skills"
    write_skill(d, skill.name, yaml_text)
    lib = SkillLibrary(skills_dir=str(d), registry_file="")
    loaded = lib.get("gen-migration")
    assert loaded is not None and len(loaded.steps) == 2
    assert loaded.steps[1].dependencies == ["analyze"]


def test_save_writes_and_hotloads(ws_tmp):
    d = ws_tmp / "skills"
    author = SkillAuthor(skills_dir=str(d), registry_file="",
                         decision_logger=_DecisionLogger())
    skill = author.from_trajectory(
        "audit-deps", "审计项目依赖安全",
        [("scan", "扫描依赖清单", "completed")],
    )
    path = author.save(skill)
    assert path == d / "audit-deps.yaml" and path.exists()
    # 落盘后可被 SkillLibrary 立即发现与校验
    lib = SkillLibrary(skills_dir=str(d), registry_file="")
    assert lib.get("audit-deps") is not None
    assert not lib.validate().get("audit-deps")
    assert [s.name for s in lib.match("审计一下依赖")] == ["audit-deps"]
    assert "skill.saved" in author.decision_logger.rows


# ---- LLM：自然语言 -> 技能 YAML ----
class _LLM(MockLLM):
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self._response


@pytest.mark.asyncio
async def test_from_llm_parses_yaml_codeblock():
    llm = _LLM("""```yaml
name: setup-env
description: 初始化开发环境
triggers:
  keywords: [setup, 环境]
steps:
  - name: install
    instruction: 安装依赖
  - name: verify
    instruction: 验证环境
    dependencies: [install]
```
""")
    author = SkillAuthor(llm=llm)
    skill = await author.from_llm("setup-env", "初始化开发环境", "设置本地开发环境")
    assert skill.name == "setup-env"
    assert [s.name for s in skill.steps] == ["install", "verify"]
    assert skill.steps[1].dependencies == ["install"]
    assert "setup" in skill.triggers.get("keywords", [])


@pytest.mark.asyncio
async def test_from_llm_falls_back_to_trajectory_on_garbage():
    llm = _LLM("这根本不是 YAML")
    author = SkillAuthor(llm=llm)
    skill = await author.from_llm(
        "save-skill", "保存技能", "把刚才的步骤保存为技能",
        trajectory=[("s1", "第一步", "completed"), ("s2", "第二步", "failed")],
    )
    assert skill.name == "save-skill"
    assert [s.name for s in skill.steps] == ["s1", "s2"]
    assert skill.steps[1].on_failure == "fallback"


# ---- 端到端：loop.save_skill ----
class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


BUG_FIX_SKILL = """name: bug-fix
version: "1.0.0"
description: 修复 Bug
priority: 9
triggers:
  keywords: [bug, 修复, 报错, 崩溃]
steps:
  - name: reproduce
    instruction: 复现问题并收集错误信息
    dependencies: []
  - name: diagnose
    instruction: 定位根因
    dependencies: [reproduce]
  - name: patch
    instruction: 实施最小修复
    dependencies: [diagnose]
  - name: regression
    instruction: 运行回归验证
    dependencies: [patch]
"""


def make_config(ws_tmp: Path, skills_dir: Path):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            auto_experience=False),
        mcp=MCPOptions(enabled=False),
        skills=SkillConfig(enabled=True, dir=str(skills_dir),
                           registry_file="", workflow_enabled=True,
                           max_active=3, allow_fallback=True),
        plugin=PluginConfig(enabled=False, dir=str(ws_tmp / "plugins")),
    )


@pytest.mark.asyncio
async def test_loop_save_skill_end_to_end(ws_tmp):
    d = ws_tmp / "skills"
    write_skill(d, "bug-fix", BUG_FIX_SKILL)
    llm = ScriptedLLM(*['{"final_answer": "ok"}'] * 4)
    loop = AgentLoop(config=make_config(ws_tmp, d), llm=llm)
    result = await loop.run("登录功能报错崩溃，帮我修复")
    assert result.ok
    path = loop.save_skill("capture-fix", "从修复轨迹保存的技能")
    saved = Path(path)
    assert saved.exists() and saved.name == "capture-fix.yaml"
    # 热加载后立即可发现；步骤来自已执行的任务轨迹
    lib = SkillLibrary(skills_dir=str(d), registry_file="")
    loaded = lib.get("capture-fix")
    assert loaded is not None
    assert len(loaded.steps) == 4
    assert [s.name for s in loaded.steps] == [
        "reproduce", "diagnose", "patch", "regression"]
