# -*- coding: utf-8 -*-
"""Direction 2: multi-language parsing / test-runner / dependency / new tools.

Offline tests: tree-sitter is optional, so the regex fallback path is what we
exercise here (tree-sitter path stays defensive).
"""
import sqlite3

import pytest

from agent.code.language_parser import (detect_language, parse_file,
                                        tree_sitter_available)
from agent.code.ast_summary import summarize_file, is_code_file
from agent.code.call_graph import build_call_graph
from agent.code.test_runner import (_build_argv, parse_test_output)
from agent.code.dependency_manager import (dependency_report,
                                           parse_manifest)
from agent.tools.base import ExecutionContext
from agent.tools.database_tool import DatabaseTool
from agent.tools.dependency_tool import DependencyTool
from agent.tools.cloud_tool import CloudTool


# ---------------------------------------------------------------- language_parser
def test_detect_language():
    assert detect_language("Main.java") == "java"
    assert detect_language("lib.rs") == "rust"
    assert detect_language("main.go") == "go"
    assert detect_language("a.cpp") == "cpp"
    assert detect_language("x.cs") == "csharp"
    assert detect_language("y.rb") == "ruby"
    assert detect_language("z.php") == "php"
    assert detect_language("u.c") == "c"
    assert detect_language("foo.txt") == ""


def test_parse_java():
    pf = parse_file("Main.java",
                    "import java.util.List;\n"
                    "public class Main {\n"
                    "    public int add(int a, int b) { return sum(a, b); }\n"
                    "}\n"
                    "interface Runner { void run(); }\n")
    kinds = {s.name: s.kind for s in pf.symbols}
    assert kinds["Main"] == "class"
    assert kinds["add"] == "method"
    assert ("add", "sum") in pf.calls
    assert "java.util.List" in pf.imports


def test_parse_go_and_calls():
    pf = parse_file("main.go",
                    "package main\nimport \"fmt\"\n"
                    "func main() { fmt.Println(hello()) }\n"
                    "func hello() string { return \"hi\" }\n"
                    "type S struct { X int }\n")
    names = {s.name for s in pf.symbols}
    assert {"main", "hello", "S"} <= names
    assert ("main", "hello") in pf.calls
    assert "fmt" in pf.imports


def test_parse_rust():
    pf = parse_file("lib.rs",
                    "use std::collections::HashMap;\n"
                    "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
                    "struct Point { x: i32 }\n"
                    "enum Color { Red }\n")
    kinds = {s.name: s.kind for s in pf.symbols}
    assert kinds["add"] == "function"
    assert kinds["Point"] == "type"
    assert kinds["Color"] == "type"


def test_parse_cpp_calls():
    pf = parse_file("main.cpp",
                    "#include <iostream>\n"
                    "class Greeter {\npublic:\n"
                    "    std::string greet(const std::string& name) "
                    "{ return hi(name); }\n};\n"
                    "int main() { Greeter g; return 0; }\n")
    names = {s.name for s in pf.symbols}
    assert {"Greeter", "greet", "main"} <= names
    assert ("greet", "hi") in pf.calls
    assert "iostream" in pf.imports


def test_parse_csharp_ruby_php():
    cs = parse_file("Calc.cs", "using System;\npublic class Calc {\n"
                    "  public int Add(int a, int b) { return Sum(a, b); }\n}\n")
    assert {s.name for s in cs.symbols} == {"Calc", "Add"}
    assert ("Add", "Sum") in cs.calls
    rb = parse_file("app.rb", "require 'json'\nclass User\n"
                    "  def name\n    fetch_name()\n  end\nend\n")
    assert rb.symbols[0].kind == "class"
    assert ("name", "fetch_name") in rb.calls
    ph = parse_file("index.php",
                    "<?php\nclass Index {\n"
                    "  public function run($id) { return find($id); }\n}\n")
    kinds = {s.name: s.kind for s in ph.symbols}
    assert kinds["Index"] == "type" and kinds["run"] == "method"


# ---------------------------------------------------------------- ast_summary / call_graph
def test_ast_summary_java(ws_tmp):
    s = summarize_file("Main.java",
                       "import java.util.List;\n"
                       "public class Main { public int add() { return 1; } }\n")
    assert s.language == "java"
    assert any(x.name == "Main" for x in s.symbols)
    assert "java.util.List" in s.imports
    assert is_code_file("x.go")


def test_call_graph_multilang(ws_tmp):
    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "main.go").write_text(
        "package main\nfunc main() { hello() }\nfunc hello() {}\n",
        encoding="utf-8")
    (ws / "src" / "Main.java").write_text(
        "public class Main {\n  int add() { return sum(); }\n}\n",
        encoding="utf-8")
    cg = build_call_graph(str(ws))
    assert "main" in cg.defs and "add" in cg.defs
    assert "hello" in cg.calls.get("main", set())
    assert "sum" in cg.calls.get("add", set())


def test_tree_sitter_optional_api():
    # tree-sitter 未安装时也应能正常报告不可用，不抛异常
    assert tree_sitter_available("java") in (True, False)


# ---------------------------------------------------------------- test_runner
def test_parse_maven_gradle_output():
    out = ("[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0\n"
           "[ERROR]   com.example.FooTest.testBar:12 "
           "expected:<1> but was:<2>\n")
    fails = parse_test_output("maven", out)
    assert fails and fails[0].name == "com.example.FooTest.testBar"
    assert "expected" in fails[0].reason


def test_parse_cargo_output():
    out = ("failures:\n"
           "---- test_add stdout ----\n"
           "panicked at src/lib.rs:4\n"
           "\ntest result: FAILED. 1 failed; 0 passed\n")
    fails = parse_test_output("cargo", out)
    assert any(f.name == "test_add" for f in fails)


def test_parse_ctest_output():
    out = ("The following tests FAILED:\n"
           "\t  1 - math (Failed)\n")
    fails = parse_test_output("ctest", out)
    assert any(f.name == "math" for f in fails)


def test_parse_go_output():
    fails = parse_test_output("go", "--- FAIL: TestAdd (0.00s)\nFAIL\n")
    assert any(f.name == "TestAdd" for f in fails)


def test_build_argv_new_frameworks():
    assert _build_argv("cargo", "", False) == ["cargo", "test"]
    assert _build_argv("maven", "pom.xml", False) == \
        ["mvn", "-f", "pom.xml", "-q", "test"]
    assert _build_argv("ctest", "math", False) == \
        ["ctest", "--output-on-failure", "-R", "math"]
    assert _build_argv("gradle", "", False, "somewhere") == \
        ["gradle", "test"]


# ---------------------------------------------------------------- dependency_manager
def test_dependency_report(ws_tmp):
    ws = ws_tmp / "repo"
    ws.mkdir(parents=True)
    (ws / "go.mod").write_text(
        "module x\n\nrequire github.com/foo/bar v1.2.3\n", encoding="utf-8")
    (ws / "Cargo.toml").write_text(
        "[dependencies]\nserde = \"1.0\"\n", encoding="utf-8")
    (ws / "requirements.txt").write_text(
        "requests==2.31.0\n", encoding="utf-8")
    r = dependency_report(str(ws))
    assert r["total_deps"] == 3
    assert any("go.mod" in m["path"] for m in r["manifests"])


def test_parse_manifests(ws_tmp):
    p = ws_tmp / "pom.xml"
    p.write_text(
        "<project><dependencies><dependency>"
        "<groupId>g</groupId><artifactId>a</artifactId>"
        "<version>1.0</version></dependency></dependencies></project>",
        encoding="utf-8")
    m = parse_manifest(str(p))
    assert m.manager == "maven"
    assert m.entries and m.entries[0].name == "g:a"
    pkg = ws_tmp / "package.json"
    pkg.write_text('{"dependencies": {"react": "^18"}}', encoding="utf-8")
    m2 = parse_manifest(str(pkg))
    assert m2.entries[0].name == "react"


# ---------------------------------------------------------------- tools
@pytest.mark.asyncio
async def test_database_tool_sqlite(ws_tmp):
    db = ws_tmp / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute("create table t(id int, name text)")
    conn.executemany("insert into t values (?, ?)",
                     [(1, "a"), (2, "b")])
    conn.commit()
    conn.close()
    tool = DatabaseTool()
    ctx = ExecutionContext(workspace=str(ws_tmp))
    r = await tool.execute({"engine": "sqlite", "path": "app.db",
                            "query": "select * from t"}, ctx)
    assert r.success and "a" in r.output
    # 写操作：默认 allow_write=False 一律拒绝
    r2 = await tool.execute({"engine": "sqlite", "path": "app.db",
                             "query": "delete from t",
                             "read_only": False, "confirm": True}, ctx)
    assert not r2.success


@pytest.mark.asyncio
async def test_dependency_tool(ws_tmp):
    (ws_tmp / "requirements.txt").write_text("requests==2.31.0\n",
                                             encoding="utf-8")
    tool = DependencyTool()
    ctx = ExecutionContext(workspace=str(ws_tmp))
    r = await tool.execute({"action": "report"}, ctx)
    assert r.success and "requests" in r.output
    r2 = await tool.execute({"action": "audit"}, ctx)
    assert r2.success and "pip-audit" in r2.output


@pytest.mark.asyncio
async def test_cloud_tool_safety(ws_tmp):
    tool = CloudTool()
    ctx = ExecutionContext(workspace=str(ws_tmp))
    # 未确认拒绝
    r = await tool.execute({"tool": "kubectl", "args": ["get", "pods"],
                            "confirm": False}, ctx)
    assert not r.success
    # 危险子命令拦截
    r2 = await tool.execute({"tool": "aws", "args": ["s3", "rb",
                                                     "--force", "x"],
                             "confirm": True}, ctx)
    assert not r2.success and "拦截" in r2.error
