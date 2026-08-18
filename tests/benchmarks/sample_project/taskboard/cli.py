"""命令行入口：taskboard add / list / complete / delete。"""
from __future__ import annotations

import argparse
import json
import sys

from taskboard.board import Board
from taskboard.models import Priority


def _format_task(t) -> str:
    tags = ",".join(t.tags) if t.tags else "-"
    return (f"[{t.status.value:>10}] {t.title}  "
            f"(prio={t.priority.value}, est={t.total_estimate}m, tags={tags})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taskboard", description="任务看板 CLI")
    parser.add_argument("--db", default="tasks.json", help="数据文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="新增任务")
    p_add.add_argument("title", help="任务标题")
    p_add.add_argument("--priority", default="medium", choices=[p.value for p in Priority])
    p_add.add_argument("--tags", default="", help="逗号分隔标签")

    sub.add_parser("list", help="列出全部任务")

    p_done = sub.add_parser("complete", help="完成任务")
    p_done.add_argument("id", help="任务 id")
    p_done.add_argument("--spent", type=int, default=0, help="实际耗时（分钟）")

    p_del = sub.add_parser("delete", help="删除任务")
    p_del.add_argument("id", help="任务 id")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    board = Board(path=args.db)
    if args.command == "add":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        task = board.add(args.title, priority=args.priority, tags=tags)
        print(json.dumps(task.to_dict(), ensure_ascii=False))
    elif args.command == "list":
        for t in board.all():
            print(_format_task(t))
    elif args.command == "complete":
        ok = board.complete(args.id, spent=args.spent)
        if not ok:
            print(f"任务不存在: {args.id}", file=sys.stderr)
            return 1
        print("completed")
    elif args.command == "delete":
        ok = board.delete(args.id)
        if not ok:
            print(f"任务不存在: {args.id}", file=sys.stderr)
            return 1
        print("deleted")
    else:  # pragma: no cover
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
