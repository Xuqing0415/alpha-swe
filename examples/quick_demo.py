"""快速演示：脚本化 LLM 驱动一次完整 ReAct（terminal -> final_answer）。

运行: python -X utf8 examples/quick_demo.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import AppConfig, MCPOptions, SandboxConfig
from agent.core.loop import AgentLoop
from agent.llm import MockLLM


class DemoLLM(MockLLM):
    """第一次调用返回工具调用，第二次收敛为最终回答。"""

    def __init__(self):
        self.n = 0

    async def complete(self, messages):
        self.n += 1
        if self.n == 1:
            return '{"tool": "terminal_execute", "params": {"command": "echo hello"}}'
        return '{"final_answer": "命令已执行"}'


async def main():
    loop = AgentLoop(
        # 演示为脚本化 LLM 驱动，无需连接 MCP 服务器（避免未启动服务的连接噪音）
        config=AppConfig(sandbox=SandboxConfig(workspace="./workspace"),
                         mcp=MCPOptions(enabled=False)),
        llm=DemoLLM(),
    )
    try:
        result = await loop.run("演示：执行一条命令")
        print("阶段:", result.phase.value)
        print("最终回答:", result.final_answer)
        print("事件数:", len(result.events))
    finally:
        # 释放 MCP 子进程与沙箱等资源；不关闭会导致 asyncio.run 收尾挂起
        await loop.close()


if __name__ == "__main__":
    asyncio.run(main())