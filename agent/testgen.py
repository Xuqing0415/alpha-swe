"""自动测试生成 —— 进阶 3.1（解决"改动无测试覆盖"缺口）。

Agent 通过 file_ops 写入/编辑 .py 文件后，若对应 test_*.py 不存在，
基于 AST 生成保守的 pytest 冒烟测试：
- import 可用性测试（模块能否被导入）；
- 每个公开函数/方法的可调用性测试；
- 全默认参数且无副作用（无 IO）纯函数的默认调用冒烟测试。

规则式生成（离线可测、不依赖 LLM）；边界/异常路径的语义级测试由
Agent 在生成后按需补强（AST 提供函数签名与默认值作为输入）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HEADER = (
    "# 本文件由 alpha-swe 自动生成（进阶 3.1 自动测试生成）。\n"
    "# 覆盖: import 可用性 + 公开符号可调用性 + 纯函数默认调用冒烟。\n"
)

# 出现这些标记的函数视为有副作用，不做默认调用冒烟（避免误触发真实 IO）
_IO_MARKERS = (
    "open(", "print(", "input(", "subprocess", "os.system", "os.popen",
    "requests.", "urllib.", "socket.", "exec(", "eval(", "tempfile.",
    "read_text", "write_text", "mkdir", "unlink",
)


def _test_file_path(workspace: str, module_path: str) -> Path:
    """计算与模块同目录的 test_<stem>.py 路径（锚定在工作区内）。"""
    root = Path(workspace).resolve()
    target = Path(module_path)
    if target.is_absolute():
        try:
            rel = target.resolve().relative_to(root)
        except ValueError:
            raise PermissionError(f"路径越界: {module_path}")
    else:
        rel = target
    return root / rel.parent / f"test_{rel.name}"


def needs_tests(workspace: str, module_path: str) -> bool:
    """目标模块是否缺少对应测试文件（缺失才需要生成）。"""
    p = Path(module_path)
    stem = p.stem.lower()
    if stem.startswith("test_"):
        return False
    test_path = _test_file_path(workspace, module_path)
    if test_path.exists():
        return False
    alt = Path(workspace).resolve() / "tests" / f"test_{p.name}"
    if alt.exists():
        return False
    return True


def _node_source(source: str, node: ast.AST) -> str:
    if getattr(node, "end_lineno", None) is None:
        return ""
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


def extract_targets(source: str) -> List[Dict[str, Any]]:
    """提取公开函数/类方法作为测试目标（跳过私有与顶层 main 块）。"""
    targets: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return targets
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        if node.name == "main" and node.col_offset == 0:
            continue
        args = [a.arg for a in node.args.args if a.arg != "self"]
        defaults = node.args.defaults
        n_required = max(0, len(args) - len(defaults))
        body = _node_source(source, node)
        pure = not any(m in body for m in _IO_MARKERS)
        targets.append({
            "name": node.name,
            "args": args,
            "n_required": n_required,
            "all_defaults": n_required == 0 and bool(args),
            "pure": pure,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
        })
    # 稳定排序（按源码出现顺序）
    return targets


def generate_tests(source: str, module_path: str) -> str:
    """基于源码生成 pytest 冒烟测试内容；无可生成目标返回空串。"""
    targets = extract_targets(source)
    if not targets:
        return ""
    module_name = Path(module_path).stem
    if not module_name.isidentifier():
        return ""  # 模块名不可作为标识符导入时不生成
    lines = [
        HEADER.rstrip("\n"),
        '"""自动生成的冒烟测试（进阶 3.1）。"""',
        "",
        "import asyncio",
        "import importlib",
        "",
        f"MODULE = importlib.import_module({module_name!r})",
        "",
        "",
        "def test_module_importable():",
        "    assert MODULE is not None",
        "",
        "",
    ]
    for t in targets:
        name = t["name"]
        lines.append(f"def test_{name}_callable():")
        lines.append(f"    assert callable(getattr(MODULE, {name!r}))")
        lines.extend(["", ""])
        if t["all_defaults"] and t["pure"]:
            lines.append(f"def test_{name}_default_call():")
            call = f"MODULE.{name}()"
            if t["is_async"]:
                lines.append(f"    result = asyncio.run({call})")
            else:
                lines.append(f"    result = {call}")
            lines.extend(["", ""])
    return "\n".join(lines)


def generate_and_write(workspace: str, module_path: str,
                       source: str) -> Optional[Tuple[str, int]]:
    """生成并写入 test_*.py；返回 (写入路径, 目标数)，无可生成目标返回 None。"""
    targets = extract_targets(source)
    if not targets:
        return None
    content = generate_tests(source, module_path)
    if not content:
        return None
    test_path = _test_file_path(workspace, module_path)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(content, encoding="utf-8")
    return str(test_path), len(targets)


async def verify_generated(workspace: str, test_path: str,
                           timeout: float = 120.0):
    """运行生成的测试文件验证可通过（best-effort，绝不抛异常）。"""
    from agent.code.test_runner import run_tests
    root = Path(workspace).resolve()
    p = Path(test_path)
    try:
        rel = p.resolve().relative_to(root)
        target = str(rel)
    except ValueError:
        target = str(p)
    try:
        return await run_tests("pytest", target, workspace, timeout=timeout)
    except Exception:
        return None


__all__ = [
    "extract_targets",
    "generate_and_write",
    "generate_tests",
    "needs_tests",
    "verify_generated",
]
