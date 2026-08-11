#!/usr/bin/env python3
"""技能作者 CLI —— 把执行轨迹/自然语言保存为技能（阶段二 2.3）。

用法:
    python -X utf8 scripts/save_skill.py --name fix-sql-injection --description "修复 SQL 注入"
        --trajectory examples/trajectory.json [--skills-dir ./skills/workflows]
        [--llm-prompt "把最近修复 SQL 注入的步骤保存为技能"]

trajectory.json: [{"step": "analyze", "instruction": "...", "outcome": "completed"}, ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.context.skill_author import SkillAuthor  # noqa: E402


def load_trajectory(path: str) -> List[Tuple[str, str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for item in data:
        out.append((str(item.get("step", "")), str(item.get("instruction", "")),
                    str(item.get("outcome", "completed"))))
    return [x for x in out if x[0] and x[1]]


async def main() -> int:
    ap = argparse.ArgumentParser(description="把执行轨迹保存为技能")
    ap.add_argument("--name", required=True, help="技能名")
    ap.add_argument("--description", required=True, help="技能描述")
    ap.add_argument("--trajectory", default="", help="轨迹 JSON 文件路径")
    ap.add_argument("--llm-prompt", default="",
                    help="自然语言补充说明（配合 llm 生成）")
    ap.add_argument("--skills-dir", default="./skills/workflows",
                    help="技能库目录")
    ap.add_argument("--registry", default="./skills/skill_manifest.json")
    ap.add_argument("--priority", type=int, default=5)
    args = ap.parse_args()

    trajectory = load_trajectory(args.trajectory) if args.trajectory else []
    if not trajectory and not args.llm_prompt:
        print("错误: 需要 --trajectory 或 --llm-prompt 之一", file=sys.stderr)
        return 2
    author = SkillAuthor(skills_dir=args.skills_dir,
                         registry_file=args.registry)
    if args.llm_prompt:
        from agent.llm import build_llm
        llm = build_llm()
        author.llm = llm
        skill = await author.from_llm(args.name, args.description,
                                      args.llm_prompt, trajectory,
                                      priority=args.priority)
    else:
        skill = author.from_trajectory(args.name, args.description,
                                       trajectory, priority=args.priority)
    path = author.save(skill)
    print(f"技能已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
