"""SkillAuthor —— 从执行轨迹/自然语言创建技能（阶段二 2.3）。

把最近的执行轨迹转化为技能定义文件：
- from_trajectory()：确定性转换（无需 LLM），步骤顺序依赖，
  失败步骤自动标 on_failure=fallback 并带重试指令；
- from_llm()：用 LLM 生成更规范/带条件的技能 YAML，失败时回退确定性转换；
- save()：落盘到技能库目录并热加载校验（可被 SkillLibrary 立即发现）。

支持"把刚才的步骤保存为技能"：loop/session 提供已完成 Task 列表即可。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from agent.context.skill import Skill, SkillLibrary, SkillParam, SkillStep
from agent.core.task import Task

logger = logging.getLogger("alpha-swe.context.skill_author")

STEP_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
TrajectoryItem = Tuple[str, str, str]  # (step_name, instruction, outcome)


def _default_triggers(name: str, description: str) -> Dict[str, List[str]]:
    """从技能名与描述提取关键词触发（拆词，中文按字/词保留）。"""
    keywords: List[str] = []
    for text in (name, description):
        if not text:
            continue
        for tok in re.split(r"[\s,，。；;：:()（）\-_/]+", text):
            tok = tok.strip().lower()
            if not tok or len(tok) > 12:
                continue
            if tok not in keywords:
                keywords.append(tok)
        # 中文长描述：按 2-4 字窗口补充中文关键词
        for i in range(len(text)):
            if "\u4e00" <= text[i] <= "\u9fff":
                for j in (2, 3):
                    if i + j <= len(text):
                        w = text[i:i + j]
                        if w not in keywords and len(w) >= 2:
                            keywords.append(w)
    return {"keywords": keywords[:12]}


def _yaml_text(skill: Skill) -> str:
    """Skill -> YAML 文本（注册表字段 + 步骤），供落盘与 LLM 校验。"""
    params: Dict[str, Dict[str, Any]] = {}
    for p in skill.params:
        params[p.name] = {"type": p.type, "required": p.required,
                          "description": p.description}
        if p.default is not None:
            params[p.name]["default"] = p.default
    data: Dict[str, Any] = {
        "name": skill.name,
        "version": skill.version,
        "description": skill.description,
        "priority": skill.priority,
        "requires": list(skill.requires),
        "permissions": list(skill.permissions),
        "tags": list(skill.tags),
        "author": skill.author or "alpha-swe",
        "params": params,
        "triggers": {k: list(v) for k, v in skill.triggers.items()},
        "steps": [],
    }
    for step in skill.steps:
        s: Dict[str, Any] = {"name": step.name, "instruction": step.instruction}
        if step.dependencies:
            s["dependencies"] = list(step.dependencies)
        if step.on_failure and step.on_failure != "abort":
            s["on_failure"] = step.on_failure
        if step.fallback:
            s["fallback"] = step.fallback
        if step.when:
            s["when"] = dict(step.when)
        data["steps"].append(s)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


class SkillAuthor:
    """从轨迹/自然语言创建技能并落盘热加载。"""

    def __init__(self, skills_dir: str = "./skills/workflows",
                 registry_file: str = "./skills/skill_manifest.json",
                 llm=None, decision_logger=None):
        self.skills_dir = skills_dir
        self.registry_file = registry_file
        self.llm = llm
        self.decision_logger = decision_logger

    # ---- 确定性：轨迹 -> 技能 ----
    def from_trajectory(self, name: str, description: str,
                        trajectory: Sequence[TrajectoryItem],
                        triggers: Optional[Dict[str, List[str]]] = None,
                        priority: int = 5,
                        version: str = "0.1.0") -> Skill:
        """轨迹 -> 技能：顺序依赖；失败步骤标 fallback 重试。"""
        steps: List[SkillStep] = []
        for i, item in enumerate(trajectory):
            step_name, instruction, outcome = (list(item) + ["completed"])[:3]
            step = SkillStep(
                name=step_name,
                instruction=instruction,
                dependencies=[trajectory[i - 1][0]] if i > 0 else [],
            )
            if outcome and outcome != "completed":
                step.on_failure = "fallback"
                step.fallback = f"根据失败信息调整后重试（原步骤: {instruction[:60]}）"
            steps.append(step)
        skill = Skill(
            name=name,
            description=description,
            version=version,
            priority=priority,
            triggers=triggers or _default_triggers(name, description),
            steps=steps,
        )
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill.authored", "skills.registry_file", name,
                f"从 {len(trajectory)} 步轨迹创建技能 {name}（确定性转换）",
            )
        return skill

    # ---- LLM：自然语言 -> 技能 YAML ----
    async def from_llm(self, name: str, description: str,
                       prompt: str,
                       trajectory: Optional[Sequence[TrajectoryItem]] = None,
                       priority: int = 5) -> Skill:
        """用 LLM 把自然语言/轨迹转化为技能；失败回退确定性转换。"""
        fallback = self.from_trajectory(
            name, description, trajectory or [], priority=priority)
        if self.llm is None or (not trajectory and not prompt):
            return fallback
        traj_lines = "\n".join(
            f"- {s}: {i}（{o}）" for s, i, o in (trajectory or []))
        system = ("你是技能作者。把用户描述的执行步骤转化为一个技能 YAML 定义，"
                  "包含 name/version/description/priority/triggers/steps。"
                  "每个 step 含 name 与 instruction；步骤按顺序依赖；"
                  "关键步骤可加 when 条件（file_exists/keyword/project_dep）。"
                  "只输出 YAML 代码块，不要输出其他内容。")
        user = (f"技能名: {name}\n描述: {description}\n任务: {prompt}\n"
                f"参考轨迹:\n{traj_lines or '（无）'}")
        try:
            raw = await self.llm.complete([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            skill = self._parse_llm_yaml(raw, name, description, priority)
            if skill is not None:
                if self.decision_logger is not None:
                    self.decision_logger.record(
                        "skill.authored", "skills.registry_file", name,
                        f"LLM 生成技能 {name}（{len(skill.steps)} 步）",
                    )
                return skill
        except Exception as e:
            logger.warning("LLM 生成技能失败，回退轨迹转换: %s", e)
        return fallback

    def _parse_llm_yaml(self, raw: str, name: str, description: str,
                        priority: int) -> Optional[Skill]:
        """从 LLM 输出中提取 YAML 代码块并解析为 Skill。"""
        m = re.search(r"```(?:ya?ml)?\s*([\s\S]*?)```", raw)
        text = m.group(1) if m else raw
        data = yaml.safe_load(text)
        if not isinstance(data, dict) or not data.get("steps"):
            return None
        steps = []
        for item in data.get("steps") or []:
            if not isinstance(item, dict):
                continue
            steps.append(SkillStep(
                name=str(item.get("name", "")).strip(),
                instruction=str(item.get("instruction", "")).strip(),
                dependencies=[str(x) for x in (item.get("dependencies") or [])],
                on_failure=str(item.get("on_failure", "abort")).strip() or "abort",
                fallback=str(item.get("fallback", "")).strip(),
                when=dict(item.get("when") or {}),
            ))
        steps = [s for s in steps if s.name and s.instruction]
        if not steps:
            return None
        return Skill(
            name=str(data.get("name") or name).strip(),
            description=str(data.get("description") or description),
            version=str(data.get("version", "0.1.0")),
            priority=int(data.get("priority", priority)),
            triggers={
                k: [str(x) for x in (v if isinstance(v, list) else [v])]
                for k, v in (data.get("triggers") or {}).items()
            } or _default_triggers(name, description),
            steps=steps,
            permissions=[str(x) for x in (data.get("permissions") or [])],
            requires=[str(x) for x in (data.get("requires") or [])],
        )

    # ---- 落盘 + 热加载校验 ----
    def save(self, skill: Skill) -> Path:
        """写 YAML 到技能库目录，并用 SkillLibrary 校验可加载。"""
        path = Path(self.skills_dir) / f"{skill.name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_yaml_text(skill), encoding="utf-8")
        lib = SkillLibrary(skills_dir=self.skills_dir,
                           registry_file=self.registry_file, enabled=True)
        loaded = lib.get(skill.name)
        issues = lib.validate().get(skill.name, [])
        if loaded is None or issues:
            logger.error("技能落盘后校验失败 %s: %s", path, issues)
            raise ValueError(f"技能校验失败: {issues}")
        if self.decision_logger is not None:
            self.decision_logger.record(
                "skill.saved", "skills.registry_file", skill.name,
                f"技能落盘并热加载: {path}",
            )
        return path

    # ---- 便捷：Task 列表 -> 轨迹 ----
    @staticmethod
    def trajectory_from_tasks(tasks: Sequence[Task]) -> List[TrajectoryItem]:
        """从已完成/失败的任务（含 skill_step 元数据）提取轨迹。"""
        out: List[TrajectoryItem] = []
        for t in tasks:
            step = t.metadata.get("skill_step") if t.metadata else None
            name = step or t.id
            outcome = "completed" if t.status.value == "completed" else "failed"
            out.append((name, t.instruction, outcome))
        return out

    @staticmethod
    def yaml_text(skill: Skill) -> str:
        return _yaml_text(skill)
