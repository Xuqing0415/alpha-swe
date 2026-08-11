"""技能引擎 —— YAML 工作流化 + 技能注册表（对应设计第 10.2 节、阶段二 2.1）。

技能 = 预定义子任务序列（步骤可声明依赖）；
技能内嵌决策点：on_failure = abort | fallback | orchestrate，fallback 携带回退指令；
SkillLibrary 从 YAML 技能库热加载，expand() 展开为 Task DAG 交给 Scheduler。

技能注册表（阶段二 2.1）：
- 每个技能可声明 requires（依赖技能）、permissions（所需工具权限）、
  params（参数定义）、version、tags、author；
- registry_file（JSON 注册表）可按技能名补充元数据，YAML 技能文件为事实来源；
- validate() 做结构校验，discover() 做发现（含依赖闭包），
  record_usage() 记录使用/成败历史，支持版本管理。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from agent.context.plugin import match_triggers
from agent.core.task import Task

logger = logging.getLogger("alpha-swe.context.skill")

STEP_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
ON_FAILURE_ALLOWED = {"abort", "fallback", "orchestrate"}


@dataclass
class SkillStep:
    """技能中的单个步骤（工作流节点）。"""
    name: str
    instruction: str
    dependencies: List[str] = field(default_factory=list)  # 前驱步骤名
    on_failure: str = "abort"   # abort | fallback | orchestrate
    fallback: str = ""          # on_failure=fallback 时的回退指令

    def task_id(self, skill_name: str) -> str:
        return f"{skill_name}::{STEP_ID_SAFE.sub('_', self.name)}"


@dataclass
class SkillParam:
    """技能参数定义（供注册表/发现/展开时校验与填充默认值）。"""
    name: str
    type: str = "string"        # string | int | float | bool | list
    required: bool = False
    default: Any = None
    description: str = ""


@dataclass
class Skill:
    """预定义工作流：触发器 + 步骤序列 + 注册表元数据。"""
    name: str
    description: str = ""
    version: str = "0.0.0"
    priority: int = 1
    triggers: Dict[str, List[str]] = field(default_factory=dict)
    steps: List[SkillStep] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)     # 依赖的其他技能
    permissions: List[str] = field(default_factory=list)  # 所需工具权限
    params: List[SkillParam] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    author: str = ""
    source: str = ""

    def to_context(self) -> str:
        seq = " -> ".join(s.name for s in self.steps)
        head = f"[skill:{self.name} v{self.version}]"
        if self.description:
            head += f" {self.description}"
        lines = [head, f"步骤序列: {seq}"]
        if self.requires:
            lines.append(f"依赖技能: {', '.join(self.requires)}")
        if self.permissions:
            lines.append(f"所需权限: {', '.join(self.permissions)}")
        if self.params:
            lines.append("参数: " + ", ".join(
                f"{p.name}:{p.type}" + ("" if p.required else "?")
                for p in self.params))
        return "\n".join(lines)


class SkillLibrary:
    """YAML 技能库：目录扫描 + mtime 热加载 + 注册表合并 + 匹配 + 展开 DAG。"""

    def __init__(self, skills_dir: str = "./skills/workflows",
                 whitelist: Optional[List[str]] = None,
                 max_active: int = 3,
                 enabled: bool = True,
                 decision_logger=None,
                 registry_file: str = "./skills/skill_manifest.json",
                 usage_log: str = "./logs/skill_usage.jsonl"):
        self.skills_dir = skills_dir
        self.whitelist = set(whitelist or [])
        self.max_active = max(1, max_active)
        self.enabled = enabled
        self.decision_logger = decision_logger
        self.registry_file = registry_file
        self.usage_log = usage_log
        self._skills: Dict[str, Skill] = {}
        self._mtime: Dict[str, float] = {}
        self._registry_mtime: float = -1.0
        self._meta: Dict[str, Dict[str, Any]] = {}
        self.refresh()

    def refresh(self) -> None:
        """热加载：重扫技能库与注册表，仅重读变更文件（新增/修改立即生效）。"""
        if not self.enabled:
            return
        self._load_registry()
        d = Path(self.skills_dir)
        if not d.is_dir():
            return
        try:
            files = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
            for p in files:
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if self._mtime.get(str(p)) == mtime:
                    continue
                self._load_file(p)
                self._mtime[str(p)] = mtime
        except OSError as e:
            logger.warning("技能目录扫描失败: %s", e)

    def _load_registry(self) -> None:
        """读取 JSON 注册表（可选），按技能名合并元数据。"""
        p = Path(self.registry_file)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return
        if mtime == self._registry_mtime:
            return
        self._registry_mtime = mtime
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            logger.warning("技能注册表加载失败 %s: %s", p, e)
            return
        self._meta = {}
        for name, meta in (data.get("skills") or {}).items():
            if isinstance(meta, dict):
                self._meta[str(name)] = meta
        if self.decision_logger is not None and self._meta:
            self.decision_logger.record(
                "skill.registry.loaded", "skills.registry_file",
                self.registry_file,
                f"技能注册表加载 {len(self._meta)} 条元数据",
            )

    def _load_file(self, path: Path) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
            if not isinstance(data, dict):
                return
            name = str(data.get("name") or path.stem).strip()
            steps: List[SkillStep] = []
            for item in data.get("steps") or []:
                if not isinstance(item, dict):
                    continue
                steps.append(SkillStep(
                    name=str(item.get("name", "")).strip(),
                    instruction=str(item.get("instruction", "")).strip(),
                    dependencies=[str(x) for x in (item.get("dependencies") or [])],
                    on_failure=str(item.get("on_failure", "abort")).strip() or "abort",
                    fallback=str(item.get("fallback", "")).strip(),
                ))
            steps = [s for s in steps if s.name and s.instruction]
            meta = self._meta.get(name, {})
            skill = Skill(
                name=name,
                description=_pick(data, meta, "description", ""),
                version=_pick(data, meta, "version", "0.0.0"),
                priority=int(data.get("priority", 1)),
                triggers={
                    k: [str(x) for x in (v if isinstance(v, list) else [v])]
                    for k, v in (data.get("triggers") or {}).items()
                },
                steps=steps,
                requires=[str(x) for x in _pick(data, meta, "requires", [])],
                permissions=[str(x) for x in _pick(data, meta, "permissions", [])],
                params=_parse_params(_pick(data, meta, "params", {})),
                tags=[str(x) for x in _pick(data, meta, "tags", [])],
                author=_pick(data, meta, "author", ""),
                source=str(path),
            )
            self._skills[name] = skill
            logger.info("热加载技能: %s（%d 步）", name, len(steps))
        except Exception as e:
            logger.error("技能加载失败 %s: %s", path, e)

    def list_skills(self) -> List[Skill]:
        self.refresh()
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        self.refresh()
        return self._skills.get(name)

    def match(self, instruction: str,
              files: Optional[Iterable[str]] = None,
              deps: Optional[Iterable[str]] = None) -> List[Skill]:
        """按指令 + 项目上下文匹配技能，按优先级降序返回（上限 max_active）。"""
        if not self.enabled:
            return []
        self.refresh()
        candidates = list(self._skills.values())
        if self.whitelist:
            candidates = [s for s in candidates if s.name in self.whitelist]
        matched = [s for s in candidates
                   if match_triggers(s.triggers, instruction, files or [], deps or [])]
        matched.sort(key=lambda s: (-s.priority, s.name))
        return matched[: self.max_active]

    def discover(self, instruction: str,
                 files: Optional[Iterable[str]] = None,
                 deps: Optional[Iterable[str]] = None) -> List[Skill]:
        """技能发现：匹配技能 + requires 依赖闭包（供 Orchestrator 建议使用）。"""
        matched = self.match(instruction, files, deps)
        if not matched:
            return []
        by_name = {s.name: s for s in self.list_skills()}
        out: Dict[str, Skill] = {}
        for s in matched:
            out[s.name] = s
            for dep in s.requires:
                if dep in by_name and dep not in out:
                    out[dep] = by_name[dep]
        result = sorted(out.values(), key=lambda s: (-s.priority, s.name))
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill.discovered", "skills.enabled", True,
                f"发现技能 {len(result)} 个（命中 {[s.name for s in matched]}）",
            )
        return result

    def validate(self) -> Dict[str, List[str]]:
        """结构校验：步骤非空且命名唯一、依赖可解析、on_failure 合法。"""
        self.refresh()
        issues: Dict[str, List[str]] = {}
        all_names = set(self._skills)
        for skill in self._skills.values():
            errs: List[str] = []
            if not skill.steps:
                errs.append("步骤为空")
            names = [s.name for s in skill.steps]
            for n in names:
                if names.count(n) > 1:
                    errs.append(f"步骤名重复: {n}")
            for step in skill.steps:
                if step.on_failure not in ON_FAILURE_ALLOWED:
                    errs.append(f"步骤 {step.name} 的 on_failure 非法: {step.on_failure}")
                for dep in step.dependencies:
                    if dep not in names:
                        errs.append(f"步骤 {step.name} 依赖不存在的步骤: {dep}")
            for req in skill.requires:
                if req not in all_names:
                    errs.append(f"依赖技能不存在: {req}")
            if errs:
                issues[skill.name] = errs
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "skill.registry.invalid", "skills.registry_file",
                        skill.name, f"校验失败: {errs}",
                    )
        return issues

    def record_usage(self, name: str, version: str = "",
                     outcome: str = "activated",
                     note: str = "") -> None:
        """记录技能使用/成败历史（版本管理：辅助"上次用 vX 解决了问题 Y"）。"""
        if not self.usage_log:
            return
        try:
            p = Path(self.usage_log)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "skill": name,
                    "version": version,
                    "outcome": outcome,
                    "note": note,
                    "timestamp": time.time(),
                }, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("技能使用记录失败: %s", e)

    def usage_summary(self) -> Dict[str, Dict[str, int]]:
        """聚合使用记录：{skill: {activated/completed/failed: n}}。"""
        summary: Dict[str, Dict[str, int]] = {}
        p = Path(self.usage_log)
        if not p.exists():
            return summary
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(row.get("skill", ""))
                if not key:
                    continue
                bucket = summary.setdefault(key, {})
                oc = str(row.get("outcome", "activated"))
                bucket[oc] = bucket.get(oc, 0) + 1
        except OSError:
            pass
        return summary

    def expand(self, skill: Skill, prompt: str,
               parent_id: Optional[str] = None) -> List[Task]:
        """把技能展开为 Task DAG：步骤依赖 -> Task 依赖，决策点写入 metadata。"""
        tasks: Dict[str, Task] = {}
        prefix = f"（技能 {skill.name}，原始任务: {str(prompt)[:160]}）"
        for step in skill.steps:
            tid = step.task_id(skill.name)
            tasks[step.name] = Task(
                id=tid,
                instruction=f"{step.instruction} {prefix}",
                priority=skill.priority,
                parent_id=parent_id,
                metadata={
                    "skill": skill.name,
                    "skill_version": skill.version,
                    "skill_step": step.name,
                    "on_failure": step.on_failure,
                    "fallback": step.fallback,
                    "requires": list(skill.requires),
                    "permissions": list(skill.permissions),
                },
            )
        for step in skill.steps:
            tasks[step.name].dependencies = [
                tasks[dep].id for dep in step.dependencies if dep in tasks
            ]
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill.expand", "skills.workflow_enabled", True,
                f"技能 {skill.name} v{skill.version} 展开为 {len(tasks)} 个子任务: "
                f"{[t.id for t in tasks.values()]}",
            )
        return list(tasks.values())

    @staticmethod
    def to_context(skills: List[Skill]) -> str:
        return "\n\n".join(s.to_context() for s in skills)


def _parse_params(data: Any) -> List[SkillParam]:
    """把 {name: {...}} 或 [{name, ...}] 规范化为 SkillParam 列表。"""
    out: List[SkillParam] = []
    if isinstance(data, dict):
        for name, spec in data.items():
            spec = spec if isinstance(spec, dict) else {}
            out.append(SkillParam(
                name=str(name),
                type=str(spec.get("type", "string")),
                required=bool(spec.get("required", False)),
                default=spec.get("default"),
                description=str(spec.get("description", "")),
            ))
    elif isinstance(data, list):
        for spec in data:
            if isinstance(spec, dict) and spec.get("name"):
                out.append(SkillParam(
                    name=str(spec["name"]),
                    type=str(spec.get("type", "string")),
                    required=bool(spec.get("required", False)),
                    default=spec.get("default"),
                    description=str(spec.get("description", "")),
                ))
    return out

def _pick(data: Dict[str, Any], meta: Dict[str, Any],
          key: str, default: Any) -> Any:
    """合并语义：YAML 显式键优先，注册表只补缺省（区分"显式空值"与"未声明"）。"""
    if key in data:
        return data[key]
    if key in meta:
        return meta[key]
    return default
