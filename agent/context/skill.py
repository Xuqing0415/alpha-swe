"""技能引擎 —— YAML 工作流化（对应设计第 10.2 节）。

技能 = 预定义子任务序列（步骤可声明依赖）；
技能内嵌决策点：on_failure = abort | fallback | orchestrate，fallback 携带回退指令；
SkillLibrary 从 YAML 技能库热加载，expand() 展开为 Task DAG 交给 Scheduler。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from agent.context.plugin import match_triggers
from agent.core.task import Task

logger = logging.getLogger("alpha-swe.context.skill")

STEP_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


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
class Skill:
    """预定义工作流：触发器 + 步骤序列。"""
    name: str
    description: str = ""
    version: str = "0.0.0"
    priority: int = 1
    triggers: Dict[str, List[str]] = field(default_factory=dict)
    steps: List[SkillStep] = field(default_factory=list)
    source: str = ""

    def to_context(self) -> str:
        seq = " -> ".join(s.name for s in self.steps)
        head = f"[skill:{self.name} v{self.version}]"
        if self.description:
            head += f" {self.description}"
        return f"{head}\n步骤序列: {seq}"


class SkillLibrary:
    """YAML 技能库：目录扫描 + mtime 热加载 + 匹配 + 展开 DAG。"""

    def __init__(self, skills_dir: str = "./skills/workflows",
                 whitelist: Optional[List[str]] = None,
                 max_active: int = 3,
                 enabled: bool = True,
                 decision_logger=None):
        self.skills_dir = skills_dir
        self.whitelist = set(whitelist or [])
        self.max_active = max(1, max_active)
        self.enabled = enabled
        self.decision_logger = decision_logger
        self._skills: Dict[str, Skill] = {}
        self._mtime: Dict[str, float] = {}
        self.refresh()

    def refresh(self) -> None:
        """热加载：重扫技能库，仅重读变更文件（新增/修改立即生效）。"""
        if not self.enabled:
            return
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
            skill = Skill(
                name=name,
                description=str(data.get("description", "")),
                version=str(data.get("version", "0.0.0")),
                priority=int(data.get("priority", 1)),
                triggers={
                    k: [str(x) for x in (v if isinstance(v, list) else [v])]
                    for k, v in (data.get("triggers") or {}).items()
                },
                steps=steps,
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
                    "skill_step": step.name,
                    "on_failure": step.on_failure,
                    "fallback": step.fallback,
                },
            )
        for step in skill.steps:
            tasks[step.name].dependencies = [
                tasks[dep].id for dep in step.dependencies if dep in tasks
            ]
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill.expand", "skills.workflow_enabled", True,
                f"技能 {skill.name} 展开为 {len(tasks)} 个子任务: "
                f"{[t.id for t in tasks.values()]}",
            )
        return list(tasks.values())

    @staticmethod
    def to_context(skills: List[Skill]) -> str:
        return "\n\n".join(s.to_context() for s in skills)