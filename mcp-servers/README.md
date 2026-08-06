# 自定义 TypeScript MCP 服务器

对应设计 13.3/阶段六 6.3：用 TypeScript 编写自定义 MCP 服务器，接入 Agent 的工具与资源生态。

## knowledge-base —— 团队知识库

- 工具：`search_kb(query, tag?)` 关键词检索；`add_kb_entry(topic, content, tags?)` 新增条目。
- 资源：`kb://topics` 主题列表；`kb://entry/{id}` 条目内容。
- 数据：`data/kb.json`（本地 JSON 持久化，可替换为内部文档源）。

## issue-tracker —— Issue 追踪

- 工具：`list_issues(status?)`；`create_issue(title, description, labels?)`；`update_issue_status(id, status)`。
- 资源：`issue://all` 全部摘要；`issue://open` 未关闭 Issue。
- 数据：`data/issues.json`（可替换为 Jira/GitHub 适配）。

## 构建与接入

```powershell
cd mcp-servers/knowledge-base
npm install
npm run build        # tsc -> dist/index.js

cd ../issue-tracker
npm install
npm run build
```

在 `config/mcp.yaml` 中注册（stdio）：

```yaml
mcp_servers:
  - name: "knowledge-base"
    transport: "stdio"
    command: "node"
    args: ["mcp-servers/knowledge-base/dist/index.js"]
  - name: "issue-tracker"
    transport: "stdio"
    command: "node"
    args: ["mcp-servers/issue-tracker/dist/index.js"]
```

Agent 启动后通过 `ensure_connected` 握手发现工具/资源；断连时自动重连，
资源读取带 TTL 缓存（见 `agent/mcp/manager.py` 与 `tests/test_mcp_phase6.py`）。
