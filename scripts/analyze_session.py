#!/usr/bin/env python3
"""会话档案分析工具 —— 长任务事后复盘（收敛期 P1 / 阶段二 2.3）。

输入为 SessionArchive 产出的会话档案（logs/sessions/session_*.json），
输出一份复盘摘要：完成状态 / 轮次 / token / 工具调用成功率 / 重试 /
压缩触发时机 / 修改文件 / 错误清单。

用法:
    python -X utf8 scripts/analyze_session.py <session.json>
    python -X utf8 scripts/analyze_session.py logs/sessions/session_*.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

# 允许从任意 cwd 直接运行：python scripts/analyze_session.py <session.json>
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.attribution import classify_session_failure  # noqa: E402
from agent.observability.archive import (  # noqa: E402
    SessionArchive, summarize_session,
)


def _human_summary(summary: dict, attribution=None) -> str:
    """把复盘摘要渲染为人类可读文本。"""
    lines = [
        f"会话: {summary['session_id']}",
        f"结果: {'成功' if summary['ok'] else '失败'}（phase={summary['phase']}）",
        f"轮次: {summary['rounds']} | LLM 调用: {summary['llm_calls']} "
        f"| token: {summary['tokens']}",
        f"工具: {summary['tool_calls']} 次调用，"
        f"成功率 {summary['tool_success_rate'] or '-'}，"
        f"失败 {summary['tool_failures']} 次",
        f"重试: {summary['retries']} 次 | "
        f"压缩: {summary['compressions']} 次"
        f"（首次在第 {summary['compression_first_after_events']} 个决策点后）",
    ]
    tasks = summary["tasks"]
    if tasks["total"]:
        lines.append(
            f"任务: {tasks['total']} 个（完成 {tasks['completed']} / "
            f"失败 {tasks['failed']} / 跳过 {tasks['skipped']}）")
    if summary["files_modified"]:
        lines.append("修改文件:")
        lines.extend(f"  - {p}" for p in summary["files_modified"])
    if summary["errors"]:
        lines.append("错误:")
        lines.extend(f"  - {e}" for e in summary["errors"][:10])
    if summary["final_answer"]:
        lines.append(f"最终答复: {summary['final_answer']}")
    if not summary["ok"] and attribution:
        # 收敛期 P2：失败会话附归因类别与改进建议
        lines.append(f"归因: {attribution['label']}（{attribution['reason']}）")
        lines.append(f"建议: {'；'.join(attribution['suggestions'])}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -X utf8 scripts/analyze_session.py",
        description="长任务会话档案复盘摘要",
    )
    parser.add_argument("path", help="会话档案 JSON 路径")
    parser.add_argument("--json", action="store_true",
                        help="输出原始 JSON 摘要（默认）")
    parser.add_argument("--text", action="store_true",
                        help="输出人类可读文本摘要")
    args = parser.parse_args(argv)

    p = Path(args.path)
    if not p.exists():
        print(f"档案不存在: {p}", file=sys.stderr)
        return 1
    doc = SessionArchive.load(str(p))
    summary = summarize_session(doc)
    attribution = None
    if not summary["ok"]:
        # 收敛期 P2：失败会话附归因类别与改进建议
        attribution = classify_session_failure(doc)
    if args.text or (not args.json and sys.stdout.isatty()):
        print(_human_summary(summary, attribution))
    else:
        if attribution is not None:
            summary["attribution"] = attribution
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
