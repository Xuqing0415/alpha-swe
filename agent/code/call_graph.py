"""轻量级函数调用图（阶段一 1.2，为影响范围分析提供数据）。

项目启动时构建一次：记录函数/类符号的定义位置与"谁调用了谁"。
当 Agent 要修改某个符号时，通过 callers_of() 找到所有受影响的上游调用方。
Python 使用标准库 ast 提取真实调用边；JS/TS 用正则做近似分段统计。
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from agent.code.ast_summary import CODE_EXTS

logger = logging.getLogger("alpha-swe.code")

_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_SKIP_DIRS = {"venv", ".venv", "node_modules", "dist", "build",
              "__pycache__", ".git", ".idea"}


@dataclass
class CallGraph:
    """项目级调用图：符号定义位置 + 调用边（caller -> callee）。"""
    defs: Dict[str, List[Tuple[str, int]]] = field(default_factory=dict)
    calls: Dict[str, Set[str]] = field(default_factory=dict)
    reverse: Dict[str, Set[str]] = field(default_factory=dict)
    functions: List[str] = field(default_factory=list)

    def callers_of(self, name: str) -> List[Tuple[str, str]]:
        """返回 (调用方符号, 所在文件) 列表（被谁调用/影响）。"""
        return sorted(
            (caller, self._file_of(caller))
            for caller in self.reverse.get(name, set())
        )

    def callees_of(self, name: str) -> List[str]:
        return sorted(self.calls.get(name, set()))

    def files_of(self, name: str) -> List[str]:
        return sorted({f for f, _ in self.defs.get(name, [])})

    def _file_of(self, symbol: str) -> str:
        for f, _ in self.defs.get(symbol, []):
            return f
        return ""

    def symbol_count(self) -> int:
        return len(self.functions)

    def edge_count(self) -> int:
        return sum(len(v) for v in self.calls.values())

    def to_text(self, max_lines: int = 40) -> str:
        """紧凑热区文本：被调用次数最多的符号（影响面最大）。"""
        if not self.functions:
            return ""
        ranked = sorted(self.functions,
                        key=lambda n: (-len(self.reverse.get(n, set())), n))
        lines = [f"[调用图] {self.symbol_count()} 个符号，{self.edge_count()} 条调用边"]
        shown = 0
        for name in ranked:
            callers = len(self.reverse.get(name, set()))
            if callers:
                lines.append(f"- {name} 被 {callers} 处调用 ({', '.join(self.files_of(name))})")
                shown += 1
                if shown >= max_lines:
                    break
        return "\n".join(lines)


def build_call_graph(root: str, files: Optional[Iterable[str]] = None,
                     max_files: int = 300) -> CallGraph:
    """扫描项目构建调用图；files 为空时自动 rglob（跳过依赖目录）。"""
    root_p = Path(root)
    cg = CallGraph()
    if not root_p.is_dir():
        return cg
    rel_files = list(files) if files is not None else _discover(root_p, max_files)
    for rel in rel_files[:max_files]:
        _index_file(root_p, str(rel), cg)
    return cg


def _discover(root: Path, max_files: int) -> List[str]:
    out: List[str] = []
    try:
        for p in root.rglob("*"):
            if len(out) >= max_files:
                break
            if not p.is_file() or p.suffix.lower() not in CODE_EXTS:
                continue
            rel = p.relative_to(root)
            if any(seg.startswith(".") or seg in _SKIP_DIRS for seg in rel.parts):
                continue
            out.append(str(rel))
    except OSError:
        pass
    return out


def _index_file(root: Path, rel: str, cg: CallGraph) -> None:
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    if rel.endswith(".py"):
        _index_python(rel, text, cg)
    elif Path(rel).suffix.lower() in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        _index_js(rel, text, cg)


def _index_python(rel: str, text: str, cg: CallGraph) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    units: List[Tuple[str, object]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    units.append((f"{node.name}.{sub.name}", sub))
    for name, node in units:
        _add_def(cg, rel, name, node.lineno)
        for sub in ast.walk(node):  # type: ignore[arg-type]
            if isinstance(sub, ast.Call):
                callee = _call_name(sub.func)
                if callee:
                    cg.calls.setdefault(name, set()).add(callee)
                    cg.reverse.setdefault(callee, set()).add(name)


def _call_name(func) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _add_def(cg: CallGraph, rel: str, name: str, lineno: int) -> None:
    if name not in cg.defs:
        cg.defs[name] = []
        cg.functions.append(name)
    cg.defs[name].append((rel, lineno))


def _index_js(rel: str, text: str, cg: CallGraph) -> None:
    from agent.code.ast_summary import _JS_ARROW, _JS_CLASS, _JS_FUNC
    raw: List[Tuple[str, int]] = []
    for m in _JS_FUNC.finditer(text):
        raw.append((m.group(1), m.start()))
    for m in _JS_ARROW.finditer(text):
        raw.append((m.group(1), m.start()))
    for m in _JS_CLASS.finditer(text):
        raw.append((m.group(1), m.start()))
    seen = set()
    unique: List[Tuple[str, int]] = []
    for name, pos in raw:
        if (name, pos) in seen:
            continue
        seen.add((name, pos))
        unique.append((name, pos))
        _add_def(cg, rel, name, text.count("\n", 0, pos) + 1)
    # 近似调用边：每个符号定义到下一个定义之间的 name( 调用
    for i, (name, pos) in enumerate(unique):
        end = unique[i + 1][1] if i + 1 < len(unique) else len(text)
        for m in _JS_CALL.finditer(text[pos:end]):
            callee = m.group(1)
            cg.calls.setdefault(name, set()).add(callee)
            cg.reverse.setdefault(callee, set()).add(name)
