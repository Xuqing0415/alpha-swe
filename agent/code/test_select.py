# -*- coding: utf-8 -*-
"""相关测试选择（方向一·优化 3.4 提升代码修改与验证闭环）。

根据推荐/修改文件选择最相关的测试目标，避免每次都跑全量测试：
- 同名测试文件：tests/test_<base>.py / test_<base>.py / <base>_test.py；
- 调用方测试：被推荐文件符号的调用方/被调方所在文件对应的测试文件。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

_TEST_DIRS = ("tests", "test", "testing")


def _candidate_test_paths(rel_src: str, root: Path) -> List[str]:
    base = Path(rel_src)
    stem = base.stem
    names = [f"test_{stem}.py", f"{stem}_test.py"]
    candidates: List[str] = []
    for d in [root / "tests", base.parent]:
        for name in names:
            p = d / name
            if p.is_file():
                try:
                    candidates.append(p.relative_to(root).as_posix())
                except ValueError:
                    continue
    return candidates


def select_related_tests(modified_files: Iterable[str], repo_root: str,
                         call_graph=None, max_targets: int = 5) -> List[str]:
    """返回与改动/推荐文件相关的 pytest 目标列表（不存在则返回空）。"""
    root = Path(repo_root)
    src_files = [str(f) for f in modified_files
                 if Path(str(f)).suffix.lower() in
                 (".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".go",
                  ".rs", ".java")]
    targets: List[str] = []
    seen: set = set()
    for rel in src_files:
        for c in _candidate_test_paths(rel, root):
            if c not in seen:
                seen.add(c)
                targets.append(c)
        if len(targets) >= max_targets:
            break
    if call_graph is not None and len(targets) < max_targets:
        for rel in src_files:
            for impact in call_graph.impact_files(str(rel)):
                for c in _candidate_test_paths(impact, root):
                    if c not in seen:
                        seen.add(c)
                        targets.append(c)
                        if len(targets) >= max_targets:
                            return targets
    return targets[:max_targets]
