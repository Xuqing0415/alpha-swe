"""项目状态感知（主线一 1.1）：跨会话维护"项目心智模型"。

- 项目结构快照（目录树、文件大小、mtime）；
- 依赖变更历史（何时升级了什么包、为什么）；
- 技术栈检测（复用 agent.code.project_profile）；
- 最近修改记录（本次会话改过的文件）；
- 测试健康状况（最近一次测试结果）；
- 技术债标记（Agent 或用户标注的"需要重构"位置）。

快照存储在项目 `.swe-agent/state.json`（已 gitignore），每次会话开始
对比上次快照生成差异文本并注入 Prompt，让 Agent 感知"上次会话以来
项目发生了什么变化"（如依赖升级后需要检查 breaking changes）。
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("alpha-swe.project_state")

_STATE_FILE = ".swe-agent/state.json"
_DEP_FILES = ("package.json", "requirements.txt", "pyproject.toml")
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".idea", ".vscode", ".swe-agent", "logs", "test_workspace",
    ".codex", ".agents",
})
_SKIP_SUFFIXES = (".pyc", ".pyo", ".whl", ".egg-info")
_MAX_RECENT = 200
_MAX_HISTORY = 200
_MAX_DIFF_FILES = 40


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _skip(rel_parts: tuple) -> bool:
    return any(seg in _SKIP_DIRS or seg.endswith(_SKIP_SUFFIXES)
               for seg in rel_parts)


def _parse_package_json(text: str) -> Dict[str, str]:
    """package.json -> {"package.json::<dep>": version}。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    out: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        for name, ver in (data.get(section) or {}).items():
            out[f"package.json::{name}"] = str(ver)
    return out


def _parse_requirements(text: str) -> Dict[str, str]:
    """requirements.txt -> {"requirements.txt::<dep>": spec}。"""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", ".")):
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(==|>=|<=|~=|!=)?\s*([^\s;]+)?", line)
        if not m:
            continue
        name, op, ver = m.group(1).lower(), m.group(2) or "", m.group(3) or ""
        out[f"requirements.txt::{name}"] = op + ver
    return out


def _parse_pyproject(text: str) -> Dict[str, str]:
    """pyproject.toml -> {"pyproject.toml::<dep>": spec}。"""
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        data = {}
    out: Dict[str, str] = {}
    proj = data.get("project") or {}
    for item in proj.get("dependencies") or []:
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*([<>=!~].*)?$", str(item).strip())
        if m:
            out[f"pyproject.toml::{m.group(1).lower()}"] = m.group(2) or ""
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, ver in poetry.items():
        if name == "python":
            continue
        if isinstance(ver, dict):
            ver = ver.get("version", "")
        out[f"pyproject.toml::{name.lower()}"] = str(ver)
    return out


def _collect_deps(root: Path) -> Dict[str, Dict[str, str]]:
    """读取依赖清单文件 -> {文件: {依赖: 版本}}。"""
    deps: Dict[str, Dict[str, str]] = {}
    for name in _DEP_FILES:
        p = root / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name == "package.json":
            parsed = _parse_package_json(text)
        elif name == "requirements.txt":
            parsed = _parse_requirements(text)
        else:
            parsed = _parse_pyproject(text)
        if parsed:
            deps[name] = parsed
    return deps


class ProjectStateTracker:
    """项目状态跟踪器：扫描/对比/持久化项目快照。"""

    def __init__(self, workspace: str, decision_logger=None,
                 state_file: Optional[str] = None) -> None:
        self.workspace = os.path.abspath(workspace)
        self.state_file = Path(state_file) if state_file else (
            Path(self.workspace) / _STATE_FILE)
        self.decision_logger = decision_logger
        self._state: Dict[str, Any] = self._load()
        self._start_snapshot: Dict[str, Any] = {}
        self._agent_intended: Dict[str, Any] = {}
        self._session_open = False

    # ---- 加载 / 保存 ----
    def _load(self) -> Dict[str, Any]:
        """加载状态快照；主文件损坏时依次回退 .bak1 / .bak2（1.1B 自愈）。"""
        for f in (self.state_file,
                  self._backup_path(self.state_file, 1),
                  self._backup_path(self.state_file, 2)):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("schema") == 1:
                    return data
            except (OSError, ValueError):
                continue
        return self._new_state()

    @staticmethod
    def _backup_path(base: Path, idx: int) -> Path:
        return base.with_name(f"{base.name}.bak{idx}")

    def _rotate_backups(self) -> None:
        bak1 = self._backup_path(self.state_file, 1)
        bak2 = self._backup_path(self.state_file, 2)
        try:
            if bak1.exists():
                if bak2.exists():
                    bak2.unlink()
                os.replace(bak1, bak2)
        except OSError:
            pass
        try:
            os.replace(self.state_file, bak1)
        except OSError:
            pass

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        return {
            "schema": 1,
            "updated_at": "",
            "structure": {},
            "deps": {},
            "tech_stack": [],
            "dependency_history": [],
            "recent_changes": [],
            "test_health": {"last_run_at": "", "passed": 0, "failed": 0,
                            "coverage": None},
            "tech_debt": [],
        }

    def save(self) -> None:
        """原子写入：先写临时文件再 rename，写入前轮换 .bak1/.bak2。"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state["updated_at"] = _now()
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8")
            if self.state_file.exists():
                self._rotate_backups()
            os.replace(tmp, self.state_file)
        except OSError as e:
            logger.warning("项目状态快照写入失败: %s", e)

    @property
    def state(self) -> Dict[str, Any]:
        return self._state

    # ---- 快照扫描 ----
    def scan(self) -> Dict[str, Any]:
        """当前项目结构 + 依赖清单 + 技术栈。"""
        root = Path(self.workspace)
        structure: Dict[str, Dict[str, int]] = {}
        if root.is_dir():
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue
                if _skip(tuple(rel.split("/"))):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                structure[rel] = {"mtime_ns": st.st_mtime_ns,
                                  "size": st.st_size}
        deps = _collect_deps(root) if root.is_dir() else {}
        return {"structure": structure, "deps": deps,
                "tech_stack": self._tech_stack(root, list(structure))}

    # ---- 主线一 1.1A：三层快照对比（Last_known / Current_disk / Agent_intended） ----
    def _tech_stack(self, root: Path,
                    files: Optional[Iterable[str]] = None) -> List[str]:
        try:
            from agent.code.project_profile import build_profile
            profile = build_profile(str(root), files=files)
            return list(profile.tech_stack)
        except Exception as e:
            logger.warning("技术栈检测失败: %s", e)
            return []

    def note_agent_write(self, path: str) -> None:
        """记录 Agent 写入后的 intended 状态（内容 sha1 + mtime/size）。

        由执行引擎在 file_ops 写成功后调用；用于会话结束时区分
        Agent 正常修改 / 外部覆盖 / 回滚。
        """
        root = Path(self.workspace)
        raw = Path(path)
        if raw.is_absolute():
            try:
                rel = raw.relative_to(root).as_posix()
            except ValueError:
                return
        else:
            rel = raw.as_posix()
        if _skip(tuple(rel.split("/"))):
            return
        target = root / rel
        if not target.is_file():
            self._agent_intended[rel] = {"deleted": True}
            return
        try:
            st = target.stat()
            digest = hashlib.sha1(
                target.read_bytes()).hexdigest()
        except OSError:
            return
        self._agent_intended[rel] = {
            "mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha1": digest,
        }

    @staticmethod
    def _classify_conflicts(last_known: Dict[str, Any],
                            current: Dict[str, Any],
                            intended: Dict[str, Any]) -> List[Dict[str, Any]]:
        """三层对比：Last_known vs Current_disk vs Agent_intended。

        分类规则（1.1A 表格）：
        - Agent 正常修改：intended 存在且 == current；
        - 文件被回滚：intended != current 且 current == last_known；
        - 外部覆盖：intended != current 且 current != last_known；
        - 外部修改：非 Agent 写入文件且 last_known != current。
        """
        conflicts: List[Dict[str, Any]] = []
        paths = set(last_known) | set(current) | set(intended)
        for p in sorted(paths):
            last = last_known.get(p)
            cur = current.get(p)
            intent = intended.get(p)
            if intent is not None:
                if intent.get("deleted"):
                    if cur is not None:
                        conflicts.append({"path": p, "kind": "reverted",
                                          "detail": "Agent 已删除该文件，但磁盘上仍存在"})
                    continue
                if cur is None:
                    conflicts.append({"path": p, "kind": "external_delete",
                                      "detail": "Agent 写入后文件被外部删除"})
                    continue
                if ProjectStateTracker._same_intent(intent, cur):
                    continue  # Agent 正常修改
                if last is not None and ProjectStateTracker._sig_eq(last, cur):
                    conflicts.append({"path": p, "kind": "reverted",
                                      "detail": "文件被回滚到 Agent 修改前状态（与上次快照一致）"})
                else:
                    conflicts.append({"path": p, "kind": "external_overwrite",
                                      "detail": "Agent 写入后的内容与当前磁盘不一致，疑似被外部覆盖"})
                continue
            last_sig = ProjectStateTracker._sig(last)
            cur_sig = ProjectStateTracker._sig(cur)
            if last is None and cur is not None:
                conflicts.append({"path": p, "kind": "external_added",
                                  "detail": "外部新增文件"})
            elif last is not None and cur is None:
                conflicts.append({"path": p, "kind": "external_removed",
                                  "detail": "外部删除文件"})
            elif last_sig is not None and last_sig != cur_sig:
                conflicts.append({"path": p, "kind": "external_modified",
                                  "detail": "外部手动修改文件"})
        return conflicts

    @staticmethod
    def _sig(entry: Optional[Dict[str, Any]]) -> Optional[tuple]:
        if not entry:
            return None
        if entry.get("deleted"):
            return None
        return (entry.get("mtime_ns"), entry.get("size"))

    @staticmethod
    def _sig_eq(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        sa, sb = ProjectStateTracker._sig(a), ProjectStateTracker._sig(b)
        return sa is not None and sa == sb

    @staticmethod
    def _same_intent(intent: Dict[str, Any],
                     current: Dict[str, Any]) -> bool:
        """intended 与当前磁盘是否一致：优先内容 sha1，缺省回退 mtime/size。"""
        if intent.get("sha1") and current.get("sha1"):
            return intent["sha1"] == current["sha1"]
        return ProjectStateTracker._sig_eq(intent, current)

    def _conflict_text(self, conflicts: List[Dict[str, Any]]) -> str:
        """三层快照冲突摘要（注入 Prompt / TUI 展示）。"""
        if not conflicts:
            return ""
        lines = ["## 会话期间的文件变更冲突"]
        kind_label = {
            "external_overwrite": "外部覆盖",
            "reverted": "回滚",
            "external_delete": "外部删除",
            "external_added": "外部新增",
            "external_removed": "外部移除",
            "external_modified": "外部修改",
        }
        for c in conflicts[:_MAX_DIFF_FILES]:
            lines.append("- " + c["path"] + ": " +
                         kind_label.get(c.get("kind", "unknown"),
                                        c.get("kind")) + " （" +
                         c.get("detail", "") + "）")
        return "\n".join(lines)

    # ---- 会话生命周期 ----
    def begin_session(self) -> Dict[str, Any]:
        """会话开始：记录起始快照，对比上次状态生成差异。

        首次进入项目（无历史基线）时直接落盘当前快照，返回空差异。
        """
        current = self.scan()
        self._start_snapshot = current
        self._agent_intended = {}
        self._session_open = True
        previous = self._state
        if not previous.get("structure") and not previous.get("deps"):
            self._state["structure"] = current["structure"]
            self._state["deps"] = current["deps"]
            self._state["tech_stack"] = current["tech_stack"]
            self.save()
            return {}
        diff = self._compute_diff(previous, current)
        # 跨会话的依赖变化（如用户手动升级）补记历史
        for d in diff.get("deps") or []:
            self._append_dep_history(d)
        return diff

    def end_session(self) -> Dict[str, Any]:
        """会话结束：记录本次会话修改的文件与依赖变化，更新基线并落盘。"""
        if not self._session_open:
            return {}
        self._session_open = False
        current = self.scan()
        changes = self._compute_session_changes(self._start_snapshot, current)
        ts = _now()
        for item in changes.get("files") or []:
            item = dict(item, changed_at=ts)
            self._state.setdefault("recent_changes", []).insert(0, item)
        del self._state["recent_changes"][_MAX_RECENT:]
        for d in changes.get("deps") or []:
            self._append_dep_history(d)
        # 主线一 1.1A：三层快照对比（Last_known vs Current vs Intended）
        last_known = self._state.get("structure", {}) or {}
        cur_struct = current.get("structure", {}) or {}
        conflicts = self._classify_conflicts(
            last_known, cur_struct, self._agent_intended)
        if conflicts:
            history = self._state.setdefault("conflicts_history", [])
            history.insert(0, {"at": _now(), "items": conflicts})
            del history[50:]
        changes["conflicts"] = conflicts
        changes["conflict_text"] = self._conflict_text(conflicts)
        self._state["structure"] = current["structure"]
        self._state["deps"] = current["deps"]
        if current["tech_stack"]:
            self._state["tech_stack"] = current["tech_stack"]
        self.save()
        return changes

    def _append_dep_history(self, item: Dict[str, Any]) -> None:
        history = self._state.setdefault("dependency_history", [])
        history.insert(0, dict(item, changed_at=_now()))
        del history[_MAX_HISTORY:]

    # ---- 差异计算 ----
    @staticmethod
    def _compute_diff(previous: Dict[str, Any],
                      current: Dict[str, Any]) -> Dict[str, Any]:
        diff: Dict[str, Any] = {
            "deps": [],
            "files": {"added": [], "removed": [], "modified": []},
        }
        old_deps = previous.get("deps", {}) or {}
        new_deps = current.get("deps", {}) or {}
        for fname in sorted(set(old_deps) | set(new_deps)):
            old_map = old_deps.get(fname, {}) or {}
            new_map = new_deps.get(fname, {}) or {}
            for key in sorted(set(old_map) | set(new_map)):
                old_v, new_v = old_map.get(key), new_map.get(key)
                if old_v == new_v:
                    continue
                diff["deps"].append({
                    "file": fname, "dep": key.split("::", 1)[-1],
                    "old": old_v, "new": new_v,
                })
        old_struct = previous.get("structure", {}) or {}
        new_struct = current.get("structure", {}) or {}
        old_paths, new_paths = set(old_struct), set(new_struct)
        diff["files"]["added"] = sorted(new_paths - old_paths)
        diff["files"]["removed"] = sorted(old_paths - new_paths)
        diff["files"]["modified"] = sorted(
            p for p in (new_paths & old_paths)
            if (new_struct[p].get("mtime_ns"), new_struct[p].get("size"))
            != (old_struct[p].get("mtime_ns"), old_struct[p].get("size")))
        return diff

    @staticmethod
    def _compute_session_changes(start: Dict[str, Any],
                                 current: Dict[str, Any]) -> Dict[str, Any]:
        changes: Dict[str, Any] = {"files": [], "deps": []}
        s_struct = start.get("structure", {}) or {}
        c_struct = current.get("structure", {}) or {}
        for p in sorted(set(s_struct) | set(c_struct)):
            old = s_struct.get(p)
            new = c_struct.get(p)
            if old is None:
                changes["files"].append({"path": p, "kind": "added"})
            elif new is None:
                changes["files"].append({"path": p, "kind": "removed"})
            elif (old.get("mtime_ns"), old.get("size")) != (
                    new.get("mtime_ns"), new.get("size")):
                changes["files"].append({"path": p, "kind": "modified"})
        s_deps = start.get("deps", {}) or {}
        c_deps = current.get("deps", {}) or {}
        for fname in sorted(set(s_deps) | set(c_deps)):
            sm = s_deps.get(fname, {}) or {}
            cm = c_deps.get(fname, {}) or {}
            for key in sorted(set(sm) | set(cm)):
                if sm.get(key) != cm.get(key):
                    changes["deps"].append({
                        "file": fname, "dep": key.split("::", 1)[-1],
                        "old": sm.get(key), "new": cm.get(key)})
        return changes

    # ---- 差异文本（注入 Prompt） ----
    def diff_text(self, diff: Optional[Dict[str, Any]] = None) -> str:
        if not diff:
            return ""
        lines: List[str] = ["## 上次会话以来的项目变化"]
        deps = diff.get("deps") or []
        if deps:
            lines.append("- 依赖变更:")
            for d in deps[:10]:
                old = d.get("old") or "（新增）"
                new = d.get("new") or "（移除）"
                tail = "（建议检查 breaking changes）" if old and new else ""
                lines.append(f"  - {d['file']}: {d['dep']} {old} -> {new}{tail}")
        files = diff.get("files") or {}
        for kind, label in (("added", "新增文件"), ("modified", "修改文件"),
                            ("removed", "删除文件")):
            items = files.get(kind) or []
            if items:
                shown = items[:_MAX_DIFF_FILES]
                suffix = f" 等 {len(items)} 个" if len(items) > len(shown) else ""
                lines.append(f"- {label}: {', '.join(shown)}{suffix}")
        return "\n".join(lines) if len(lines) > 1 else ""

    # ---- 状态维护 API ----
    def record_test_result(self, passed: int, failed: int,
                           coverage: Optional[float] = None) -> None:
        health = self._state.setdefault("test_health", {})
        health.update({"last_run_at": _now(), "passed": passed,
                       "failed": failed, "coverage": coverage})

    def add_tech_debt(self, note: str, added_by: str = "user") -> None:
        debt = self._state.setdefault("tech_debt", [])
        debt.insert(0, {"note": note, "added_by": added_by,
                        "added_at": _now(), "status": "open"})
        self.save()

    def list_tech_debt(self) -> List[Dict[str, Any]]:
        return list(self._state.get("tech_debt") or [])

    def resolve_tech_debt(self, note: str) -> bool:
        for item in self._state.get("tech_debt") or []:
            if item.get("note") == note:
                item["status"] = "resolved"
                item["resolved_at"] = _now()
                self.save()
                return True
        return False
