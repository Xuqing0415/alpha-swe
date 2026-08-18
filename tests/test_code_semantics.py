"""阶段一语义理解层测试：AST 摘要 / 调用图 / 项目约定提取 / 工具与 Prompt 注入。"""
import json

import pytest

from agent.code.ast_summary import is_code_file, summarize_file
from agent.code.call_graph import build_call_graph
from agent.code.project_profile import build_profile
from agent.config import LLMConfig
from agent.context.plugin import ProjectContext
from agent.core.task import Task
from agent.prompt.builder import PromptBuilder
from agent.tools.base import ExecutionContext
from agent.tools.fileio import FileIOTool

PY_SRC = '''"""demo"""
import os
from collections import OrderedDict


def add(a, b):
    return a + b


def helper(x):
    return add(x, 1)


class Calc:
    def __init__(self, base):
        self.base = base

    def run(self, n):
        return helper(n)
'''


# ---- 1.1 AST 感知文件读取 ----
def test_python_ast_summary_symbols():
    s = summarize_file("src/demo.py", PY_SRC)
    names = [x.name for x in s.symbols]
    assert "add" in names and "helper" in names
    assert "Calc" in names and "Calc.run" in names
    assert "os" in s.imports and "collections" in s.imports
    assert "add" in s.exports and "Calc" in s.exports
    assert s.language == "python"


def test_python_ast_summary_syntax_error_fallback():
    s = summarize_file("bad.py", "def broken(:")
    assert s.language == "python" and s.empty


def test_js_ts_summary_regex():
    ts = ("import { a } from './x';\n"
          "export const run = (n) => n + 1;\n"
          "export class App {}\n"
          "function util() {}\n")
    s = summarize_file("src/app.ts", ts)
    names = [x.name for x in s.symbols]
    assert "run" in names and "App" in names and "util" in names
    assert "run" in s.exports and "App" in s.exports
    assert any("x" in imp for imp in s.imports)


def test_is_code_file():
    assert is_code_file("a.py") and is_code_file("b.tsx")
    assert not is_code_file("readme.md") and not is_code_file("noext")


# ---- 1.1b tree-sitter 精确提取（阶段一增强） ----
def test_js_ts_tree_sitter_precise_symbols(ws_tmp):
    from agent.code import ts_parser
    if not ts_parser.available():
        pytest.skip("tree-sitter 未安装")
    ts = ("import { a } from './x';\n"
          "export const run = (n) => n + 1;\n"
          "export class App {\n"
          "  greet() { return run(this.n); }\n"
          "}\n"
          "function util() { run(1); }\n"
          "export interface Config { a: string }\n")
    s = summarize_file("src/app.ts", ts)
    kinds = {x.name: x.kind for x in s.symbols}
    assert kinds.get("run") == "const"
    assert kinds.get("App") == "class"
    assert kinds.get("App.greet") == "method"
    assert kinds.get("Config") == "interface"
    assert "run" in s.exports and "App" in s.exports
    args = {x.name: x.args for x in s.symbols}
    assert args.get("run") == "(n)"


def test_js_ts_tree_sitter_call_edges(ws_tmp):
    from agent.code import ts_parser
    if not ts_parser.available():
        pytest.skip("tree-sitter 未安装")
    (ws_tmp / "app.ts").write_text(
        "export const run = (n) => n + 1;\n"
        "export class App {\n"
        "  greet() { return run(this.n); }\n"
        "}\n"
        "function main() { const a = new App(); return a.greet(); }\n",
        encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    assert "run" in list(cg.callees_of("App.greet"))
    assert "greet" in list(cg.callees_of("main"))
    callers = [c for c, _ in cg.callers_of("run")]
    assert "App.greet" in callers


def test_js_ts_regex_fallback_when_tree_sitter_missing(ws_tmp, monkeypatch):
    from agent.code import ts_parser
    monkeypatch.setattr(ts_parser, "available", lambda: False)
    ts = ("import { a } from './x';\n"
          "export const run = (n) => n + 1;\n"
          "export class App {}\n"
          "function util() {}\n")
    s = summarize_file("src/app.ts", ts)
    names = [x.name for x in s.symbols]
    assert "run" in names and "App" in names and "util" in names
    assert "run" in s.exports and "App" in s.exports
    (ws_tmp / "app.js").write_text(
        "function util() {}\nfunction main() { util(); }\n", encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    assert "util" in cg.functions and "main" in cg.functions


# ---- 1.2 调用图 ----
def test_call_graph_python_edges(ws_tmp):
    (ws_tmp / "mod_a.py").write_text("def add(a, b): return a + b\n",
                                     encoding="utf-8")
    (ws_tmp / "mod_b.py").write_text(
        "from mod_a import add\n"
        "def run(x):\n"
        "    return add(x, 2)\n", encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    assert cg.symbol_count() >= 2
    assert "add" in cg.functions and "run" in cg.functions
    callers = [c for c, _ in cg.callers_of("add")]
    assert "run" in callers
    assert cg.files_of("add") == ["mod_a.py"]


def test_call_graph_js_approx(ws_tmp):
    (ws_tmp / "app.js").write_text(
        "function util() {}\n"
        "function main() { util(); }\n", encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    assert "util" in cg.functions and "main" in cg.functions
    # main 调用 util（tree-sitter 精确边；旧正则曾把定义行误算成自调用）
    assert any(c == "main" for c, _ in cg.callers_of("util"))


def test_call_graph_empty_on_missing_root():
    cg = build_call_graph("__no_such_dir__")
    assert cg.symbol_count() == 0 and cg.to_text() == ""


# ---- 1.3 项目约定/技术栈 ----
def test_project_profile_tech_stack(ws_tmp):
    (ws_tmp / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18.2.0", "express": "~4.18.0"},
        "devDependencies": {"typescript": "^5.0.0", "jest": "^29.0.0"},
    }), encoding="utf-8")
    (ws_tmp / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"strict": True, "target": "ES2020"},
    }), encoding="utf-8")
    (ws_tmp / "src").mkdir()
    (ws_tmp / "src" / "index.ts").write_text("export const x = 1\n",
                                             encoding="utf-8")
    profile = build_profile(str(ws_tmp),
                            ["package.json", "tsconfig.json", "src/index.ts"])
    stack = " ".join(profile.tech_stack)
    assert "React 18" in stack and "Express 4" in stack
    assert "TypeScript" in stack
    assert any("strict" in c for c in profile.conventions)
    assert "src" in profile.structure
    text = profile.to_text()
    assert "技术栈" in text and "React 18" in text


def test_project_profile_python(ws_tmp):
    (ws_tmp / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n'
        'dependencies = ["fastapi"]\n', encoding="utf-8")
    profile = build_profile(str(ws_tmp), ["pyproject.toml"])
    stack = " ".join(profile.tech_stack)
    assert "FastAPI" in stack
    assert any("Python" in c for c in profile.conventions)


# ---- 工具层注入 ----
@pytest.mark.asyncio
async def test_fileio_read_includes_ast_summary(ws_tmp):
    (ws_tmp / "calc.py").write_text(PY_SRC, encoding="utf-8")
    tool = FileIOTool()
    r = await tool.execute({"action": "read", "path": "calc.py"},
                           ExecutionContext(workspace=str(ws_tmp)))
    assert r.success
    assert "def add(a, b)" in r.output
    assert "[代码结构摘要]" in r.output
    assert "Calc.run" in r.output
    assert r.metadata.get("ast_summary") is True


@pytest.mark.asyncio
async def test_fileio_read_plain_file_no_summary(ws_tmp):
    (ws_tmp / "note.txt").write_text("hello", encoding="utf-8")
    r = await FileIOTool().execute({"action": "read", "path": "note.txt"},
                                   ExecutionContext(workspace=str(ws_tmp)))
    assert r.success and r.output == "hello"
    assert r.metadata.get("ast_summary") is not True


class _DecisionLogger:
    def __init__(self):
        self.rows = []

    def record(self, name, key, value, decision):
        self.rows.append(name)


@pytest.mark.asyncio
async def test_fileio_records_symbol_and_call_graph_decisions(ws_tmp):
    (ws_tmp / "mod_a.py").write_text("def add(a, b): return a + b\n",
                                     encoding="utf-8")
    (ws_tmp / "mod_b.py").write_text(
        "from mod_a import add\n"
        "def run(x):\n"
        "    return add(x, 1)\n", encoding="utf-8")
    cg = build_call_graph(str(ws_tmp))
    dl = _DecisionLogger()
    tool = FileIOTool(call_graph=cg, decision_logger=dl)
    r = await tool.execute({"action": "read", "path": "mod_b.py"},
                           ExecutionContext(workspace=str(ws_tmp)))
    assert "symbol.retrieved" in dl.rows
    assert "call_graph.hit" in dl.rows
    assert "run 调用 add" in r.output


# ---- Prompt 注入 ----
def test_prompt_builder_injects_project_profile():
    pb = PromptBuilder(tool_schemas=[], llm_config=LLMConfig(provider="mock"))
    pb.set_project_profile("## 项目约定与技术栈\n- 技术栈: React 18")
    msgs = pb.build(Task(id="t", instruction="改一个组件"))
    assert "React 18" in msgs[0]["content"]


def test_prompt_builder_no_profile_when_unset():
    pb = PromptBuilder(tool_schemas=[], llm_config=LLMConfig(provider="mock"))
    msgs = pb.build(Task(id="t", instruction="x"))
    assert "项目约定与技术栈" not in msgs[0]["content"]


# ---- ProjectContext 集成 ----
def test_project_context_scan_attaches_profile_and_call_graph(ws_tmp):
    (ws_tmp / "package.json").write_text(json.dumps(
        {"dependencies": {"react": "^18.0.0"}}), encoding="utf-8")
    (ws_tmp / "src").mkdir()
    (ws_tmp / "src" / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
    pc = ProjectContext.scan(str(ws_tmp), max_depth=2, max_files=50)
    assert pc.profile is not None and "React" in pc.profile_text
    assert pc.call_graph is not None and pc.call_graph.symbol_count() >= 1
    # merge 保留 base 的 profile/call_graph
    merged = ProjectContext.from_instruction("src/b.py", pc)
    assert merged.profile is pc.profile
    assert merged.call_graph is pc.call_graph