"""阶段六 6.4：连接编译后的 TypeScript MCP 服务器（stdio）并验证工具/资源。

前置：mcp-servers/{knowledge-base,issue-tracker} 已 npm install && npm run build
（dist 缺失时自动跳过）。数据文件通过环境变量指向临时目录，避免污染仓库。
"""
import json
import sys
from pathlib import Path

import pytest

from agent.config import MCPClientConfig
from agent.mcp.client import MCPClient

ROOT = Path(__file__).resolve().parent.parent
KB_DIST = ROOT / "mcp-servers" / "knowledge-base" / "dist" / "index.js"
IT_DIST = ROOT / "mcp-servers" / "issue-tracker" / "dist" / "index.js"

NEEDS_BUILD = "先运行: cd mcp-servers/* && npm install && npm run build"


def kb_client():
    return MCPClient(
        MCPClientConfig(name="kb", transport="stdio", command="node",
                        args=[str(KB_DIST)]),
        tool_timeout=10.0,
    )


def it_client(data_file):
    return MCPClient(
        MCPClientConfig(name="issue", transport="stdio", command="node",
                        args=[str(IT_DIST)],
                        env={"ISSUE_DATA_FILE": str(data_file)}),
        tool_timeout=10.0,
    )


@pytest.mark.skipif(not KB_DIST.exists(), reason=NEEDS_BUILD)
@pytest.mark.asyncio
async def test_knowledge_base_tools_and_resources(ws_tmp):
    client = kb_client()
    try:
        assert await client.connect() is True
        tools = await client.list_tools()
        names = [t["name"] for t in tools]
        assert "search_kb" in names and "add_kb_entry" in names
        # 种子数据包含 “pytest” 关键词
        res = await client.call_tool("search_kb", {"query": "PEP8"})
        assert res.success and "Python 风格" in res.output
        resources = await client.list_resources()
        assert any("kb://" in r["uri"] for r in resources)
        topics = await client.read_resource("kb://topics")
        assert "构建约定" in topics
    finally:
        await client.close()


@pytest.mark.skipif(not IT_DIST.exists(), reason=NEEDS_BUILD)
@pytest.mark.asyncio
async def test_issue_tracker_workflow(ws_tmp):
    data_file = ws_tmp / "issues.json"
    # 种子数据
    data_file.write_text(json.dumps([
        {"id": "1", "title": "修复登录页超时", "description": "需要加缓存。",
         "status": "open", "labels": ["bug"]},
    ], ensure_ascii=False), encoding="utf-8")
    client = it_client(data_file)
    try:
        assert await client.connect() is True
        r = await client.call_tool("list_issues", {"status": "open"})
        assert r.success and "登录页超时" in r.output
        r2 = await client.call_tool(
            "create_issue", {"title": "新功能", "description": "导出 CSV"})
        assert r2.success and "已创建" in r2.output
        r3 = await client.call_tool(
            "update_issue_status", {"id": "2", "status": "done"})
        assert r3.success and "已更新" in r3.output
        # 资源：issue://open 应包含未关闭项（#1），不含 #2（done）
        open_issues = await client.read_resource("issue://open")
        assert "#1" in open_issues and "#2" not in open_issues
        # 数据写入的是临时文件，仓库内 data/issues.json 未被污染
        saved = json.loads(data_file.read_text(encoding="utf-8"))
        assert len(saved) == 2 and saved[1]["status"] == "done"
    finally:
        await client.close()
