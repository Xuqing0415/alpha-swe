# -*- coding: utf-8 -*-
"""收敛期遗留：真实项目端到端验证。

在带真实技术栈气息的多文件项目（Python 包 + pytest 测试套件）上，
用 ScriptedLLM 驱动 AgentLoop 完整走一遍「理解 -> 跨模块修改 -> 补回归
测试 -> 收尾」，最终以 pytest 全绿 + 回归测试存在作为完成标准。

与 tests/test_benchmark_suite.py 的差异：
- 项目是多文件 Python 包（跨模块依赖），而非单文件用例；
- 基线测试套件在修复前确实失败（任务有真实缺陷）；
- 脚本化 Agent 需先读文件理解模块职责，再跨模块修改。

运行：python -X utf8 -m pytest tests/test_real_project_e2e.py -q
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pytest

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM

from test_benchmark_suite import run_pytest  # 复用 pytest 运行器


@dataclass
class RealProjectCase:
    name: str
    files: Dict[str, str]       # 项目初始状态（含缺陷）
    golden: Dict[str, str]      # Agent 应写出的修改/新增
    task: str
    regression_rel: str         # 必须存在的回归测试文件
    read_first: List[str] = field(default_factory=list)  # 先读的源文件


def write_files(root: Path, files: Dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


# ---- 场景一：空存储崩溃修复（跨 storage/api 两模块）+ 回归测试 ----
ORDERSVC_INIT = """from ordersvc.storage import OrderStore
from ordersvc.api import latest_order_id

__all__ = ["OrderStore", "latest_order_id"]
"""

ORDERSVC_STORAGE = """import json
from pathlib import Path


class OrderStore:
    def __init__(self, path):
        self._path = Path(path)
        self._data = self._load()

    def _load(self):
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, order):
        self._data.append(order)
        self._write()

    def _write(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False), encoding="utf-8")

    def latest(self):
        return self._data[-1]

    def find_by_status(self, status):
        return [o for o in self._data if o.get("status") == status]
"""

ORDERSVC_API = """from ordersvc.storage import OrderStore


def latest_order_id(store):
    order = store.latest()
    return order["id"]
"""

ORDERSVC_TESTS = """from ordersvc.api import latest_order_id
from ordersvc.storage import OrderStore


def test_save_and_latest(tmp_path):
    store = OrderStore(tmp_path / "orders.json")
    store.save({"id": 1, "status": "paid"})
    assert store.latest() == {"id": 1, "status": "paid"}


def test_latest_empty_store_should_not_crash(tmp_path):
    store = OrderStore(tmp_path / "orders.json")
    assert store.latest() is None
"""

ORDERSVC_STORAGE_FIXED = ORDERSVC_STORAGE.replace(
    "    def latest(self):\n        return self._data[-1]",
    "    def latest(self):\n        if not self._data:\n            return None\n"
    "        return self._data[-1]")

ORDERSVC_API_FIXED = """from ordersvc.storage import OrderStore


def latest_order_id(store):
    order = store.latest()
    if order is None:
        return None
    return order["id"]
"""

ORDERSVC_REGRESSION = """from ordersvc.storage import OrderStore


def test_find_by_status_empty_store(tmp_path):
    store = OrderStore(tmp_path / "orders.json")
    assert store.find_by_status("paid") == []
"""

# ---- 场景二：折扣计算跨模块错误（pricing x checkout）+ 边界回归 ----
CHECKOUT_PRICING = """def subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def apply_discount(amount, discount_pct):
    # BUG: 用乘法代替了保留比例，折扣越大金额反而越高
    return amount * discount_pct


def compute_total(items, discount_pct=0.0):
    return apply_discount(subtotal(items), discount_pct)
"""

CHECKOUT_MAIN = """from checkout.pricing import compute_total


def summarize(items, discount_pct=0.0):
    total = compute_total(items, discount_pct)
    return {"items": len(items), "total": round(total, 2)}
"""

CHECKOUT_TESTS = """from checkout.main import summarize
from checkout.pricing import compute_total


def test_no_discount():
    items = [{"price": 10.0, "qty": 2}]
    assert compute_total(items) == 20.0


def test_ten_percent_discount():
    items = [{"price": 10.0, "qty": 2}]
    assert compute_total(items, 0.1) == 18.0


def test_summarize():
    items = [{"price": 5.0, "qty": 3}]
    assert summarize(items, 0.1) == {"items": 1, "total": 13.5}
"""

CHECKOUT_PRICING_FIXED = CHECKOUT_PRICING.replace(
    "    return amount * discount_pct",
    "    return amount * (1 - discount_pct)")

CHECKOUT_REGRESSION = """from checkout.pricing import apply_discount


def test_zero_discount_keeps_full():
    assert apply_discount(100.0, 0.0) == 100.0


def test_full_discount_zero():
    assert apply_discount(100.0, 1.0) == 0.0
"""


REAL_PROJECT_CASES = [
    RealProjectCase(
        name="orders-empty-store-fix",
        files={
            "ordersvc/__init__.py": ORDERSVC_INIT,
            "ordersvc/storage.py": ORDERSVC_STORAGE,
            "ordersvc/api.py": ORDERSVC_API,
            "tests/test_orders.py": ORDERSVC_TESTS,
        },
        golden={
            "ordersvc/storage.py": ORDERSVC_STORAGE_FIXED,
            "ordersvc/api.py": ORDERSVC_API_FIXED,
            "tests/test_regression.py": ORDERSVC_REGRESSION,
        },
        task=("ordersvc 在空存储时崩溃：OrderStore.latest() 抛出 IndexError，"
              "latest_order_id 也随之失败。修复空存储处理并补充 "
              "tests/test_regression.py 回归测试"),
        regression_rel="tests/test_regression.py",
        read_first=["ordersvc/storage.py", "ordersvc/api.py"],
    ),
    RealProjectCase(
        name="checkout-discount-fix",
        files={
            "checkout/__init__.py": "",
            "checkout/pricing.py": CHECKOUT_PRICING,
            "checkout/main.py": CHECKOUT_MAIN,
            "tests/test_checkout.py": CHECKOUT_TESTS,
        },
        golden={
            "checkout/pricing.py": CHECKOUT_PRICING_FIXED,
            "tests/test_regression.py": CHECKOUT_REGRESSION,
        },
        task=("checkout 包折扣计算错误：apply_discount 把折扣当成保留比例"
              "（20 元打 9 折算出 2 元）。修复 pricing.apply_discount 并"
              "补充 tests/test_regression.py 边界回归测试"),
        regression_rel="tests/test_regression.py",
        read_first=["checkout/pricing.py"],
    ),
]


def _make_loop_config(ws: Path) -> AppConfig:
    return AppConfig(
        agent=AgentConfig(max_rounds=12, max_retries=2, max_concurrency=1,
                      default_token_budget=60000, default_time_budget=300.0),
        sandbox=SandboxConfig(workspace=str(ws)),
        memory=MemoryConfig(db_path=str(ws / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class ScriptedLLM(MockLLM):
    """按脚本依次返回响应；经验总结器返回空对象，避免额外脚本。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        assert self._responses, "LLM 调用次数超出脚本"
        return self._responses.pop(0)


class StubPlanner:
    async def plan(self, prompt, context=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


def _think(text):
    return json.dumps({"think": text}, ensure_ascii=False)


def _read(rel: str) -> str:
    return json.dumps({"tool": "file_ops", "params": {
        "action": "read", "path": rel}}, ensure_ascii=False)


def _write(rel: str, body: str) -> str:
    return json.dumps({"tool": "file_ops", "params": {
        "action": "write", "path": rel, "content": body}}, ensure_ascii=False)


def _final(text):
    return json.dumps({"final_answer": text}, ensure_ascii=False)


def _script_for(case: RealProjectCase) -> List[str]:
    responses = [_think("定位问题后先读相关模块，再写入修复与回归测试")]
    for rel in case.read_first:
        responses.append(_read(rel))
    for rel, body in case.golden.items():
        responses.append(_write(rel, body))
    responses.append(_final("已完成修复并补充回归测试"))
    return responses


@pytest.mark.parametrize("case", REAL_PROJECT_CASES,
                         ids=lambda c: c.name)
def test_real_project_baseline_fails(ws_tmp, case):
    """基线验证：修复前的测试套件确实失败（任务有真实缺陷）。"""
    write_files(ws_tmp, case.files)
    assert not run_pytest(ws_tmp, "tests"), (
        f"{case.name} 基线测试应失败（缺陷真实存在）")


@pytest.mark.parametrize("case", REAL_PROJECT_CASES,
                         ids=lambda c: c.name)
@pytest.mark.asyncio
async def test_real_project_end_to_end(ws_tmp, case):
    """ScriptedLLM 驱动 AgentLoop 完成跨模块修复 + 补回归测试。"""
    write_files(ws_tmp, case.files)
    responses = _script_for(case)
    responses.append(json.dumps({
        "problem": case.task,
        "solution": "修复缺陷并补充回归测试",
        "steps": ["读模块", "写入修复", "补回归测试"],
        "key_files": list(case.golden),
        "outcome": "success",
    }, ensure_ascii=False))
    llm = ScriptedLLM(*responses)
    loop = AgentLoop(config=_make_loop_config(ws_tmp), llm=llm,
                     planner=StubPlanner())
    try:
        result = await loop.run(case.task)
        assert result.ok, f"{case.name} 端到端执行应成功"
        assert run_pytest(ws_tmp, "tests"), (
            f"{case.name} 端到端后 pytest 应全绿")
        reg = ws_tmp / case.regression_rel
        assert reg.exists(), f"{case.name} 应产出回归测试文件"
        assert llm.calls, "应至少有一次 LLM 调用"
    finally:
        await loop.close()