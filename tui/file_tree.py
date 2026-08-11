"""文件树视图 —— 工作区文件树构建/渲染 + Textual ListView 组件。

- 目录在前、文件在后，按名称排序；
- 经典 ASCII 树形连线：`+-- `（最后一项）/ `|-- `（兄弟项）；
- 默认隐藏 node_modules / .git / __pycache__ 等目录；
- 修改过的文件标记 `*`（黄色），当前操作文件前缀 `>`（青色）；
- ListView 自带虚拟滚动，大项目文件多时也流畅。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView

DEFAULT_IGNORED = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "logs", ".codex", "test_workspace",
}

_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".class"}


@dataclass
class TreeNode:
    """文件树节点（纯数据结构）。"""

    name: str
    path: str
    is_dir: bool
    size: int = 0
    children: List["TreeNode"] = field(default_factory=list)


def build_tree(root: str,
               ignored: Optional[Set[str]] = None) -> Optional[TreeNode]:
    """扫描根目录构建树；根不存在或不可读返回 None。"""
    root_path = Path(root)
    if not root_path.is_dir():
        return None
    ignore = DEFAULT_IGNORED | (ignored or set())
    return _build(root_path, root_path.name or root_path.as_posix(), ignore)


def _build(path: Path, name: str, ignore: Set[str]) -> TreeNode:
    node = TreeNode(name=name, path=str(path), is_dir=path.is_dir())
    if not path.is_dir():
        try:
            node.size = path.stat().st_size
        except OSError:
            pass
        return node
    try:
        children = list(path.iterdir())
    except OSError:
        return node
    children.sort(key=lambda p: (p.is_file(), p.name.lower()))
    for child in children:
        if child.name in ignore or child.suffix.lower() in _EXCLUDED_SUFFIXES:
            continue
        node.children.append(_build(child, child.name, ignore))
    return node


def iter_visible(node: TreeNode,
                 collapsed: Optional[Set[str]] = None,
                 filter_text: str = "") -> Iterator[Tuple[TreeNode, int, bool]]:
    """深度优先遍历可见节点，产出 (node, depth, is_last)。

    collapsed：折叠的目录 path 集合；filter_text：按名称子串过滤。
    """
    collapsed = collapsed or set()
    filter_text = (filter_text or "").strip().lower()

    def _matches(n: TreeNode) -> bool:
        if not filter_text:
            return True
        if filter_text in n.name.lower():
            return True
        # 目录包含匹配的子项时也展示（祖先路径可见）
        return n.is_dir and any(_matches(c) for c in n.children)

    def _walk(n: TreeNode, depth: int, is_last: bool) -> Iterator[Tuple[TreeNode, int, bool]]:
        if not _matches(n):
            return
        yield n, depth, is_last
        if n.is_dir and n.path not in collapsed:
            children = [c for c in n.children if _matches(c)]
            for i, child in enumerate(children):
                yield from _walk(child, depth + 1, i == len(children) - 1)

    yield from _walk(node, 0, True)


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}M"
    if size >= 1024:
        return f"{size / 1024:.1f}K"
    return f"{size}B"


def render_node(node: TreeNode, depth: int, is_last: bool, *,
                modified: Optional[Set[str]] = None,
                active: str = "",
                selected: Optional[Set[str]] = None) -> Text:
    """把单个节点渲染为带缩进/连线/标记的 Text。"""
    modified = modified or set()
    selected = selected or set()
    if depth == 0:
        prefix = ""
    else:
        prefix = "  " + ("    " * (depth - 1)) + ("+-- " if is_last else "|-- ")
    name = node.name + ("/" if node.is_dir else "")
    parts = Text()
    if active and node.path == active:
        parts.append("> ", style="cyan")
    if node.is_dir:
        parts.append(prefix + name, style="bold white")
    else:
        parts.append(prefix + name, style="white")
        parts.append(" " * max(1, 30 - len(prefix + name))
                     + _fmt_size(node.size), style="bright_black")
    if node.path in selected:
        parts.append(" [x]", style="yellow")
    if node.path in modified:
        parts.append(" *", style="yellow")
    return parts


class FileTreeView(ListView):
    """文件树视图组件：展开/折叠、导航、过滤、修改/活动标记。"""

    BINDINGS = [
        Binding("right", "toggle_node", "展开", show=False),
        Binding("left", "collapse_node", "折叠", show=False),
        Binding("/", "focus_search", "搜索", show=False),
        Binding("space", "toggle_select", "多选", show=False),
        Binding("a", "select_all_visible", "全选", show=False),
        Binding("x", "clear_selection", "清空选择", show=False),
        Binding("o", "open_selected", "打开选中", show=False),
        Binding("c", "copy_selected_paths", "复制路径", show=False),
    ]

    def __init__(self, root: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._root = root
        self._tree: Optional[TreeNode] = None
        self._collapsed: Set[str] = set()
        self._filter = ""
        self._modified: Set[str] = set()
        self._active = ""
        self._selected_paths: Set[str] = set()

    def refresh_tree(self, root: Optional[str] = None) -> None:
        """重新扫描并渲染文件树（保持折叠/过滤状态）。"""
        if root is not None:
            self._root = root
        if self._root:
            self._tree = build_tree(self._root)
        self.render_visible()

    def set_marks(self, modified: Set[str], active: str = "") -> None:
        self._modified = set(modified)
        self._active = active or ""
        self.render_visible()

    def set_filter(self, text: str) -> None:
        self._filter = text
        self.render_visible()

    def has_path(self, path: str) -> bool:
        """树中是否已存在该路径（增量更新：新文件才重建）。"""
        target = str(Path(path))
        if self._tree is None:
            return False
        stack = [self._tree]
        while stack:
            node = stack.pop()
            if node.path == target:
                return True
            stack.extend(node.children)
        return False

    def selection_count(self) -> int:
        return len(self._selected_paths)

    # ---- 多选与批量操作 ----
    def _notify_host(self) -> None:
        host = self.app
        if hasattr(host, "on_tree_selection_changed"):
            host.on_tree_selection_changed()

    def action_toggle_select(self) -> None:
        """Space：选中/取消选中当前高亮节点。"""
        item = self.highlighted_child
        node = getattr(item, "_tree_node", None) if item else None
        if node is None:
            return
        if node.path in self._selected_paths:
            self._selected_paths.discard(node.path)
        else:
            self._selected_paths.add(node.path)
        self.render_visible()
        self._notify_host()

    def action_select_all_visible(self) -> None:
        """A：全选当前可见文件（过滤结果内）。"""
        if self._tree is None:
            return
        for node, _depth, _last in iter_visible(
                self._tree, self._collapsed, self._filter):
            if not node.is_dir:
                self._selected_paths.add(node.path)
        self.render_visible()
        self._notify_host()

    def action_clear_selection(self) -> None:
        """X：清空所有选择。"""
        self._selected_paths.clear()
        self.render_visible()
        self._notify_host()

    def action_open_selected(self) -> None:
        """O：批量打开选中文件（终端区预览）。"""
        paths = sorted(self._selected_paths)
        if not paths:
            return
        host = self.app
        if hasattr(host, "show_tree_files"):
            host.show_tree_files(paths)

    def action_copy_selected_paths(self) -> None:
        """C：把选中路径复制到输入栏供继续输入命令。"""
        paths = sorted(self._selected_paths)
        if not paths:
            return
        host = self.app
        if hasattr(host, "copy_tree_paths"):
            host.copy_tree_paths(paths)

    def render_visible(self) -> None:
        keep = self.index if self.index is not None else 0
        self.clear()
        if self._tree is None:
            self.append(ListItem(Label("（工作区不可用）", classes="dim")))
            return
        for node, depth, is_last in iter_visible(
                self._tree, self._collapsed, self._filter):
            row = render_node(node, depth, is_last,
                              modified=self._modified, active=self._active,
                              selected=self._selected_paths)
            item = ListItem(Label(row))
            item._tree_node = node  # type: ignore[attr-defined]
            self.append(item)
        if self.children:
            self.index = min(keep, len(self.children) - 1)

    # ---- 动作 ----
    def action_toggle_node(self) -> None:
        item = self.highlighted_child
        node = getattr(item, "_tree_node", None) if item else None
        if node is not None and node.is_dir:
            if node.path in self._collapsed:
                self._collapsed.discard(node.path)
            else:
                self._collapsed.add(node.path)
            self.render_visible()

    def action_collapse_node(self) -> None:
        item = self.highlighted_child
        node = getattr(item, "_tree_node", None) if item else None
        if node is not None and node.is_dir:
            self._collapsed.add(node.path)
            self.render_visible()

    def action_focus_search(self) -> None:
        host = self.app
        if hasattr(host, "focus_tree_search"):
            host.focus_tree_search()

    def on_list_view_selected(self, event) -> None:
        """Enter 打开文件：展示在终端区（等同 cat）。"""
        node = getattr(event.item, "_tree_node", None)
        if node is None or node.is_dir:
            self.action_toggle_node()
            return
        host = self.app
        if hasattr(host, "show_tree_file"):
            host.show_tree_file(node.path)


__all__ = ["TreeNode", "build_tree", "iter_visible", "render_node",
           "FileTreeView", "DEFAULT_IGNORED"]
