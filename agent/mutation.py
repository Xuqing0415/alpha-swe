"""变异测试 —— 阶段三 3.2（验证测试是否真的有效）。

对 Agent 修改的代码做确定性变异（反转比较/算术/布尔算子、翻转布尔常量），
逐个运行受影响测试，统计"变异是否被发现"（检测率）。检测率低于
mutation_target_rate 时说明测试质量不足，应补强。

run_mutation_analysis() 采用"先基线后变异"策略：基线测试必须通过才会继续；
测试框架无法启动时整体 skip（不误报）。变异写入后立即在 finally 恢复原文件。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.code.test_runner import run_tests

# 确定性变异算子（阶段三 3.2：反转布尔条件 / 交换算术 / 翻转布尔常量）
_FLIP_CMP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Gt: ast.LtE, ast.GtE: ast.Lt,
    ast.Lt: ast.GtE, ast.LtE: ast.Gt,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_FLIP_BIN = {
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.Div, ast.Div: ast.Mult,
}
_FLIP_BOOL = {ast.And: ast.Or, ast.Or: ast.And}


def _op_name(op: ast.AST) -> str:
    if isinstance(op, ast.Compare):
        names = [type(o).__name__ for o in op.ops]
        return "cmp:" + "->".join(names)
    if isinstance(op, ast.Constant):
        return f"const:{op.value!r}->{not op.value!r}"
    return type(op).__name__


def _mutation_points(tree: ast.AST) -> List[tuple]:
    """收集可变异点：(node, index, op, flip_ctor)。"""
    points: List[tuple] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for idx, op in enumerate(node.ops):
                flip = _FLIP_CMP.get(type(op))
                if flip is not None:
                    points.append((node, idx, op, flip))
        elif isinstance(node, ast.BinOp):
            flip = _FLIP_BIN.get(type(node.op))
            if flip is not None:
                points.append((node, 0, node.op, flip))
        elif isinstance(node, ast.BoolOp):
            flip = _FLIP_BOOL.get(type(node.op))
            if flip is not None:
                points.append((node, 0, node.op, flip))
        elif isinstance(node, ast.Constant):
            value = node.value
            if value is True or value is False:
                points.append((node, 0, node, None))
    return points


def _apply_flip(node: ast.AST, point: tuple) -> None:
    """在指定节点上应用翻转（就地修改）。"""
    target, idx, _op, flip = point
    if isinstance(target, ast.Compare):
        target.ops[idx] = flip()
    elif isinstance(target, ast.BinOp):
        target.op = flip()
    elif isinstance(target, ast.BoolOp):
        target.op = flip()
    elif isinstance(target, ast.Constant):
        target.value = not target.value


def apply_mutations(source: str, limit: int = 10) -> List[Dict[str, Any]]:
    """生成确定性变异（每个变异只翻转一个点）。

    返回 [{name, mutated}]；源码不可解析或无变异点时返回空列表。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    points = _mutation_points(tree)
    results: List[Dict[str, Any]] = []
    for i, point in enumerate(points[:limit]):
        target = ast.parse(source)
        _apply_flip(target, _mutation_points(target)[i])
        ast.fix_missing_locations(target)
        try:
            mutated = ast.unparse(target)
        except Exception:
            continue
        results.append({"name": _op_name(point[2]), "mutated": mutated})
    return results


def mutation_score(analysis: Dict[str, Any]) -> Optional[float]:
    """返回变异检测率；分析被跳过或总数为 0 时返回 None。"""
    if analysis.get("skipped"):
        return None
    total = int(analysis.get("total", 0) or 0)
    if total <= 0:
        return None
    return float(analysis.get("killed", 0)) / total


async def run_mutation_analysis(workspace: str, module_path: str,
                                source: str, test_path: str,
                                timeout: float = 60.0,
                                max_mutations: int = 8) -> Dict[str, Any]:
    """运行变异测试：返回 {skipped, total, killed, score, survivors}。

    基线测试不通过或测试无法启动 -> skipped=True（不误报）。
    变异写盘后立即恢复原文件（finally 保证）。
    """
    base = await run_tests("pytest", test_path, workspace, timeout=timeout)
    if not base.success:
        return {"skipped": True, "total": 0, "killed": 0, "score": 0.0,
                "survivors": [],
                "reason": f"基线测试未通过，跳过变异检测: {str(base.output)[:120]}"}
    mutations = apply_mutations(source, limit=max_mutations)
    if not mutations:
        return {"skipped": True, "total": 0, "killed": 0, "score": 0.0,
                "survivors": [], "reason": "无可用变异算子"}
    module_file = Path(workspace).resolve() / module_path
    original = None
    if module_file.is_file():
        original = module_file.read_text(encoding="utf-8")
    killed = 0
    survivors: List[str] = []
    try:
        for m in mutations:
            module_file.parent.mkdir(parents=True, exist_ok=True)
            module_file.write_text(m["mutated"], encoding="utf-8")
            try:
                res = await run_tests("pytest", test_path, workspace,
                                      timeout=timeout)
                detected = not res.success
            except Exception:
                detected = False
            if detected:
                killed += 1
            else:
                survivors.append(m["name"])
    finally:
        if original is not None:
            module_file.write_text(original, encoding="utf-8")
        elif module_file.exists():
            module_file.unlink()
    total = len(mutations)
    return {"skipped": False, "total": total, "killed": killed,
            "score": (killed / total) if total else 0.0,
            "survivors": survivors, "reason": ""}


__all__ = ["apply_mutations", "mutation_score", "run_mutation_analysis"]
