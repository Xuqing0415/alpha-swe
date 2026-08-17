"""收敛期 P0-2：真实任务基准集（阶段二 2.1）。

首批 28 个真实软件任务，按难度 L1-L4 分级，每个任务带「可自动判断的
完成标准」：
- verify 谓词在项目目录上执行（python -c 断言 / pytest / node --check /
  静态模式匹配），不依赖人工判断；
- 每个用例同时给出「规范解法 golden」：
  * 反例 harness：未修复的项目不得通过完成标准（判定有区分度）；
  * 正例 harness：规范解法写入后必须通过完成标准（标准可实现）；
- 脚本化端到端：用 ScriptedLLM 驱动 AgentLoop 执行规范解法，验证
  Agent -> 工具调用 -> verify 的全链路可闭环（为接入真实 LLM 预留）。

运行：python -X utf8 -m pytest tests/test_benchmark_suite.py -q
"""
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM


# ---- 可自动判定的完成标准执行器 ----
def write_files(root: Path, files: Dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def run_py(root: Path, code: str, timeout: int = 30) -> bool:
    """在项目目录执行 python -c，返回是否成功（断言/输出匹配）。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def run_pytest(root: Path, rel: str, timeout: int = 60) -> bool:
    """运行指定测试文件，测试全绿才算通过完成标准。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--basetemp", str(root / ".pytest_tmp"), str(root / rel)],
            cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def run_node(root: Path, code: str, timeout: int = 30) -> bool:
    """执行 node -e 断言脚本，返回是否成功。"""
    try:
        proc = subprocess.run(
            ["node", "-e", code], cwd=str(root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def node_check(root: Path, rel: str) -> bool:
    """node --check 语法检查（构建成功代理）。"""
    try:
        proc = subprocess.run(["node", "--check", str(root / rel)],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def grep(root: Path, pattern: str, *rels: str) -> bool:
    """至少一个目标文件命中正则（输出匹配类完成标准）。"""
    import re
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


@dataclass
class BenchmarkCase:
    name: str
    files: Dict[str, str]           # 初始项目文件（含缺陷/待办起点）
    task: str                       # 用户任务描述
    difficulty: int                 # 1-4（L1 改一行 ~ L4 新增模块）
    tech: str                       # 技术栈标签
    golden: Dict[str, str]          # 规范解法（写入后 verify 必须通过）
    verify: Callable[[Path], bool]  # 可自动判断的完成标准


CASES: list[BenchmarkCase] = [
    # ================= L1：改一行 =================
    BenchmarkCase(
        name="py-null-guard",
        files={"app.py": 'def get_user_name(user):\n'
                         '    return user["name"]\n'},
        task="get_user_name 在 user 为 None 或缺少 name 时崩溃，修复为返回 anonymous",
        difficulty=1, tech="python",
        golden={"app.py": 'def get_user_name(user):\n'
                          '    if not user or "name" not in user:\n'
                          '        return "anonymous"\n'
                          '    return user["name"]\n'},
        verify=lambda ws: run_py(
            ws, "from app import get_user_name; "
                "assert get_user_name(None) == 'anonymous'; "
                "assert get_user_name({}) == 'anonymous'; "
                "assert get_user_name({'name': 'x'}) == 'x'"),
    ),
    BenchmarkCase(
        name="py-key-default",
        files={"config_loader.py": 'def load_config(path):\n'
                                   '    data = _read(path)\n'
                                   '    return data["api_key"]\n'
                                   '\n'
                                   '\n'
                                   'def _read(path):\n'
                                   '    return {}\n'},
        task="load_config 在配置文件缺少 api_key 时抛 KeyError，改为返回空串",
        difficulty=1, tech="python",
        golden={"config_loader.py": 'def load_config(path):\n'
                                    '    data = _read(path)\n'
                                    '    return data.get("api_key", "")\n'
                                    '\n'
                                    '\n'
                                    'def _read(path):\n'
                                    '    return {}\n'},
        verify=lambda ws: run_py(
            ws, "from config_loader import load_config; "
                "assert load_config('/nope') == ''"),
    ),
    BenchmarkCase(
        name="js-session-guard",
        files={"server.js": "const express = require('express');\n"
                            "const app = express();\n"
                            "app.get('/home', (req, res) => {\n"
                            "  const name = req.session.user.name;\n"
                            "  res.json({ name });\n"
                            "});\n"
                            "module.exports = app;\n"},
        task="首页接口在未登录时读取 req.session.user.name 崩溃，加空值保护",
        difficulty=1, tech="javascript/express",
        golden={"server.js": "const express = require('express');\n"
                             "const app = express();\n"
                             "app.get('/home', (req, res) => {\n"
                             "  const name = req.session && req.session.user\n"
                             "    ? req.session.user.name : 'guest';\n"
                             "  res.json({ name });\n"
                             "});\n"
                             "module.exports = app;\n"},
        verify=lambda ws: (node_check(ws, "server.js")
                           and grep(ws, r"req\.session\s*&&", "server.js")),
    ),    BenchmarkCase(
        name="ts-null-safe",
        files={"service.ts": "export function getUserName(user: any): string {\n"
                             "  return user.profile.name;\n"
                             "}\n"},
        task="getUserName 在 user 或 profile 为空时崩溃，改为可选链 + 默认值",
        difficulty=1, tech="typescript",
        golden={"service.ts": "export function getUserName(user: any): string {\n"
                              "  return user?.profile?.name ?? \"guest\";\n"
                              "}\n"},
        verify=lambda ws: grep(ws, r"user\?\.profile\?\.", "service.ts"),
    ),
    BenchmarkCase(
        name="py-int-parse",
        files={"helpers.py": "def to_int(text):\n"
                             "    return int(text)\n"},
        task="to_int 遇到非数字输入会抛异常，改为失败时返回 0",
        difficulty=1, tech="python",
        golden={"helpers.py": "def to_int(text):\n"
                              "    try:\n"
                              "        return int(text)\n"
                              "    except (TypeError, ValueError):\n"
                              "        return 0\n"},
        verify=lambda ws: run_py(
            ws, "from helpers import to_int; "
                "assert to_int('42') == 42 and to_int('abc') == 0 "
                "and to_int(None) == 0"),
    ),
    # ================= L2：改一个函数 =================
    BenchmarkCase(
        name="py-loop-comprehension",
        files={"stats.py": "def square_list(items):\n"
                           "    out = []\n"
                           "    for x in items:\n"
                           "        out.append(x * x)\n"
                           "    return out\n"},
        task="把 square_list 的循环改写成列表推导式",
        difficulty=2, tech="python",
        golden={"stats.py": "def square_list(items):\n"
                            "    return [x * x for x in items]\n"},
        verify=lambda ws: (run_py(
            ws, "from stats import square_list; "
                "assert square_list([1, 2, 3]) == [1, 4, 9]")
            and grep(ws, r"\[x \* x for x in items\]", "stats.py")),
    ),
    BenchmarkCase(
        name="py-csv-quoted",
        files={"parser.py": "def parse_csv(text):\n"
                            "    return [row.split(\",\") for row in\n"
                            "            text.strip().splitlines() if row]\n"},
        task="parse_csv 无法处理带引号的字段（如 \"a,b\",c），改用标准 csv 解析",
        difficulty=2, tech="python",
        golden={"parser.py": "import csv\n"
                             "import io\n"
                             "\n"
                             "\n"
                             "def parse_csv(text):\n"
                             "    return list(csv.reader(io.StringIO(text)))\n"},
        verify=lambda ws: run_py(
            ws, "from parser import parse_csv; "
                "assert parse_csv('\"a,b\",c\\n') == [['a,b', 'c']]"),
    ),
    BenchmarkCase(
        name="py-sort-safe",
        files={"sorter.py": "def sort_by_name(users):\n"
                            "    return sorted(users, key=lambda u: u[\"name\"])\n"},
        task="sort_by_name 在用户缺少 name 字段时抛 KeyError，改用安全取值",
        difficulty=2, tech="python",
        golden={"sorter.py": "def sort_by_name(users):\n"
                             "    return sorted(users, key=lambda u: u.get(\"name\", \"\"))\n"},
        verify=lambda ws: run_py(
            ws, "from sorter import sort_by_name; "
                "r = sort_by_name([{'name': 'b', 'id': 2}, {'id': 1}]); "
                "assert r[0]['id'] == 1"),
    ),    BenchmarkCase(
        name="js-dedupe-by-id",
        files={"util.js": "function dedupe(items) {\n"
                          "  return [...new Set(items)];\n"
                          "}\n"
                          "module.exports = { dedupe };\n"},
        task="dedupe 对对象数组去重失效（引用不同），改为按 id 字段去重",
        difficulty=2, tech="javascript",
        golden={"util.js": "function dedupe(items) {\n"
                           "  const seen = new Set();\n"
                           "  return items.filter((it) => {\n"
                           "    if (seen.has(it.id)) return false;\n"
                           "    seen.add(it.id);\n"
                           "    return true;\n"
                           "  });\n"
                           "}\n"
                           "module.exports = { dedupe };\n"},
        verify=lambda ws: run_node(
            ws, "const { dedupe } = require('./util.js'); "
                "const r = dedupe([{id:1},{id:1},{id:2}]); "
                "if (r.length !== 2) process.exit(1);"),
    ),
    BenchmarkCase(
        name="py-with-open",
        files={"reader.py": "def read_lines(path):\n"
                            "    f = open(path, encoding=\"utf-8\")\n"
                            "    return f.readlines()\n",
               "x.txt": "a\n"},
        task="read_lines 打开文件后未关闭，改用 with 语句管理资源",
        difficulty=2, tech="python",
        golden={"reader.py": "def read_lines(path):\n"
                             "    with open(path, encoding=\"utf-8\") as f:\n"
                             "        return f.readlines()\n"},
        verify=lambda ws: (run_py(
            ws, "from reader import read_lines; "
                "assert read_lines('x.txt') == ['a\\n']")
            and grep(ws, r"with open\(", "reader.py")),
    ),
    # ================= L3：跨文件 / 新增测试 / 新增端点 =================
    BenchmarkCase(
        name="py-pytest-add",
        files={"src/utils.py": "def parse_csv(text):\n"
                               "    return [row.split(\",\") for row in\n"
                               "            text.strip().splitlines() if row]\n"},
        task="为 src/utils.py 的 parse_csv 补单元测试，覆盖正常行与空行",
        difficulty=3, tech="python/pytest",
        golden={"tests/test_utils.py": "from src.utils import parse_csv\n"
                                       "\n"
                                       "\n"
                                       "def test_normal_rows():\n"
                                       "    assert parse_csv(\"a,b\\nc,d\") == [[\"a\", \"b\"], [\"c\", \"d\"]]\n"
                                       "\n"
                                       "\n"
                                       "def test_empty_lines_skipped():\n"
                                       "    assert parse_csv(\"\\n\\n\") == []\n"
                                       "\n"
                                       "\n"
                                       "def test_single_column():\n"
                                       "    assert parse_csv(\"x\") == [[\"x\"]]\n"},
        verify=lambda ws: run_pytest(ws, "tests/test_utils.py"),
    ),
    BenchmarkCase(
        name="express-orders-endpoint",
        files={"package.json": '{"name": "order-svc", "private": true}\n',
               "src/app.js": "const express = require('express');\n"
                             "const app = express();\n"
                             "module.exports = app;\n"},
        task="为订单服务添加 GET /api/orders 端点，返回订单列表",
        difficulty=3, tech="javascript/express",
        golden={"src/app.js": "const express = require('express');\n"
                              "const app = express();\n"
                              "const orders = [];\n"
                              "app.get('/api/orders', (req, res) => res.json(orders));\n"
                              "module.exports = app;\n"},
        verify=lambda ws: (node_check(ws, "src/app.js")
                           and grep(ws, r"app\.get\(", "src/app.js")
                           and grep(ws, r"/api/orders", "src/app.js")),
    ),    BenchmarkCase(
        name="sqlalchemy-email-column",
        files={"app/models.py": "from sqlalchemy import Column, Integer, String\n"
                                "from sqlalchemy.orm import declarative_base\n"
                                "\n"
                                "Base = declarative_base()\n"
                                "\n"
                                "\n"
                                "class User(Base):\n"
                                "    __tablename__ = \"users\"\n"
                                "    id = Column(Integer, primary_key=True)\n"
                                "    name = Column(String(50))\n"},
        task="给 User 模型加一个 email 字符串字段并生成迁移脚本",
        difficulty=3, tech="python/sqlalchemy",
        golden={"app/models.py": "from sqlalchemy import Column, Integer, String\n"
                                 "from sqlalchemy.orm import declarative_base\n"
                                 "\n"
                                 "Base = declarative_base()\n"
                                 "\n"
                                 "\n"
                                 "class User(Base):\n"
                                 "    __tablename__ = \"users\"\n"
                                 "    id = Column(Integer, primary_key=True)\n"
                                 "    name = Column(String(50))\n"
                                 "    email = Column(String(120))\n"},
        verify=lambda ws: grep(ws, r"email\s*=\s*Column\(", "app/models.py"),
    ),
    BenchmarkCase(
        name="flask-login-safe",
        files={"app.py": "from flask import Flask, request, jsonify\n"
                         "app = Flask(__name__)\n"
                         "\n"
                         "\n"
                         "@app.route(\"/login\", methods=[\"POST\"])\n"
                         "def login():\n"
                         "    data = request.get_json()\n"
                         "    user = data[\"username\"]\n"
                         "    return jsonify(ok=True, user=user)\n"},
        task="登录接口缺少 username 时 500 崩溃，改为安全取值并返回空用户名",
        difficulty=3, tech="python/flask",
        golden={"app.py": "from flask import Flask, request, jsonify\n"
                          "app = Flask(__name__)\n"
                          "\n"
                          "\n"
                          "@app.route(\"/login\", methods=[\"POST\"])\n"
                          "def login():\n"
                          "    data = request.get_json() or {}\n"
                          "    user = data.get(\"username\", \"\")\n"
                          "    return jsonify(ok=True, user=user)\n"},
        verify=lambda ws: (grep(ws, r"data\.get\(['\"]username", "app.py")
                           and grep(ws, r"get_json\(\) or \{\}", "app.py")),
    ),
    BenchmarkCase(
        name="py-logging-migration",
        files={"app.py": "def run():\n"
                         "    print(\"start\")\n"
                         "    value = 42\n"
                         "    print(f\"value={value}\")\n"
                         "    print(\"done\")\n"},
        task="把 run() 里的 print 全部迁移到 logging 模块（保留行为）",
        difficulty=3, tech="python",
        golden={"app.py": "import logging\n"
                          "\n"
                          "logger = logging.getLogger(\"app\")\n"
                          "\n"
                          "\n"
                          "def run():\n"
                          "    logger.info(\"start\")\n"
                          "    value = 42\n"
                          "    logger.info(\"value=%s\", value)\n"
                          "    logger.info(\"done\")\n"},
        verify=lambda ws: (no_grep(ws, r"print\(", "app.py")
                           and grep(ws, r"logging\.", "app.py")
                           and run_py(ws, "import app; app.run()")),
    ),    # ================= L4：新增模块 / 完整实现 =================
    BenchmarkCase(
        name="py-rate-limiter",
        files={"ratelimit.py": "class RateLimiter:\n"
                               "    \"\"\"每窗口最多允许 max_calls 次调用。\"\"\"\n"
                               "\n"
                               "    def __init__(self, max_calls, window_seconds):\n"
                               "        raise NotImplementedError\n"
                               "\n"
                               "    def allow(self, now):\n"
                               "        raise NotImplementedError\n",
               "tests/test_ratelimit.py": "from ratelimit import RateLimiter\n"
                                          "\n"
                                          "\n"
                                          "def test_allows_within_window():\n"
                                          "    rl = RateLimiter(2, 10)\n"
                                          "    assert rl.allow(0) is True\n"
                                          "    assert rl.allow(1) is True\n"
                                          "    assert rl.allow(2) is False\n"
                                          "\n"
                                          "\n"
                                          "def test_window_resets():\n"
                                          "    rl = RateLimiter(2, 10)\n"
                                          "    assert rl.allow(0) is True\n"
                                          "    assert rl.allow(1) is True\n"
                                          "    assert rl.allow(2) is False\n"
                                          "    assert rl.allow(11) is True\n"},
        task="实现 RateLimiter 滑动窗口限流类并通过已有测试",
        difficulty=4, tech="python",
        golden={"ratelimit.py": "class RateLimiter:\n"
                                "    def __init__(self, max_calls, window_seconds):\n"
                                "        self.max_calls = max_calls\n"
                                "        self.window_seconds = window_seconds\n"
                                "        self._calls = []\n"
                                "\n"
                                "    def allow(self, now):\n"
                                "        self._calls = [t for t in self._calls\n"
                                "                       if now - t < self.window_seconds]\n"
                                "        if len(self._calls) >= self.max_calls:\n"
                                "            return False\n"
                                "        self._calls.append(now)\n"
                                "        return True\n"},
        verify=lambda ws: run_pytest(ws, "tests/test_ratelimit.py"),
    ),
    BenchmarkCase(
        name="py-json-store",
        files={"store.py": "class JsonStore:\n"
                           "    \"\"\"把数据持久化到 JSON 文件：get/set/delete。\"\"\"\n"
                           "\n"
                           "    def __init__(self, path):\n"
                           "        raise NotImplementedError\n"
                           "\n"
                           "    def get(self, key, default=None):\n"
                           "        raise NotImplementedError\n"
                           "\n"
                           "    def set(self, key, value):\n"
                           "        raise NotImplementedError\n"
                           "\n"
                           "    def delete(self, key):\n"
                           "        raise NotImplementedError\n",
               "tests/test_store.py": "import os\n"
                                      "\n"
                                      "\n"
                                        "def _clean(path):\n"
                                        "    try:\n"
                                        "        if os.path.exists(path):\n"
                                        "            os.remove(path)\n"
                                        "    except OSError:\n"
                                        "        pass\n"
                                      "\n"
                                      "\n"
                                      "def test_set_get_roundtrip():\n"
                                      "    from store import JsonStore\n"
                                      "    path = \"db_roundtrip.json\"\n"
                                      "    _clean(path)\n"
                                      "    try:\n"
                                      "        s = JsonStore(path)\n"
                                      "        s.set(\"k\", 1)\n"
                                      "        assert s.get(\"k\") == 1\n"
                                      "    finally:\n"
                                      "        _clean(path)\n"
                                      "\n"
                                      "\n"
                                      "def test_persist_across_instances():\n"
                                      "    from store import JsonStore\n"
                                      "    path = \"db_persist.json\"\n"
                                      "    _clean(path)\n"
                                      "    try:\n"
                                      "        JsonStore(path).set(\"k\", [1, 2])\n"
                                      "        assert JsonStore(path).get(\"k\") == [1, 2]\n"
                                      "    finally:\n"
                                      "        _clean(path)\n"
                                      "\n"
                                      "\n"
                                      "def test_delete():\n"
                                      "    from store import JsonStore\n"
                                      "    path = \"db_delete.json\"\n"
                                      "    _clean(path)\n"
                                      "    try:\n"
                                      "        s = JsonStore(path)\n"
                                      "        s.set(\"k\", 1)\n"
                                      "        s.delete(\"k\")\n"
                                      "        assert s.get(\"k\") is None\n"
                                      "    finally:\n"
                                      "        _clean(path)\n"},
        task="实现 JSON 文件持久化键值存储 JsonStore 并通过已有测试",
        difficulty=4, tech="python",
        golden={"store.py": "import json\n"
                            "\n"
                            "\n"
                            "class JsonStore:\n"
                            "    def __init__(self, path):\n"
                            "        self.path = path\n"
                            "\n"
                            "    def _load(self):\n"
                            "        try:\n"
                            "            with open(self.path, encoding=\"utf-8\") as f:\n"
                            "                return json.load(f)\n"
                            "        except (OSError, ValueError):\n"
                            "            return {}\n"
                            "\n"
                            "    def _save(self, data):\n"
                            "        with open(self.path, \"w\", encoding=\"utf-8\") as f:\n"
                            "            json.dump(data, f, ensure_ascii=False)\n"
                            "\n"
                            "    def get(self, key, default=None):\n"
                            "        return self._load().get(key, default)\n"
                            "\n"
                            "    def set(self, key, value):\n"
                            "        data = self._load()\n"
                            "        data[key] = value\n"
                            "        self._save(data)\n"
                            "\n"
                            "    def delete(self, key):\n"
                            "        data = self._load()\n"
                            "        data.pop(key, None)\n"
                            "        self._save(data)\n"},
        verify=lambda ws: run_pytest(ws, "tests/test_store.py"),
    ),
    BenchmarkCase(
        name="py-js-fib",
        files={"fib.js": 'function fib(n) {\n'
                         '    return 0; // TODO\n'
                         '}\n'
                         'module.exports = { fib };\n',
               "tests/test_fib.js": "const assert = require('assert');\n"
                                    "const { fib } = require('../fib.js');\n"
                                    "assert.strictEqual(fib(0), 0);\n"
                                    "assert.strictEqual(fib(1), 1);\n"
                                    "assert.strictEqual(fib(10), 55);\n"
                                    "assert.strictEqual(fib(20), 6765);\n"},
        task="实现 fib 模块（斐波那契数列，fib(0)=0, fib(1)=1）并通过 tests/test_fib.js 断言",
        difficulty=4, tech="javascript",
        golden={"fib.js": "function fib(n) {\n"
                          "  if (n < 0) throw new Error('negative');\n"
                          "  let a = 0, b = 1;\n"
                          "  for (let i = 0; i < n; i++) {\n"
                          "    [a, b] = [b, a + b];\n"
                          "  }\n"
                          "  return a;\n"
                          "}\n"
                          "module.exports = { fib };\n"},
        verify=lambda ws: run_node(ws, "require('./tests/test_fib.js')"),
    ),
    BenchmarkCase(
        name="py-cli-entry",
        files={"cli.py": "def main(argv=None):\n"
                         "    \"\"\"解析参数并打印问候语。\"\"\"\n"
                         "    raise NotImplementedError\n"},
        task="实现 cli.main：--name 指定名字、--upper 大写输出，无参数打印 Hello, world!",
        difficulty=4, tech="python",
        golden={"cli.py": "import argparse\n"
                          "\n"
                          "\n"
                          "def main(argv=None):\n"
                          "    parser = argparse.ArgumentParser()\n"
                          "    parser.add_argument(\"--name\", default=\"world\")\n"
                          "    parser.add_argument(\"--upper\", action=\"store_true\")\n"
                          "    args = parser.parse_args(argv)\n"
                          "    text = f\"Hello, {args.name}!\"\n"
                          "    if args.upper:\n"
                          "        text = text.upper()\n"
                          "    print(text)\n"},
        verify=lambda ws: run_py(
            ws, "import io, contextlib\n"
                "from cli import main\n"
                "buf = io.StringIO()\n"
                "with contextlib.redirect_stdout(buf):\n"
                "    main([])\n"
                "assert buf.getvalue() == 'Hello, world!\\n'\n"
                "buf = io.StringIO()\n"
                "with contextlib.redirect_stdout(buf):\n"
                "    main(['--name', 'codex', '--upper'])\n"
                "assert buf.getvalue() == 'HELLO, CODEX!\\n'"),
    ),
    BenchmarkCase(
        name="py-email-validator",
        files={"validator.py": "def is_valid_email(email):\n"
                               "    \"\"\"返回邮箱格式是否合法。\"\"\"\n"
                               "    raise NotImplementedError\n",
               "tests/test_validator.py": "from validator import is_valid_email\n"
                                          "\n"
                                          "\n"
                                          "def test_valid():\n"
                                          "    assert is_valid_email(\"a@b.com\")\n"
                                          "    assert is_valid_email(\"user.name+tag@example.co.uk\")\n"
                                          "\n"
                                          "\n"
                                          "def test_invalid():\n"
                                          "    assert not is_valid_email(\"\")\n"
                                          "    assert not is_valid_email(\"plain\")\n"
                                          "    assert not is_valid_email(\"a@b\")\n"
                                          "    assert not is_valid_email(\"a b@c.com\")\n"
                                          "    assert not is_valid_email(\"@b.com\")\n"},
        task="实现 is_valid_email 邮箱格式校验函数并通过已有测试",
        difficulty=4, tech="python",
        golden={"validator.py": "import re\n"
                                "\n"
                                "_EMAIL_RE = re.compile(\n"
                                "    r\"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$\"\n"
                                ")\n"
                                "\n"
                                "\n"
                                "def is_valid_email(email):\n"
                                "    return bool(_EMAIL_RE.fullmatch(email or \"\"))\n"},
        verify=lambda ws: run_pytest(ws, "tests/test_validator.py"),
    ),


    # ================= L1 扩展：改一行 =================
    BenchmarkCase(
        name="py-safe-divide",
        files={"math_utils.py": "def divide(a, b):\n"
                                "    return a / b\n"},
        task="divide 在除数为 0 或参数为空时崩溃，改为返回 0",
        difficulty=1, tech="python",
        golden={"math_utils.py": "def divide(a, b):\n"
                                  "    if not b:\n"
                                  "        return 0\n"
                                  "    return a / b\n"},
        verify=lambda ws: run_py(
            ws, "from math_utils import divide; "
                "assert divide(10, 0) == 0; "
                "assert divide(10, None) == 0; "
                "assert divide(10, 2) == 5.0"),
    ),
    BenchmarkCase(
        name="js-array-first",
        files={"utils.js": "function first(arr) {\n"
                           "  return arr[0];\n"
                           "}\n"
                           "module.exports = { first };\n"},
        task="first 在数组为空或传入 null 时崩溃，改为返回 null",
        difficulty=1, tech="javascript",
        golden={"utils.js": "function first(arr) {\n"
                            "  return arr && arr.length ? arr[0] : null;\n"
                            "}\n"
                            "module.exports = { first };\n"},
        verify=lambda ws: (node_check(ws, "utils.js")
                           and grep(ws, r"arr\s*&&", "utils.js")),
    ),
    # ================= L2 扩展：改一个函数 =================
    BenchmarkCase(
        name="py-rename-symbol",
        files={"data_service.py": "def fetch_data(source):\n"
                                  "    return {\"source\": source, \"rows\": []}\n",
               "main.py": "from data_service import fetch_data\n"
                          "\n"
                          "\n"
                          "def run():\n"
                          "    return fetch_data(\"db\")\n"},
        task="把 fetch_data 重命名为 load_data，并同步更新所有调用方",
        difficulty=2, tech="python",
        golden={"data_service.py": "def load_data(source):\n"
                                   "    return {\"source\": source, \"rows\": []}\n",
                "main.py": "from data_service import load_data\n"
                           "\n"
                           "\n"
                           "def run():\n"
                           "    return load_data(\"db\")\n"},
        verify=lambda ws: run_py(
            ws, "from main import run; "
                "assert run() == {'source': 'db', 'rows': []}; "
                "import data_service; "
                "assert not hasattr(data_service, 'fetch_data')"),
    ),
    BenchmarkCase(
        name="py-thread-safe-counter",
        files={"counter.py": "import threading\n"
                             "\n"
                             "\n"
                             "class Counter:\n"
                             "    def __init__(self):\n"
                             "        self.value = 0\n"
                             "\n"
                             "    def increment(self):\n"
                             "        self.value += 1\n"},
        task="Counter.increment 在多线程下会丢失更新，加锁保证线程安全",
        difficulty=2, tech="python/threading",
        golden={"counter.py": "import threading\n"
                              "\n"
                              "\n"
                              "class Counter:\n"
                              "    def __init__(self):\n"
                              "        self.value = 0\n"
                              "        self._lock = threading.Lock()\n"
                              "\n"
                              "    def increment(self):\n"
                              "        with self._lock:\n"
                              "            self.value += 1\n"},
        verify=lambda ws: (grep(ws, r"threading\.Lock", "counter.py")
                           and grep(ws, r"with\s+self\._lock",
                                    "counter.py")
                           and run_py(
                               ws, "from counter import Counter\n"
                                   "import threading\n"
                                   "c = Counter()\n"
                                   "def work():\n"
                                   "    for _ in range(200):\n"
                                   "        c.increment()\n"
                                   "ts = [threading.Thread(target=work) "
                                   "for _ in range(8)]\n"
                                   "for t in ts: t.start()\n"
                                   "for t in ts: t.join()\n"
                                   "assert c.value == 1600\n")),
    ),

    # ================= L3 扩展：跨文件修改 =================
    BenchmarkCase(
        name="sql-injection-safe",
        files={"db.py": "import sqlite3\n"
                        "\n"
                        "\n"
                        "def find_user(conn, username):\n"
                        "    cur = conn.execute(\n"
                        "        f\"SELECT * FROM users WHERE username = '{username}'\")\n"
                        "    return cur.fetchone()\n"},
        task="find_user 用字符串拼接 SQL，存在注入风险，改为参数化查询",
        difficulty=3, tech="python/sqlite",
        golden={"db.py": "import sqlite3\n"
                         "\n"
                         "\n"
                         "def find_user(conn, username):\n"
                         "    cur = conn.execute(\n"
                         "        \"SELECT * FROM users WHERE username = ?\", "
                         "(username,))\n"
                         "    return cur.fetchone()\n"},
        verify=lambda ws: run_py(
            ws, "from db import find_user; "
                "import sqlite3; "
                "conn = sqlite3.connect(':memory:'); "
                "conn.execute('CREATE TABLE users (username TEXT)'); "
                "conn.execute(\"INSERT INTO users VALUES ('admin')\"); "
                "assert find_user(conn, 'admin') is not None; "
                "assert find_user(conn, \"' OR 1=1 --\") is None"),
    ),
    BenchmarkCase(
        name="py-retry-decorator",
        files={"retry.py": "def retry(max_attempts):\n"
                           "    def deco(fn):\n"
                           "        return fn\n"
                           "    return deco\n"},
        task="实现 retry 装饰器：函数抛异常时重试，最多 max_attempts 次",
        difficulty=3, tech="python",
        golden={"retry.py": "import time\n"
                            "\n"
                            "\n"
                            "def retry(max_attempts):\n"
                            "    def deco(fn):\n"
                            "        def wrapper(*args, **kwargs):\n"
                            "            last = None\n"
                            "            for _ in range(max_attempts):\n"
                            "                try:\n"
                            "                    return fn(*args, **kwargs)\n"
                            "                except Exception as e:\n"
                            "                    last = e\n"
                            "                    time.sleep(0)\n"
                            "            raise last\n"
                            "        return wrapper\n"
                            "    return deco\n"},
        verify=lambda ws: run_py(
            ws, "from retry import retry\n"
                "calls = {'n': 0}\n"
                "@retry(max_attempts=3)\n"
                "def flaky():\n"
                "    calls['n'] += 1\n"
                "    if calls['n'] < 3:\n"
                "        raise ValueError('boom')\n"
                "    return 'ok'\n"
                "assert flaky() == 'ok' and calls['n'] == 3\n"
                "@retry(max_attempts=2)\n"
                "def always_fail():\n"
                "    raise RuntimeError('x')\n"
                "try:\n"
                "    always_fail()\n"
                "    raise SystemExit('should have raised')\n"
                "except RuntimeError:\n"
                "    pass\n"),
    ),
    # ================= L4 扩展：新增模块 =================
    BenchmarkCase(
        name="py-ttl-cache",
        files={"cache.py": "class TTLCache:\n"
                           "    def __init__(self, ttl_seconds):\n"
                           "        self.ttl = ttl_seconds\n"
                           "        self._data = {}\n"
                           "\n"
                           "    def get(self, key):\n"
                           "        return self._data.get(key)\n"
                           "\n"
                           "    def set(self, key, value):\n"
                           "        self._data[key] = value\n",
               "tests/test_cache.py": "import time\n"
                                      "from cache import TTLCache\n"
                                      "\n"
                                      "\n"
                                      "def test_cache_get_set():\n"
                                      "    c = TTLCache(ttl_seconds=1)\n"
                                      "    c.set(\"a\", 1)\n"
                                      "    assert c.get(\"a\") == 1\n"
                                      "\n"
                                      "\n"
                                      "def test_cache_expires():\n"
                                      "    c = TTLCache(ttl_seconds=0.05)\n"
                                      "    c.set(\"a\", 1)\n"
                                      "    time.sleep(0.1)\n"
                                      "    assert c.get(\"a\") is None\n"},
        task="实现 TTLCache：get/set 带过期时间，过期后 get 返回 None，并通过已有测试",
        difficulty=4, tech="python",
        golden={"cache.py": "import time\n"
                            "\n"
                            "\n"
                            "class TTLCache:\n"
                            "    def __init__(self, ttl_seconds):\n"
                            "        self.ttl = ttl_seconds\n"
                            "        self._data = {}\n"
                            "        self._expires = {}\n"
                            "\n"
                            "    def get(self, key):\n"
                            "        exp = self._expires.get(key)\n"
                            "        if exp is None:\n"
                            "            return None\n"
                            "        if time.monotonic() > exp:\n"
                            "            self._data.pop(key, None)\n"
                            "            self._expires.pop(key, None)\n"
                            "            return None\n"
                            "        return self._data[key]\n"
                            "\n"
                            "    def set(self, key, value):\n"
                            "        self._data[key] = value\n"
                            "        self._expires[key] = time.monotonic() + self.ttl\n"},
        verify=lambda ws: run_pytest(ws, "tests/test_cache.py"),
    ),
    BenchmarkCase(
        name="ts-api-pagination",
        files={"src/orders.ts": "export interface Order { id: number; }\n"
                                "export function listOrders(orders: Order[]): Order[] {\n"
                                "  return orders;\n"
                                "}\n"},
        task="为 listOrders 增加分页参数 page 与 pageSize，返回对应切片",
        difficulty=4, tech="typescript",
        golden={"src/orders.ts": "export interface Order { id: number; }\n"
                                 "export function listOrders(orders: Order[], "
                                 "page = 1, pageSize = 10): Order[] {\n"
                                 "  const start = (page - 1) * pageSize;\n"
                                 "  return orders.slice(start, start + pageSize);\n"
                                 "}\n"},
        verify=lambda ws: (grep(ws, r"\.slice\(", "src/orders.ts")
                           and grep(ws, r"pageSize", "src/orders.ts")),
    ),
]

# ---- 断言 1/2：区分度（反例不通过 + 正例通过） ----
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_baseline_does_not_pass(ws_tmp, case):
    """反例 harness：未修复的项目不得通过完成标准。"""
    write_files(ws_tmp, case.files)
    assert not case.verify(ws_tmp), f"{case.name} 基线不应通过 verify"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_golden_passes(ws_tmp, case):
    """正例 harness：规范解法写入后必须通过完成标准。"""
    write_files(ws_tmp, case.files)
    write_files(ws_tmp, case.golden)
    assert case.verify(ws_tmp), f"{case.name} 规范解法应通过 verify"


# ---- 断言 3：分级统计 ----
def test_difficulty_distribution():
    """分级统计：收敛期扩展至 28 个场景，L1-L4 各 7 个，覆盖 4 类技术栈。"""
    from collections import Counter
    counts = Counter(c.difficulty for c in CASES)
    assert len(CASES) == 28
    assert counts[1] == 7 and counts[2] == 7
    assert counts[3] == 7 and counts[4] == 7
    assert len({c.tech for c in CASES}) >= 3  # 至少 3 种技术栈标签


# ---- 断言 4：脚本化端到端（Agent -> 工具调用 -> verify 闭环） ----
class ScriptedLLM(MockLLM):
    """按脚本依次返回响应，记录调用历史。"""

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


def _make_loop_config(ws: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=10, max_retries=2, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws)),
        memory=MemoryConfig(db_path=str(ws / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


E2E_CASES = [c for c in CASES if c.name in
             {"py-null-guard", "py-int-parse", "py-email-validator"}]


@pytest.mark.parametrize("case", E2E_CASES, ids=lambda c: c.name)
@pytest.mark.asyncio
async def test_scripted_llm_end_to_end(ws_tmp, case):
    """ScriptedLLM 驱动 AgentLoop：思考 -> file_ops 写入规范解法 -> verify。"""
    write_files(ws_tmp, case.files)
    single_file = dict(case.golden)
    responses = ['{"think": "定位问题后直接写入规范解法"}']
    for rel, body in single_file.items():
        action = {"tool": "file_ops", "params": {
            "action": "write", "path": rel, "content": body}}
        responses.append(json.dumps(action, ensure_ascii=False))
    responses.append('{"final_answer": "已完成"}')
    # 任务完成后经验总结器还会消费一次 LLM 调用（失败则回退规则提取）
    responses.append(json.dumps({
        "problem": case.task,
        "solution": "写入规范解法并通过完成标准",
        "steps": ["定位问题", "写入规范解法"],
        "key_files": list(single_file),
        "outcome": "success",
    }, ensure_ascii=False))
    llm = ScriptedLLM(*responses)
    loop = AgentLoop(config=_make_loop_config(ws_tmp), llm=llm,
                     planner=StubPlanner())
    try:
        result = await loop.run(case.task)
        assert result.ok, f"{case.name} 端到端执行应成功"
        assert case.verify(ws_tmp), f"{case.name} 端到端后 verify 应通过"
        assert llm.calls, "应至少有一次 LLM 调用"
    finally:
        await loop.close()
