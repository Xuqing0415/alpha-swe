"""回归检测 —— 阶段三 3.3（改完就测、测不过就停）。

Agent 通过 file_ops 修改 .py 后，自动运行受影响模块的测试：
- 只运行与改动模块对应的 test_*.py（同目录或 tests/ 布局），不跑全量；
- 测试真实执行失败 -> regression：把失败作为该步骤的工具结果回写，
  触发"修复-重测"循环，重试耗尽后由 criticality 决定失败/跳过；
- 测试框架无法启动/超时/无对应测试 -> skip：不误报回归，只记录决策点。

三态分类 classify_test_result()：clean / regression / skip。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# 测试框架未能真正执行的输出标记（应判为 skip 而非 regression）
_SKIP_MARKERS = (
    "启动测试失败",
    "找不到测试命令",
    "测试超时",
    "不支持的测试框架",
    "no tests ran",  # pytest 未收集到任何用例：无可验证内容，按跳过处理
)
# pytest 确实执行过的输出标记（判为 regression）
_RUN_MARKERS = (
    "passed",
    "failed",
    "error",
    "no tests ran",
    "====",
    "collected",
)


def affected_test_path(workspace: str,
                       module_path: str) -> Optional[str]:
    """返回模块对应的测试文件（工作区相对路径）；不存在返回 None。"""
    root = Path(workspace).resolve()
    p = Path(module_path)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            return None
    else:
        rel = p
    stem = rel.stem.lower()
    if stem.startswith("test_"):
        return None
    candidates = [
        rel.parent / f"test_{rel.name}",
        Path("tests") / f"test_{rel.name}",
    ]
    for c in candidates:
        if (root / c).is_file():
            return str(c).replace("\\", "/")
    return None


def classify_test_result(result) -> str:
    """三态判定：clean（通过）/ regression（真实失败）/ skip（无法运行）。"""
    if getattr(result, "success", False):
        return "clean"
    if getattr(result, "failures", None):
        return "regression"
    output = str(getattr(result, "output", "") or "")
    if any(m in output for m in _SKIP_MARKERS):
        return "skip"
    lower = output.lower()
    if any(m in lower for m in _RUN_MARKERS):
        return "regression"
    return "skip"


__all__ = ["affected_test_path", "classify_test_result"]
