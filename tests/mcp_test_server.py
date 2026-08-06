"""测试用 MCP stdio 服务器（mcp SDK 2.x MCPServer API）。

提供两个工具（add / echo）与一个资源（memory://facts），
供 tests/test_mcp_client.py 与 tests/test_loop_mcp.py 使用。
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="test-server", instructions="alpha-swe 测试服务器")


@server.tool(name="add", description="两个整数相加")
def add(a: int, b: int) -> int:
    return a + b


@server.tool(name="echo", description="原样返回输入文本")
def echo(text: str) -> str:
    return text


@server.resource("memory://facts", name="facts",
                 description="Alpha-SWE 测试知识")
def facts() -> str:
    return "Alpha-SWE 测试知识: 用 pytest 写测试。"


if __name__ == "__main__":
    server.run(transport="stdio")