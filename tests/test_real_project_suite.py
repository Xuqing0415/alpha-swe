"""真实项目测试集（阶段二 2.4 验证方式 + 贯穿性测试集积累）。

用带有真实技术栈气息的迷你项目（Express/Django+SQLAlchemy/pytest/Flask），
验证技能发现效果：
- discover() 在陌生项目上按 任务关键词 + 项目依赖/文件类型 命中正确技能；
- 端到端 AgentLoop 运行技能工作流，确认 skills_activated 事件与步骤执行顺序。

运行：python -X utf8 -m pytest tests/test_real_project_suite.py -q
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig, SkillConfig, PluginConfig)
from agent.context.plugin import ProjectContext
from agent.context.skill import SkillLibrary
from agent.core.loop import AgentLoop
from agent.llm import MockLLM


@dataclass
class Case:
    """一个真实项目场景：项目文件 + 用户任务 + 期望技能。"""
    name: str
    files: Dict[str, str]
    task: str
    expected: List[str]          # 期望 discover() 命中的技能
    deps: List[str] = field(default_factory=list)


def write_project(ws_tmp: Path, files: Dict[str, str]) -> None:
    for rel, body in files.items():
        p = ws_tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


# ---- 真实项目场景 ----
EXPRESS_PKG = json.dumps({
    "name": "order-svc",
    "dependencies": {"express": "^4.18.0"},
    "devDependencies": {"jest": "^29.0.0"},
})
EXPRESS_APP = """const express = require('express');
const app = express();
module.exports = app;
"""
ORDER_MODEL = """const orders = [];
function listOrders() { return orders; }
function createOrder(data) { orders.push(data); return data; }
module.exports = { listOrders, createOrder };
"""

PYPROJECT_SQLA = """[project]
name = "blog"
requires-python = ">=3.11"
dependencies = ["sqlalchemy", "fastapi"]
"""
MODELS_PY = """from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(50))
"""

REQ_PYTEST = """pytest==8.0.0
requests==2.31.0
"""
UTILS_PY = """def parse_csv(text):
    return [row.split(",") for row in text.strip().splitlines() if row]
"""

FLASK_APP = """from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    user = data["username"]  # KeyError: missing key -> 500
    return jsonify(ok=True, user=user)
"""

REQ_FLASK = """flask==3.0.0
"""

# ---- 新增夹具：多技术栈（koa / fastapi / django / typeorm / prisma /
#      sequelize / jest / vitest / 多语言重构与 bug 变体） ----
KOA_PKG = json.dumps({
    "name": "shop-svc",
    "dependencies": {"koa": "^2.14.0", "@koa/router": "^12.0.0"},
})
KOA_APP = """const Koa = require('koa');
const Router = require('@koa/router');
const app = new Koa();
const router = new Router();
module.exports = { app, router };
"""

PYPROJECT_FASTAPI = """[project]
name = "orders"
requires-python = ">=3.11"
dependencies = ["fastapi", "uvicorn"]
"""
FASTAPI_MAIN = """from fastapi import FastAPI, APIRouter
app = FastAPI()
router = APIRouter(prefix="/api")
app.include_router(router)
"""

REQ_DJANGO = """django==5.0.0
"""
DJANGO_MODELS = """from django.db import models

class User(models.Model):
    name = models.CharField(max_length=50)
"""

TYPEORM_PKG = json.dumps({
    "name": "user-svc",
    "dependencies": {"typeorm": "^0.3.0", "reflect-metadata": "^0.2.0"},
})
USER_ENTITY_TS = """import { Entity, PrimaryGeneratedColumn, Column } from "typeorm";

@Entity("users")
export class User {
  @PrimaryGeneratedColumn()
  id: number;
  @Column()
  name: string;
}
"""

PRISMA_PKG = json.dumps({
    "name": "blog",
    "dependencies": {"@prisma/client": "^5.0.0"},
    "devDependencies": {"prisma": "^5.0.0"},
})
PRISMA_SCHEMA = """generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id   Int    @id @default(autoincrement())
  name String
}
"""

SEQ_PKG = json.dumps({
    "name": "legacy",
    "dependencies": {"sequelize": "^6.0.0"},
})
SEQ_USER_JS = """const { Sequelize, DataTypes } = require('sequelize');
const sequelize = new Sequelize('sqlite::memory:');
const User = sequelize.define('User', {
  id: { type: DataTypes.INTEGER, primaryKey: true },
  name: DataTypes.STRING,
});
module.exports = User;
"""

JEST_PKG = json.dumps({
    "name": "calc",
    "devDependencies": {"jest": "^29.0.0"},
})
CALC_JS = """function add(a, b) { return a + b; }
module.exports = { add };
"""

VITEST_PKG = json.dumps({
    "name": "calc-ts",
    "devDependencies": {"vitest": "^1.0.0"},
})
CALC_TS = """export function add(a: number, b: number): number {
  return a + b;
}
"""

JUST_PKG = json.dumps({"name": "ts-svc", "private": True})

ORDER_TS = """export interface Order {
  id: number;
  total: number;
}
export function computeTotal(items: { price: number; qty: number }[]) {
  return items.reduce((sum, it) => sum + it.price * it.qty, 0);
}
"""

CRASH_APP_JS = """const express = require('express');
const app = express();
app.get('/home', (req, res) => {
  const user = req.session.user;  // TypeError: cannot read
  res.json({ name: user.name });
});
module.exports = app;
"""

SERVICE_TS = """export function getUserName(user: any): string {
  return user.profile.name;  // null -> crash
}
"""

REQ_BUG = """requests==2.31.0
"""
MAIN_PY = """def load_config(path):
    data = {}
    return data["api_key"]  # KeyError
"""

EXPRESS_PKG = json.dumps({
    "name": "order-svc",
    "dependencies": {"express": "^4.18.0"},
    "devDependencies": {"jest": "^29.0.0"},
})
EXPRESS_APP = """const express = require('express');
const app = express();
module.exports = app;
"""
ORDER_MODEL = """const orders = [];
function listOrders() { return orders; }
function createOrder(data) { orders.push(data); return data; }
module.exports = { listOrders, createOrder };
"""

PYPROJECT_SQLA = """[project]
name = "blog"
requires-python = ">=3.11"
dependencies = ["sqlalchemy", "fastapi"]
"""
MODELS_PY = """from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(50))
"""

REQ_PYTEST = """pytest==8.0.0
requests==2.31.0
"""
UTILS_PY = """def parse_csv(text):
    return [row.split(",") for row in text.strip().splitlines() if row]
"""

FLASK_APP = """from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    user = data["username"]  # KeyError: missing key -> 500
    return jsonify(ok=True, user=user)
"""

REQ_FLASK = """flask==3.0.0
"""

CASES: List[Case] = [
    Case(
        name="express-rest-endpoint",
        files={"package.json": EXPRESS_PKG, "src/app.js": EXPRESS_APP,
               "src/orders.js": ORDER_MODEL},
        task="为订单模块添加一个 /api/orders 的 REST 端点，支持列出与创建订单",
        expected=["add-rest-endpoint"],
        deps=["express"],
    ),
    Case(
        name="django-sqlalchemy-migration",
        files={"pyproject.toml": PYPROJECT_SQLA, "app/models.py": MODELS_PY},
        task="给 User 模型加一个 email 字段并生成数据库迁移",
        expected=["db-migration"],
        deps=["sqlalchemy"],
    ),
    Case(
        name="pytest-unit-tests",
        files={"requirements.txt": REQ_PYTEST, "src/utils.py": UTILS_PY},
        task="给 utils.py 的 parse_csv 写单元测试，覆盖正常与空行输入",
        expected=["test-generation"],
        deps=["pytest"],
    ),
    Case(
        name="flask-bugfix",
        files={"requirements.txt": REQ_FLASK, "app.py": FLASK_APP},
        task="登录功能一直报错 500 崩溃，帮我修复这个 bug",
        expected=["bug-fix"],
        deps=["flask"],
    ),
    Case(
        name="fuzzy-description",
        files={"requirements.txt": REQ_PYTEST, "src/utils.py": UTILS_PY},
        task="这个东西最近总出问题，有时候直接崩溃，帮我看看",
        expected=["bug-fix"],
        deps=[],
    ),
    # ---- REST 端点：多后端框架（add-rest-endpoint + 框架约定） ----
    Case(
        name="koa-rest-endpoint",
        files={"package.json": KOA_PKG, "src/app.js": KOA_APP},
        task="为商品模块添加一个 /api/products 的 REST 端点，支持列出与创建商品",
        expected=["add-rest-endpoint", "koa"],
        deps=["koa"],
    ),
    Case(
        name="fastapi-rest-endpoint",
        files={"pyproject.toml": PYPROJECT_FASTAPI,
               "app/main.py": FASTAPI_MAIN},
        task="为订单模块添加一个 /api/orders 端点，用 Pydantic 做参数校验",
        expected=["add-rest-endpoint", "fastapi", "python"],
        deps=["fastapi"],
    ),
    Case(
        name="django-rest-endpoint",
        files={"requirements.txt": REQ_DJANGO, "app/models.py": DJANGO_MODELS},
        task="添加一个 /api/users 的 REST 端点，返回用户列表",
        expected=["add-rest-endpoint", "django", "python"],
        deps=["django"],
    ),
    # ---- ORM 迁移：多技术栈（db-migration + ORM 约定） ----
    Case(
        name="typeorm-migration",
        files={"package.json": TYPEORM_PKG,
               "src/entities/User.ts": USER_ENTITY_TS},
        task="给 User 实体加一个 email 字段并生成数据库迁移",
        expected=["db-migration", "typeorm"],
        deps=["typeorm"],
    ),
    Case(
        name="prisma-migration",
        files={"package.json": PRISMA_PKG,
               "prisma/schema.prisma": PRISMA_SCHEMA},
        task="更新 Prisma schema，给 User 加字段并生成迁移",
        expected=["db-migration", "prisma"],
        deps=["prisma"],
    ),
    Case(
        name="sequelize-migration",
        files={"package.json": SEQ_PKG, "src/models/user.js": SEQ_USER_JS},
        task="给 User 模型加一个 email 字段并生成迁移脚本",
        expected=["db-migration", "sequelize"],
        deps=["sequelize"],
    ),
    # ---- 测试生成：多框架（test-generation + 框架约定） ----
    Case(
        name="jest-test",
        files={"package.json": JEST_PKG, "src/calc.js": CALC_JS},
        task="给 calc.js 的 add 函数写单元测试，覆盖正常与边界输入",
        expected=["test-generation", "jest"],
        deps=["jest"],
    ),
    Case(
        name="vitest-test",
        files={"package.json": VITEST_PKG, "src/calc.ts": CALC_TS},
        task="给 calc.ts 的 add 函数写单元测试，覆盖正常与边界输入",
        expected=["test-generation", "vitest"],
        deps=["vitest"],
    ),
    # ---- 重构：语言范围限定（python-refactor vs refactor-js 互斥） ----
    Case(
        name="python-refactor-utils",
        files={"src/utils.py": UTILS_PY},
        task="重构 src/utils.py，把 parse_csv 拆分成更小的函数并优化代码",
        expected=["python-refactor", "python"],
        deps=[],
    ),
    Case(
        name="js-ts-refactor-order",
        files={"package.json": JUST_PKG, "src/order.ts": ORDER_TS},
        task="重构 src/order.ts，把计算逻辑提取成独立函数",
        expected=["refactor-js"],
        deps=[],
    ),
    # ---- Bug 修复：多语言变体 ----
    Case(
        name="js-crash-bug",
        files={"package.json": EXPRESS_PKG, "src/app.js": CRASH_APP_JS},
        task="首页接口一直报错 500，帮我修复这个 bug",
        expected=["bug-fix"],
        deps=["express"],
    ),
    Case(
        name="ts-null-bug",
        files={"package.json": JUST_PKG, "src/service.ts": SERVICE_TS},
        task="详情页渲染崩溃，提示 cannot read properties of null，修复这个 bug",
        expected=["bug-fix"],
        deps=[],
    ),
    Case(
        name="python-error-bug",
        files={"requirements.txt": REQ_BUG, "src/main.py": MAIN_PY},
        task="运行脚本报 KeyError，帮我修复这个 bug",
        expected=["bug-fix"],
        deps=[],
    ),
]


# ---- 1) 发现层验证：陌生项目上自动命中正确技能 ----
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_discovery_hits_expected_skills(ws_tmp, case: Case):
    write_project(ws_tmp, case.files)
    pc = ProjectContext.scan(str(ws_tmp), max_depth=3, max_files=100)
    lib = SkillLibrary(skills_dir=str(ws_tmp.parent.parent / "skills" / "workflows"),
                       registry_file=str(ws_tmp.parent.parent / "skills" / "skill_manifest.json"),
                       enabled=True)
    discovered = lib.discover(case.task, files=pc.files, deps=pc.deps)
    names = [s.name for s in discovered]
    for expected in case.expected:
        assert expected in names, (
            f"[{case.name}] 期望命中 {expected}，实际发现: {names}"
        )


def test_discovery_prefers_priority_and_returns_requires_closure(ws_tmp):
    write_project(ws_tmp, EXPRESS_PKG and {"package.json": EXPRESS_PKG,
                                           "src/orders.js": ORDER_MODEL})
    pc = ProjectContext.scan(str(ws_tmp), max_depth=3, max_files=50)
    lib = SkillLibrary(skills_dir=str(ws_tmp.parent.parent / "skills" / "workflows"),
                       registry_file="", enabled=True)
    matched = lib.match("修复登录接口的 bug", files=pc.files, deps=pc.deps)
    assert matched, "应至少命中一个技能"
    # 优先级降序：bug-fix priority=9 应排在 add-rest-endpoint(8) 前
    assert matched[0].name == "bug-fix"


_UNRELATED_TASKS = [
    ("translate-readme", {"package.json": EXPRESS_PKG}, "把 README 翻译成英文"),
    ("weekly-report", {"requirements.txt": REQ_DJANGO}, "写一封周报邮件给团队"),
    ("deploy-server", {"pyproject.toml": PYPROJECT_FASTAPI}, "把项目部署到服务器"),
    ("arrange-schedule", {"package.json": JUST_PKG}, "安排明天的日程"),
    ("change-logo", {"package.json": EXPRESS_PKG}, "把 LOGO 换一个颜色"),
]


@pytest.mark.parametrize("name,files,task", _UNRELATED_TASKS,
                         ids=[c[0] for c in _UNRELATED_TASKS])
def test_discovery_no_false_positive_on_unrelated_task(ws_tmp, name, files, task):
    """与任何技能都无关的任务不应误命中（负例集，量化误报率）。"""
    write_project(ws_tmp, files)
    pc = ProjectContext.scan(str(ws_tmp), max_depth=2, max_files=50)
    lib = SkillLibrary(skills_dir=str(ws_tmp.parent.parent / "skills" / "workflows"),
                       registry_file="", enabled=True)
    assert lib.match(task, files=pc.files, deps=pc.deps) == []

# ---- 2) 端到端激活验证：AgentLoop 技能工作流执行 ----
class ScriptedLLM(MockLLM):
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


def make_config(ws_tmp: Path, skills_dir: Path, registry: str):
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db"),
                            auto_experience=False),
        mcp=MCPOptions(enabled=False),
        skills=SkillConfig(enabled=True, dir=str(skills_dir),
                           registry_file=registry,
                           usage_log=str(ws_tmp / "skill_usage.jsonl"),
                           workflow_enabled=True, max_active=3,
                           allow_fallback=True),
        plugin=PluginConfig(enabled=False, dir=str(ws_tmp / "plugins")),
    )


@pytest.mark.asyncio
async def test_end_to_end_skill_activation_and_order(ws_tmp):
    skills_dir = ws_tmp.parent.parent / "skills" / "workflows"
    registry = str(ws_tmp.parent.parent / "skills" / "skill_manifest.json")
    write_project(ws_tmp, dict(CASES[0].files))
    case = CASES[0]  # express-rest-endpoint
    lib = SkillLibrary(skills_dir=str(skills_dir), registry_file=registry,
                       enabled=True)
    skill = lib.get("add-rest-endpoint")
    assert skill is not None
    # 每个步骤一个 final_answer
    llm = ScriptedLLM(*['{"final_answer": "ok"}'] * len(skill.steps))
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir, registry), llm=llm)
    result = await loop.run(case.task)
    assert result.ok
    activated = [e for e in loop.events if e["type"] == "skills_activated"]
    assert activated, "应有 skills_activated 事件"
    assert "add-rest-endpoint" in activated[0]["data"]["skills"]
    # 决策日志包含技能激活与展开
    names = [dp.name for dp in loop._decision.decisions]
    assert "skill.activate" in names and "skill.expand" in names
    # 技能步骤按依赖顺序执行
    step_order = [e["data"]["task_id"] for e in loop.events
                  if e["type"] == "task_start"]
    skill_tasks = [t for t in step_order if "add-rest-endpoint" in t]
    assert skill_tasks == [
        "add-rest-endpoint::route", "add-rest-endpoint::validation",
        "add-rest-endpoint::controller", "add-rest-endpoint::tests",
        "add-rest-endpoint::run-tests",
    ]
    # 任务开始事件携带技能进度字段（阶段二 2.4 可视化数据源）
    skill_starts = [e for e in loop.events if e["type"] == "task_start"
                    and e["data"].get("skill_step")]
    assert skill_starts and skill_starts[0]["data"]["skill_step"] == "route"
    assert skill_starts[0]["data"]["step_index"] == 0
    assert skill_starts[0]["data"]["step_total"] == 5


@pytest.mark.asyncio
async def test_end_to_end_bugfix_skill_executes(ws_tmp):
    skills_dir = ws_tmp.parent.parent / "skills" / "workflows"
    registry = str(ws_tmp.parent.parent / "skills" / "skill_manifest.json")
    write_project(ws_tmp, dict(CASES[3].files))  # flask-bugfix
    lib = SkillLibrary(skills_dir=str(skills_dir), registry_file=registry,
                       enabled=True)
    skill = lib.get("bug-fix")
    llm = ScriptedLLM(*['{"final_answer": "已修复"}'] * len(skill.steps))
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir, registry), llm=llm)
    result = await loop.run(CASES[3].task)
    assert result.ok
    activated = [e for e in loop.events if e["type"] == "skills_activated"]
    assert activated and "bug-fix" in activated[0]["data"]["skills"]
    # 使用记录（版本管理）写入
    summary = loop.skill_library.usage_summary()
    assert summary.get("bug-fix", {}).get("activated", 0) >= 1


# ---- 3) 量化：技能发现命中率 / 语言限定 / 视图联动事件契约 ----
def test_discovery_stats_hit_rate(ws_tmp):
    """量化技能发现：全部场景的期望技能命中率与负例误报率统计。"""
    from agent.context.plugin import ProjectContext
    from agent.context.skill import SkillLibrary

    skills_dir = ws_tmp.parent.parent / "skills" / "workflows"
    registry = str(ws_tmp.parent.parent / "skills" / "skill_manifest.json")
    lib = SkillLibrary(skills_dir=str(skills_dir), registry_file=registry,
                       enabled=True)
    stats = {"total": 0, "hit": 0, "missing": []}
    for case in CASES:
        ws = ws_tmp / case.name
        write_project(ws, case.files)
        pc = ProjectContext.scan(str(ws), max_depth=3, max_files=100)
        names = [s.name for s in lib.discover(case.task, files=pc.files,
                                              deps=pc.deps)]
        stats["total"] += len(case.expected)
        ok = [e for e in case.expected if e in names]
        stats["hit"] += len(ok)
        miss = [e for e in case.expected if e not in names]
        if miss:
            stats["missing"].append((case.name, miss, names))
    # 控制全部场景：期望技能必须全部命中（精确量化，不依赖概率）
    assert stats["missing"] == [], stats["missing"]
    assert stats["hit"] == stats["total"]
    # 负例误报率：全部无关任务必须零命中
    fp = 0
    for _name, files, task in _UNRELATED_TASKS:
        ws = ws_tmp / ("neg_" + _name)
        write_project(ws, files)
        pc = ProjectContext.scan(str(ws), max_depth=2, max_files=50)
        if lib.match(task, files=pc.files, deps=pc.deps):
            fp += 1
    assert fp == 0, f"负例误报 {fp} 个"
    # 结果输出到 stdout（-s 时可见）：命中率 / 误报率 / 技能库规模
    total_cases = len(CASES) + len(_UNRELATED_TASKS)
    print(f"[技能发现量化] 场景数={total_cases} "
          f"命中率={stats['hit']}/{stats['total']} "
          f"误报率={fp}/{len(_UNRELATED_TASKS)} "
          f"技能库={len(lib.list_skills())}个")


def test_refactor_language_scoping(ws_tmp):
    """语言范围限定：Python/JS 重构任务不会跨技术栈误命中。"""
    from agent.context.plugin import ProjectContext
    from agent.context.skill import SkillLibrary

    lib = SkillLibrary(
        skills_dir=str(ws_tmp.parent.parent / "skills" / "workflows"),
        registry_file=str(ws_tmp.parent.parent / "skills" / "skill_manifest.json"),
        enabled=True)
    # Python 项目：重构任务命中 python-refactor，不命中 refactor-js
    py = ws_tmp / "pyproj"
    (py / "src").mkdir(parents=True)
    (py / "src" / "utils.py").write_text("x = 1\n", encoding="utf-8")
    pc = ProjectContext.scan(str(py), max_depth=2)
    names = [s.name for s in lib.match(
        "重构 utils.py，把 parse_csv 拆成小函数", files=pc.files, deps=pc.deps)]
    assert "python-refactor" in names
    assert "refactor-js" not in names
    # JS 项目：重构任务命中 refactor-js，不命中 python-refactor
    js = ws_tmp / "jsproj"
    (js / "src").mkdir(parents=True)
    (js / "src" / "order.ts").write_text("export const x: number = 1;\n",
                                         encoding="utf-8")
    pc2 = ProjectContext.scan(str(js), max_depth=2)
    names2 = [s.name for s in lib.match(
        "重构 order.ts，把计算逻辑提取成独立函数", files=pc2.files,
        deps=pc2.deps)]
    assert "refactor-js" in names2
    assert "python-refactor" not in names2


@pytest.mark.asyncio
async def test_view_linkage_skill_events_feed_tui(ws_tmp):
    """视图联动量化：技能工作流真实事件流满足 TUI 各视图的消费契约。

    - 主日志：skills_activated / task_start（技能进度 1/5）可被 format_event 渲染；
    - 任务面板：每个 task_start 都有对应 task_done（状态完整闭环）；
    - 时间线：tracer 记录了足够 span 供火焰图/瀑布图渲染。
    """
    from tui.formatting import format_event

    skills_dir = ws_tmp.parent.parent / "skills" / "workflows"
    registry = str(ws_tmp.parent.parent / "skills" / "skill_manifest.json")
    write_project(ws_tmp, dict(CASES[0].files))  # express-rest-endpoint
    lib = SkillLibrary(skills_dir=str(skills_dir), registry_file=registry,
                       enabled=True)
    skill = lib.get("add-rest-endpoint")
    assert skill is not None
    llm = ScriptedLLM(*['{"final_answer": "ok"}'] * len(skill.steps))
    loop = AgentLoop(config=make_config(ws_tmp, skills_dir, registry), llm=llm)
    result = await loop.run(CASES[0].task)
    assert result.ok

    activated = [e for e in loop.events if e["type"] == "skills_activated"]
    assert activated and activated[0]["data"].get("total", 0) == len(skill.steps)
    # 主日志渲染链：真实事件可直接被 TUI format_event 消费
    rendered = format_event(activated[0])
    assert "技能工作流激活" in rendered.plain
    assert "add-rest-endpoint" in rendered.plain

    # 任务面板/日志：每个技能步骤都有 task_start，且携带完整进度字段
    starts = [e for e in loop.events
              if e["type"] == "task_start" and e["data"].get("skill")]
    assert len(starts) == len(skill.steps)
    for i, e in enumerate(starts):
        d = e["data"]
        assert d["skill_step"] == skill.steps[i].name
        assert d["step_index"] == i
        assert d["step_total"] == len(skill.steps)
        text = format_event(e)
        assert f"{i + 1}/{len(skill.steps)}" in text.plain
        assert d["skill_step"] in text.plain

    # 任务面板状态闭环：每个启动的任务都有完成事件
    done_ids = {e["data"]["task_id"] for e in loop.events
                if e["type"] == "task_done"}
    start_ids = {e["data"]["task_id"] for e in loop.events
                 if e["type"] == "task_start"}
    assert start_ids and start_ids <= done_ids

    # 时间线数据源：至少为每个技能步骤记录一个 span
    timeline = loop.tracer.get_timeline_data(limit=500)
    assert len(timeline) >= len(skill.steps)
    assert all(r.get("duration", 0) >= 0 for r in timeline)
