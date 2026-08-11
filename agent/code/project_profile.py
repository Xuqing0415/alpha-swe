"""项目约定与技术栈自动提取（阶段一 1.3）。

首次进入项目时扫描配置文件与依赖清单，生成"项目约定摘要"：
- 技术栈（React/Express/FastAPI 等，含大版本号）；
- 约定（tsconfig strict、lint/格式化工具、测试框架、Python 版本）；
- 顶层目录结构。
结果作为持久化上下文注入后续所有 Prompt，避免 Agent 使用过时/不适配的 API。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger("alpha-swe.code")

# 依赖名 -> 技术栈标签（含版本）
_TECH_RULES = [
    ("react", "React"),
    ("next", "Next.js"),
    ("vue", "Vue"),
    ("express", "Express"),
    ("fastify", "Fastify"),
    ("django", "Django"),
    ("flask", "Flask"),
    ("fastapi", "FastAPI"),
    ("pytest", "pytest"),
    ("jest", "Jest"),
    ("vitest", "Vitest"),
    ("typescript", "TypeScript"),
    ("eslint", "ESLint"),
    ("prettier", "Prettier"),
    ("ruff", "Ruff"),
    ("mypy", "Mypy"),
]
_PY_KNOWN = {"django", "flask", "fastapi", "pytest", "requests", "sqlalchemy",
             "ruff", "black", "mypy", "pydantic", "celery", "click"}

_RE_QUOTED = r'[\"\']'  # 单/双引号字符类
_RE_REQUIRES_PYTHON = re.compile(
    r'requires-python\s*=\s*["\']([^"\']+)["\']'
)


@dataclass
class ProjectProfile:
    """一次扫描得到的项目约定摘要。"""
    root: str
    tech_stack: List[str] = field(default_factory=list)
    conventions: List[str] = field(default_factory=list)
    config_files: List[str] = field(default_factory=list)
    structure: List[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = ["## 项目约定与技术栈"]
        if self.tech_stack:
            lines.append(f"- 技术栈: {', '.join(self.tech_stack)}")
        if self.conventions:
            lines.append("- 约定:")
            for c in self.conventions[:20]:
                lines.append(f"  - {c}")
        if self.config_files:
            lines.append(f"- 配置文件: {', '.join(self.config_files[:15])}")
        if self.structure:
            lines.append(f"- 目录结构: {', '.join(self.structure[:20])}")
        return "\n".join(lines)


def build_profile(root: str,
                  files: Optional[Iterable[str]] = None) -> ProjectProfile:
    """扫描项目文件生成约定摘要；目录不存在返回空 Profile。"""
    root_p = Path(root)
    profile = ProjectProfile(root=str(root_p))
    if not root_p.is_dir():
        return profile
    rel_files = [str(f).replace("\\", "/") for f in (files or [])]
    try:
        _scan_configs(root_p, rel_files, profile)
        profile.structure = _top_structure(root_p, rel_files)
        profile.tech_stack = _dedupe(profile.tech_stack)
        profile.conventions = _dedupe(profile.conventions)
        profile.config_files = _dedupe(profile.config_files)
    except Exception as e:
        logger.warning("项目约定提取失败: %s", e)
    return profile


def _scan_configs(root: Path, files: List[str], profile: ProjectProfile) -> None:
    lower_by_rel = {f.lower(): f for f in files}
    for lower, rel in sorted(lower_by_rel.items()):
        if rel == "package.json":
            profile.config_files.append(rel)
            _package_tech(_read_json(root / rel), profile)
        elif rel == "requirements.txt":
            profile.config_files.append(rel)
            for dep in _read_lines(root / rel):
                if dep in _PY_KNOWN:
                    profile.tech_stack.append(_tech_label(dep))
        elif rel == "pyproject.toml":
            profile.config_files.append(rel)
            _pyproject_tech(root / rel, profile)
        elif rel == "tsconfig.json" or lower == "tsconfig.base.json":
            profile.config_files.append(rel)
            _tsconfig_tech(root / rel, profile)
        elif lower.endswith(".eslintrc") or lower.endswith(".eslintrc.json") \
                or lower.endswith(".eslintrc.js") or lower.endswith(".eslintrc.cjs"):
            profile.config_files.append(rel)
            profile.conventions.append("使用 ESLint 代码规范")
            profile.tech_stack.append("ESLint")
        elif rel in (".prettierrc", ".prettierrc.json", "prettier.config.js",
                     "prettier.config.cjs", ".prettierrc.yaml"):
            profile.config_files.append(rel)
            profile.conventions.append("使用 Prettier 格式化")
        elif rel in ("pytest.ini", "tox.ini", "setup.cfg"):
            profile.config_files.append(rel)
            if "pytest" in _read_text(root / rel):
                profile.tech_stack.append("pytest")
        elif rel == "Dockerfile" or lower.endswith(".dockerfile"):
            profile.config_files.append(rel)
            profile.tech_stack.append("Docker")


def _package_tech(data: Optional[Dict], profile: ProjectProfile) -> None:
    if not data:
        return
    deps: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        deps.update(data.get(section) or {})
    for dep, ver in deps.items():
        name = str(dep).lower()
        base = name.split("/")[-1]
        for kw, label in _TECH_RULES:
            if base == kw or base.startswith(kw + "-"):
                profile.tech_stack.append(_tech_label(label, ver))
                break
    if data.get("packageManager"):
        profile.conventions.append(f"包管理器: {data['packageManager']}")
    if data.get("engines", {}).get("node"):
        profile.conventions.append(f"Node 版本要求: {data['engines']['node']}")


def _tsconfig_tech(path: Path, profile: ProjectProfile) -> None:
    data = _read_json(path)
    if not data:
        return
    opts = data.get("compilerOptions") or {}
    parts = []
    if opts.get("target"):
        parts.append(f"target={opts['target']}")
    if opts.get("strict"):
        parts.append("strict")
    if opts.get("jsx"):
        parts.append(f"jsx={opts['jsx']}")
    if parts:
        profile.conventions.append("tsconfig: " + ", ".join(parts))
        if opts.get("strict"):
            profile.tech_stack.append("TypeScript (strict)")


def _pyproject_tech(path: Path, profile: ProjectProfile) -> None:
    text = _read_text(path)
    m = _RE_REQUIRES_PYTHON.search(text)
    if m:
        profile.conventions.append(f"Python 版本要求: {m.group(1)}")
        profile.tech_stack.append(f"Python {m.group(1)}")
    for dep in _PY_KNOWN:
        pattern = re.compile(
            r"^\s*" + _RE_QUOTED + r"?" + re.escape(dep)
            + _RE_QUOTED + r"?\s*[=~>]", re.MULTILINE)
        in_list = re.search(
            _RE_QUOTED + re.escape(dep) + _RE_QUOTED, text)
        if pattern.search(text) or in_list:
            profile.tech_stack.append(_tech_label(dep))
    if "[tool.ruff]" in text:
        profile.conventions.append("使用 Ruff 代码检查")
        profile.tech_stack.append("Ruff")
    if "[tool.black]" in text:
        profile.conventions.append("使用 Black 格式化")
        profile.tech_stack.append("Black")


def _tech_label(name: str, version: str = "") -> str:
    label = name.replace("-", " ").title()
    label = {"Fastapi": "FastAPI", "Typescript": "TypeScript"}.get(label, label)
    if version:
        major = re.sub(r"[^0-9.]", "", version.split("||")[0].strip("^~><= "))
        m = major.split(".")[0]
        if m:
            return f"{label} {m}"
    return label


def _top_structure(root: Path, files: List[str]) -> List[str]:
    """顶层目录（根目录下的单文件也列出，便于了解布局）。"""
    top: Set[str] = set()
    for f in files:
        parts = f.split("/")
        if not parts[0] or parts[0].startswith("."):
            continue
        candidate = root / parts[0]
        if len(parts) > 1 or candidate.is_dir():
            top.add(parts[0])
    return sorted(top)


def _dedupe(items: List[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _read_json(path: Path) -> Optional[Dict]:
    try:
        return json.loads(_read_text(path))
    except Exception:
        return None


def _read_lines(path: Path) -> List[str]:
    return [
        line.split("#")[0].strip().lower()
        for line in _read_text(path).splitlines()
        if line.split("#")[0].strip()
    ]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""