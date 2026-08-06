"""CLI 入口：python -m tui "任务提示词"

示例：
    python -m tui "写一个 hello.py 并运行"
    python -m tui --config config/agent.yaml "修复测试失败"
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
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
