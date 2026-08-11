"""Unified diff 渲染 —— 把文件变更渲染为带颜色的 diff 行。

- `--- a/path` / `+++ b/path`：亮白
- `@@ ... @@`：青色
- `-` 行：红色；`+` 行：绿色；上下文行：暗灰
纯 ASCII 输出，无 emoji，遵循“Diff 视图”设计一节。
"""
from __future__ import annotations

import difflib
from typing import List, Optional

from rich.text import Text

_HEADER_STYLE = "bold white"
_HUNK_STYLE = "cyan"
_DEL_STYLE = "red"
_ADD_STYLE = "green"
_CTX_STYLE = "bright_black"


def render_unified_diff(
    path: str,
    before: Optional[str],
    after: str,
    context: int = 3,
) -> List[Text]:
    """生成 unified diff 的彩色行列表。

    新建文件（before 为 None）时给出完整新增视图；无变更时返回提示行。
    """
    if before is None:
        added = after.splitlines()
        lines = [
            Text("--- /dev/null", style=_HEADER_STYLE),
            Text(f"+++ b/{path}", style=_HEADER_STYLE),
            Text(f"@@ -0,0 +1,{len(added)} @@", style=_HUNK_STYLE),
        ]
        lines.extend(Text(f"+{line}", style=_ADD_STYLE) for line in added)
        return lines
    if before == after:
        return [Text(f"（无变更）: {path}", style=_CTX_STYLE)]

    diff_lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}",
        lineterm="", n=context,
    ))
    out: List[Text] = []
    for raw in diff_lines:
        if raw.startswith(("---", "+++")):
            out.append(Text(raw, style=_HEADER_STYLE))
        elif raw.startswith("@@"):
            out.append(Text(raw, style=_HUNK_STYLE))
        elif raw.startswith("-"):
            out.append(Text(raw, style=_DEL_STYLE))
        elif raw.startswith("+"):
            out.append(Text(raw, style=_ADD_STYLE))
        else:
            out.append(Text(raw, style=_CTX_STYLE))
    return out


def diff_summary(path: str, before: Optional[str], after: str) -> str:
    """简短变更摘要：新建 / 增删行数统计。"""
    if before is None:
        return f"{path}: 新建文件，{len(after.splitlines())} 行"
    if before == after:
        return f"{path}: 无变更"
    sm = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(),
                                 autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            added += j2 - j1
        if tag in ("replace", "delete"):
            removed += i2 - i1
    return f"{path}: +{added} -{removed}"


__all__ = ["render_unified_diff", "diff_summary"]
