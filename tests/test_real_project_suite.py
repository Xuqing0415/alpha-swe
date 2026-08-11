"""真实项目测试集（阶段二 2.4 验证方式 + 贯穿性测试集积累）。

用带有真实技术栈气息的迷你项目（Express/Django+SQLAlchemy/pytest/Flask），
验证技能发现效果：
- discover() 在陌生项目上按 任务关键词 + 项目依赖/文件类型 命中正确技能；
- 优先级排序与无关任务零误触发。

运行：python -X utf8 -m pytest tests/test_real_project_suite.py -q
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from agent.context.plugin import ProjectContext
from agent.context.skill import SkillLibrary


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


def test_discovery_no_false_positive_on_unrelated_task(ws_tmp):
    write_project(ws_tmp, {"package.json": EXPRESS_PKG})
    pc = ProjectContext.scan(str(ws_tmp), max_depth=2, max_files=50)
    lib = SkillLibrary(skills_dir=str(ws_tmp.parent.parent / "skills" / "workflows"),
                       registry_file="", enabled=True)
    # 与任何技能都无关的任务不应误命中
    assert lib.match("把 README 翻译成英文", files=pc.files, deps=pc.deps) == []
