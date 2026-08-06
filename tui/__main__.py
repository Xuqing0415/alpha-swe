"""CLI 入口：python -m tui "任务提示词"

示例：
    python -m tui "写一个 hello.py 并运行"
    python -m tui --config config/agent.yaml "修复测试失败"
    python -m tui --replay logs/sessions/session_xxx.json "按时间线回放会话"
"""
from __future__ import annotations

import argparse
import logging
import sys

from agent.config import load_config, load_mcp_config
from agent.mcp.manager import MCPManager

from tui.app import AlphaSWEApp


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tui", description="Alpha-SWE Textual TUI")
    parser.add_argument("prompt", nargs="*", help="要交给 Agent 的任务提示词")
    parser.add_argument("--config", default=None, help="agent.yaml 路径")
    parser.add_argument("--replay", default=None,
                        help="回放一个会话档案 JSON（阶段七 7.3）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出调试日志")
    args = parser.parse_args(argv)

    if args.replay:
        return replay_session(args.replay)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    prompt = " ".join(args.prompt).strip() or "分析当前项目结构并给出改进建议"
    config = load_config(args.config)
    mcp_manager = MCPManager.from_config(load_mcp_config(), config.mcp)

    app = AlphaSWEApp(prompt, config=config, mcp_manager=mcp_manager)
    app.run()
    return 0


def replay_session(path: str) -> int:
    """按时间线逐步打印会话档案（事件 / span / 决策合并排序）。"""
    from agent.observability import SessionReplay

    replay = SessionReplay.load(path)
    arch = replay.archive
    print("=" * 68)
    print(f"会话档案回放: {path}")
    print(f"session={arch.get('session_id')}  created={arch.get('created_at')}")
    print(f"prompt={arch.get('prompt', '')[:120]}")
    print(f"events={len(arch.get('events', []))}  "
          f"spans={len(arch.get('spans', []))}  "
          f"decisions={len(arch.get('decisions', []))}")
    print("=" * 68)
    rows = replay.timeline()
    if not rows:
        print("（档案为空）")
        return 0
    for i, row in enumerate(rows):
        label = row["label"]
        if row["kind"] == "event":
            payload = row["payload"]
            detail = payload.get("data", {})
            extra = ""
            if payload.get("type") == "tool_call":
                extra = f" {detail.get('tool', '')}"
            elif payload.get("type") == "think":
                extra = f" {str(detail.get('content', ''))[:60]}"
            print(f"[{i:03d}] {row['kind']:8s} {label}{extra}")
        else:
            print(f"[{i:03d}] {row['kind']:8s} {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
