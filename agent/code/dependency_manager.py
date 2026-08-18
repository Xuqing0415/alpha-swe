# -*- coding: utf-8 -*-
"""依赖清单识别与审计（方向二阶段二 2.5）。

- ``detect_manifests()``：扫描项目根，识别依赖清单文件（Maven/Gradle/Go/Rust/
  CMake/vcpkg/npm/pip）；
- ``parse_manifest()``：把单个清单解析为 ``DepEntry`` 列表（名称 + 版本/约束）；
- ``audit_command()``：返回对应生态的依赖审计/更新命令（是否安装由调用方决定）。

全部使用标准库（xml/json/re），pyproject.toml 用 tomllib（Python 3.11+，
不可用时回退正则）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

# 清单文件名 -> (语言, 生态)
MANIFESTS: Dict[str, tuple] = {
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "build.gradle.kts": ("java", "gradle"),
    "go.mod": ("go", "go"),
    "Cargo.toml": ("rust", "cargo"),
    "CMakeLists.txt": ("cpp", "cmake"),
    "vcpkg.json": ("cpp", "vcpkg"),
    "package.json": ("javascript", "npm"),
    "requirements.txt": ("python", "pip"),
    "pyproject.toml": ("python", "pip"),
    "Pipfile": ("python", "pip"),
}

# 审计/更新命令（按生态）
AUDIT_COMMANDS: Dict[str, List[str]] = {
    "pip": ["pip-audit"],
    "npm": ["npm", "audit"],
    "maven": ["mvn", "versions:display-dependency-updates"],
    "gradle": ["gradle", "dependencyUpdates"],
    "cargo": ["cargo", "audit"],
    "go": ["go", "list", "-m", "-u", "all"],
    "cmake": ["conan", "list"],
    "vcpkg": ["vcpkg", "update"],
}


@dataclass
class DepEntry:
    """单个依赖项。"""
    name: str
    version: str = ""          # 固定版本或约束原文
    kind: str = ""             # runtime / dev / test / build
    manager: str = ""          # maven / gradle / go / cargo / npm / pip ...

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "kind": self.kind, "manager": self.manager}


@dataclass
class ManifestInfo:
    """一个依赖清单文件及其解析结果。"""
    path: str
    language: str
    manager: str
    entries: List[DepEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "language": self.language,
                "manager": self.manager,
                "deps": [e.to_dict() for e in self.entries]}


def detect_manifests(root) -> List[ManifestInfo]:
    """扫描项目目录（浅层 + 常见子目录），返回清单列表。"""
    root_p = Path(root)
    out: List[ManifestInfo] = []
    if not root_p.is_dir():
        return out
    for rel, (lang, mgr) in MANIFESTS.items():
        candidates = [root_p / rel]
        if rel == "requirements.txt":
            candidates.append(root_p / "requirements-dev.txt")
        for cand in candidates:
            if cand.is_file():
                try:
                    info = parse_manifest(str(cand))
                    out.append(info)
                except Exception:
                    out.append(ManifestInfo(str(cand), lang, mgr))
    # 按路径排序，保证确定性
    return sorted(out, key=lambda m: m.path)


def parse_manifest(path) -> ManifestInfo:
    """解析单个清单文件为 ManifestInfo。"""
    p = Path(path)
    name = p.name
    if name not in MANIFESTS:
        raise ValueError(f"未知清单类型: {name}")
    lang, mgr = MANIFESTS[name]
    text = p.read_text(encoding="utf-8", errors="ignore")
    entries: List[DepEntry] = []
    if name == "pom.xml":
        entries = _parse_pom(text)
    elif name.startswith("build.gradle"):
        entries = _parse_gradle(text)
    elif name == "go.mod":
        entries = _parse_gomod(text)
    elif name == "Cargo.toml":
        entries = _parse_cargo(text)
    elif name == "CMakeLists.txt":
        entries = _parse_cmake(text)
    elif name == "vcpkg.json":
        entries = _parse_vcpkg(text)
    elif name == "package.json":
        entries = _parse_package_json(text)
    elif name == "pyproject.toml":
        entries = _parse_pyproject(text)
    elif name in ("requirements.txt", "requirements-dev.txt"):
        entries = _parse_requirements(text)
    elif name == "Pipfile":
        entries = _parse_pipfile(text)
    return ManifestInfo(str(p), lang, mgr, entries)


def _parse_pom(text: str) -> List[DepEntry]:
    import xml.etree.ElementTree as ET
    out: List[DepEntry] = []
    try:
        root = ET.fromstring(text)
        # 兼容带/不带 Maven 命名空间的 pom.xml
        for dep in root.iter():
            if dep.tag.rsplit("}", 1)[-1] != "dependency":
                continue
            g = _pom_child(dep, "groupId")
            a = _pom_child(dep, "artifactId")
            v = _pom_child(dep, "version")
            scope = _pom_child(dep, "scope")
            if a:
                out.append(DepEntry(
                    name=f"{g}:{a}" if g else a, version=v or "",
                    kind=scope or "runtime", manager="maven"))
    except Exception:
        pass
    return out


def _pom_child(node, tag: str) -> str:
    """按本地标签名取子元素文本（兼容命名空间）。"""
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == tag:
            return (child.text or "").strip()
    return ""


def _parse_gradle(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    for m in re.finditer(
            r"(?:implementation|api|compile|runtimeOnly|testImplementation|"
            r"testCompileOnly|annotationProcessor)\s+['\"]([\w.\-]+):"
            r"([\w.\-]+):([\w.\-+]+)['\"]", text):
        out.append(DepEntry(
            name=f"{m.group(1)}:{m.group(2)}", version=m.group(3),
            manager="gradle"))
    return out


def _parse_gomod(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("require "):
            line = line[len("require "):].strip()
        elif not in_block:
            continue
        m = re.match(r"([\w.\-/_]+)\s+(v[\w.+\-]+)", line)
        if m:
            out.append(DepEntry(name=m.group(1), version=m.group(2),
                                manager="go"))
    return out


def _parse_cargo(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        if section in ("dependencies", "dev-dependencies",
                       "build-dependencies"):
            m = re.match(r"([\w\-]+)\s*=\s*['\"]([^'\"]+)['\"]", line)
            if m:
                kind = "dev" if "dev" in section else (
                    "build" if "build" in section else "runtime")
                out.append(DepEntry(name=m.group(1), version=m.group(2),
                                    kind=kind, manager="cargo"))
                continue
            m2 = re.match(r"([\w\-]+)\s*=\s*\{\s*version\s*=\s*"
                          r"['\"]([^'\"]+)['\"]", line)
            if m2:
                out.append(DepEntry(name=m2.group(1), version=m2.group(2),
                                    kind=kind, manager="cargo"))
    return out


def _parse_cmake(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    for m in re.finditer(
            r"find_package\s*\(\s*([\w.\-]+)", text):
        out.append(DepEntry(name=m.group(1), manager="cmake"))
    seen = {e.name for e in out}
    for m in re.finditer(r"add_subdirectory\s*\(\s*([\w.\-]+)", text):
        if m.group(1) not in seen:
            out.append(DepEntry(name=m.group(1) + " (subdir)",
                                manager="cmake"))
    return out


def _parse_vcpkg(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    try:
        data = json.loads(text)
        for d in data.get("dependencies", []):
            if isinstance(d, str):
                out.append(DepEntry(name=d, manager="vcpkg"))
            elif isinstance(d, dict) and d.get("name"):
                out.append(DepEntry(
                    name=d["name"],
                    version=str(d.get("version>=") or ""),
                    manager="vcpkg"))
    except Exception:
        pass
    return out


def _parse_package_json(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    try:
        data = json.loads(text)
        for kind, key in (("runtime", "dependencies"),
                          ("dev", "devDependencies")):
            for name, ver in (data.get(key) or {}).items():
                out.append(DepEntry(name=name, version=str(ver),
                                    kind=kind, manager="npm"))
    except Exception:
        pass
    return out


def _parse_pyproject(text: str) -> List[DepEntry]:
    # 优先 tomllib；失败回退正则
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        data = None
    if data is not None:
        out: List[DepEntry] = []
        deps = (data.get("project") or {}).get("dependencies") or []
        for d in deps:
            out.append(_pip_entry(str(d)))
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies")
        for name, spec in (poetry or {}).items():
            if name == "python":
                continue
            ver = spec if isinstance(spec, str) else str(
                (spec or {}).get("version", ""))
            out.append(DepEntry(name=name, version=ver, manager="pip"))
        return out
    # 正则回退：[project] dependencies = ["a>=1.0", ...]
    out = []
    for m in re.finditer(r"[\s,]*['\"]([^'\"]+)['\"]", text):
        if "=" in m.group(1) or m.group(1).isalnum():
            out.append(_pip_entry(m.group(1)))
    return out


def _parse_requirements(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        out.append(_pip_entry(line))
    return out


def _parse_pipfile(text: str) -> List[DepEntry]:
    out: List[DepEntry] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([\w.\-]+)\s*=\s*['\"]([^'\"]+)['\"]", line)
        if m and "packages" in section:
            out.append(DepEntry(name=m.group(1), version=m.group(2),
                                manager="pip"))
    return out


def _pip_entry(spec: str) -> DepEntry:
    name = spec.split(";")[0].strip()
    m = re.match(r"([A-Za-z0-9._\-]+)\s*([<>=!~]=?[^\s,]+)?", name)
    if m:
        return DepEntry(name=m.group(1), version=m.group(2) or "",
                        manager="pip")
    return DepEntry(name=name, manager="pip")


def audit_command(manager: str):
    """返回对应生态的审计命令列表；未知生态返回 None。"""
    return AUDIT_COMMANDS.get(manager)


def dependency_report(root) -> Dict[str, object]:
    """一次性产出依赖总览，便于注入 Prompt 或落盘。"""
    manifests = detect_manifests(root)
    total = sum(len(m.entries) for m in manifests)
    lines = [f"[依赖清单] {len(manifests)} 个清单，{total} 个依赖"]
    for m in manifests:
        names = ", ".join(e.name for e in m.entries[:20])
        if len(m.entries) > 20:
            names += " …（共 %d 项）" % len(m.entries)
        lines.append(f"- {m.path}（{m.language}/{m.manager}）: "
                     f"{names or '0 项'}")
    return {
        "manifests": [m.to_dict() for m in manifests],
        "total_deps": total,
        "text": "\n".join(lines),
    }
