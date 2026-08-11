"""插件引擎 —— 动态上下文注入（对应设计第 10 节）。

激活条件可组合叠加（命中任意一类即激活）：
- keywords：任务指令关键词（如「数据库」→ SQL 最佳实践插件）；
- file_ext：项目文件扩展名（如 .tsx → React+TS 规范插件）；
- project_file：项目文件路径模式（fnmatch 通配，如 **/package.json）；
- project_dep：项目依赖名（解析 package.json / requirements.txt / pyproject.toml）。

多插件同时命中时按 priority 降序注入，超出 max_active 截断；
config.active_plugins 作为白名单（非空时只激活列出的插件）。
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from agent.code.call_graph import CallGraph, build_call_graph
from agent.code.project_profile import ProjectProfile, build_profile

logger = logging.getLogger("alpha-swe.context.plugin")

FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
EXT_RE = re.compile(r"\.([A-Za-z0-9]+)(?=\Z|[^A-Za-z0-9])")
PATH_RE = re.compile(r"(?=[\w./\\-]*[A-Za-z])[\w./\\-]+\.(?:[A-Za-z0-9]+)")


def parse_front_matter(text: str) -> tuple[Dict[str, Any], str]:
    """解析 Markdown 文件头 YAML front-matter，返回 (meta, body)。"""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        logger.warning("front-matter 解析失败: %s", e)
        meta = {}
    return meta if isinstance(meta, dict) else {}, text[m.end():]


def _exts_of(files: Iterable[str]) -> Set[str]:
    exts: Set[str] = set()
    for f in files:
        m = EXT_RE.search(str(f))
        if m:
            exts.add(m.group(1).lower())
    return exts


def match_triggers(triggers: Dict[str, List[str]],
                   instruction: str,
                   files: Iterable[str],
                   deps: Iterable[str]) -> List[str]:
    """返回命中的触发类型列表（keywords / file_ext / project_file / project_dep）。"""
    hit_types: List[str] = []
    text = (instruction or "").lower()
    file_list = [str(f) for f in (files or [])]
    exts = _exts_of(file_list)
    dep_set = {str(d).lower() for d in (deps or [])}

    ext_scope = {str(e).lower().lstrip(".") for e in
                 (triggers.get("file_ext") or [])}
    for kw in triggers.get("keywords") or []:
        if str(kw).lower() in text:
            # 技能/插件声明了语言范围（file_ext）时，关键词命中还需项目文件
            # 类型匹配，避免语言无关关键词（如"重构"）误触发其他技术栈技能；
            # 无项目文件上下文（files 为空）时不做强限定，避免空上下文漏配
            if ext_scope and file_list and not (ext_scope & exts):
                break
            hit_types.append("keywords")
            break
    for ext in triggers.get("file_ext") or []:
        if str(ext).lower().lstrip(".") in exts:
            hit_types.append("file_ext")
            break
    for pat in triggers.get("project_file") or []:
        lowered = str(pat).lower()
        if any(fnmatch.fnmatch(f.lower(), lowered) or lowered in f.lower()
               for f in file_list):
            hit_types.append("project_file")
            break
    for dep in triggers.get("project_dep") or []:
        if str(dep).lower() in dep_set:
            hit_types.append("project_dep")
            break
    return hit_types


@dataclass
class Plugin:
    """单个插件：内容 + 优先级 + 多类触发条件。"""
    name: str
    content: str
    description: str = ""
    priority: int = 1
    version: str = "0.0.0"
    triggers: Dict[str, List[str]] = field(default_factory=dict)
    source: str = ""

    def render(self) -> str:
        head = f"[plugin:{self.name} v{self.version}]"
        if self.description:
            head += f" {self.description}"
        return f"{head}\n{self.content}"


class ProjectContext:
    """一次运行的项目上下文：文件清单 + 依赖 + 扩展名集合（供插件/技能匹配）。"""

    def __init__(self, files: Optional[List[str]] = None,
                 deps: Optional[Set[str]] = None,
                 root: str = "",
                 profile: Optional["ProjectProfile"] = None,
                 call_graph: Optional["CallGraph"] = None):
        self.files = [str(f) for f in (files or [])]
        self.deps = set(str(d) for d in (deps or []))
        self.root = root
        self.profile = profile
        self.call_graph = call_graph

    @property
    def profile_text(self) -> str:
        """项目约定摘要文本（阶段一 1.3，注入 Prompt 用）。"""
        return self.profile.to_text() if self.profile is not None else ""

    @property
    def exts(self) -> Set[str]:
        return _exts_of(self.files)

    def merge(self, other: "ProjectContext") -> "ProjectContext":
        seen = set(self.files)
        files = list(self.files)
        for f in other.files:
            if f not in seen:
                seen.add(f)
                files.append(f)
        return ProjectContext(files=files, deps=self.deps | other.deps,
                              root=self.root,
                              profile=self.profile if other.profile is None
                              else other.profile,
                              call_graph=self.call_graph if other.call_graph is None
                              else other.call_graph)

    @classmethod
    def scan(cls, workspace: str, max_depth: int = 2,
             max_files: int = 200,
             skip: Optional[Iterable[str]] = None) -> "ProjectContext":
        """扫描工作区文件（深度/数量受限），并解析依赖清单。"""
        skip_dirs = set(skip or {})
        files: List[str] = []
        root = Path(workspace)
        if not root.is_dir():
            return cls(files=[], root=str(root))
        try:
            for p in root.rglob("*"):
                if len(files) >= max_files:
                    break
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(root).parts
                if len(rel_parts) > max_depth:
                    continue
                if any(seg.startswith(".") or seg in skip_dirs
                       for seg in rel_parts):
                    continue
                files.append(str(Path(*rel_parts)))
        except OSError as e:
            logger.warning("工作区扫描失败: %s", e)
        root_s = str(root)
        profile = build_profile(root_s, files)
        call_graph = build_call_graph(root_s, files, max_files=max_files)
        return cls(files=files, deps=cls._detect_deps(files, root),
                   root=root_s, profile=profile, call_graph=call_graph)

    @staticmethod
    def _detect_deps(files: List[str], root: Path) -> Set[str]:
        """从 package.json / requirements.txt / pyproject.toml 提取依赖名。"""
        deps: Set[str] = set()
        try:
            if "package.json" in files:
                data = json.loads(
                    (root / "package.json").read_text(encoding="utf-8", errors="replace")
                )
                for section in ("dependencies", "devDependencies"):
                    deps.update((data.get(section) or {}).keys())
            req = root / "requirements.txt"
            if req.is_file():
                for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.split("#")[0].strip()
                    if line and not line.startswith("-"):
                        deps.add(re.split(r"[<>=!~\[\]]", line)[0].strip().lower())
            py = root / "pyproject.toml"
            if py.is_file():
                text = py.read_text(encoding="utf-8", errors="replace")
                try:
                    import tomllib
                    data = tomllib.loads(text)
                except Exception:
                    data = {}
                if data:
                    proj = data.get("project") or {}
                    for d in proj.get("dependencies") or []:
                        deps.add(re.split(r"[<>=!~\[\]@;]", d)[0].strip().lower())
                    for group in (proj.get("optional-dependencies") or {}).values():
                        for d in group or []:
                            deps.add(re.split(r"[<>=!~\[\]@;]", d)[0].strip().lower())
                    poetry = (data.get("tool") or {}).get("poetry") or {}
                    for d in (poetry.get("dependencies") or {}):
                        deps.add(str(d).lower())
                else:
                    # 非 TOML 可解析（极少见）时回退到朴素正则
                    for m in re.finditer(r"^\s*([A-Za-z0-9_.-]+)\s*[=~>]", text, re.MULTILINE):
                        deps.add(m.group(1).lower())
        except Exception as e:
            logger.warning("依赖解析失败: %s", e)
        return deps

    @classmethod
    def from_instruction(cls, instruction: str,
                         base: Optional["ProjectContext"] = None) -> "ProjectContext":
        """从任务指令中提取文件路径构造上下文，并与项目上下文合并。"""
        paths = sorted(set(PATH_RE.findall(instruction or "")))
        pc = cls(files=paths, deps=set())
        return base.merge(pc) if base else pc


class PluginManager:
    """插件热加载与激活：扫描目录（Markdown + front-matter），mtime 热刷新。"""

    def __init__(self, plugins_dir: str = "./plugins",
                 whitelist: Optional[List[str]] = None,
                 max_active: int = 5,
                 enabled: bool = True,
                 decision_logger=None):
        self.plugins_dir = plugins_dir
        self.whitelist = set(whitelist or [])
        self.max_active = max(1, max_active)
        self.enabled = enabled
        self.decision_logger = decision_logger
        self._plugins: Dict[str, Plugin] = {}
        self._mtime: Dict[str, float] = {}
        self.refresh()

    def refresh(self) -> None:
        """热加载：重扫目录，仅重读变更文件（新增/修改立即生效）。"""
        if not self.enabled:
            return
        d = Path(self.plugins_dir)
        if not d.is_dir():
            return
        try:
            for p in sorted(d.glob("*.md")):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if self._mtime.get(str(p)) == mtime:
                    continue
                self._load_file(p)
                self._mtime[str(p)] = mtime
        except OSError as e:
            logger.warning("插件目录扫描失败: %s", e)

    def _load_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_front_matter(text)
            name = str(meta.get("name") or path.stem).strip()
            plugin = Plugin(
                name=name,
                content=body.strip(),
                description=str(meta.get("description", "")),
                priority=int(meta.get("priority", 1)),
                version=str(meta.get("version", "0.0.0")),
                triggers={
                    k: [str(x) for x in (v if isinstance(v, list) else [v])]
                    for k, v in (meta.get("triggers") or {}).items()
                },
                source=str(path),
            )
            self._plugins[name] = plugin
            logger.info("热加载插件: %s (%s)", name, path)
        except Exception as e:
            logger.error("插件加载失败 %s: %s", path, e)

    def list_plugins(self) -> List[Plugin]:
        self.refresh()
        return list(self._plugins.values())

    def get(self, name: str) -> Optional[Plugin]:
        self.refresh()
        return self._plugins.get(name)

    def get_active(self, instruction: str,
                   files: Optional[Iterable[str]] = None,
                   deps: Optional[Iterable[str]] = None) -> List[Plugin]:
        """按指令 + 项目上下文匹配激活插件，按优先级降序返回（上限 max_active）。"""
        if not self.enabled:
            return []
        self.refresh()
        candidates = list(self._plugins.values())
        if self.whitelist:
            candidates = [p for p in candidates if p.name in self.whitelist]
            if self.decision_logger is not None and candidates:
                self.decision_logger.record(
                    "plugin.whitelist", "active_plugins", sorted(self.whitelist),
                    f"按白名单过滤后剩余插件: {[p.name for p in candidates]}",
                )
        matched: List[tuple[Plugin, List[str]]] = []
        for p in candidates:
            hit = match_triggers(p.triggers, instruction, files or [], deps or [])
            if hit:
                matched.append((p, hit))
        matched.sort(key=lambda t: (-t[0].priority, t[0].name))
        active = matched[: self.max_active]
        for p, hit in active:
            if self.decision_logger is not None:
                self.decision_logger.record(
                    "plugin.activate", "plugin.enabled", True,
                    f"激活插件 {p.name}（命中: {','.join(hit)}，priority={p.priority}）",
                )
        if len(matched) > self.max_active and self.decision_logger is not None:
            dropped = [p.name for p, _ in matched[self.max_active:]]
            self.decision_logger.record(
                "plugin.truncate", "plugin.max_active", self.max_active,
                f"命中 {len(matched)} 个插件，超过上限截断，丢弃: {dropped}",
            )
        return [p for p, _ in active]

    @staticmethod
    def to_context(plugins: List[Plugin]) -> str:
        """把激活插件渲染为注入 Prompt 的上下文文本。"""
        return "\n\n".join(p.render() for p in plugins)