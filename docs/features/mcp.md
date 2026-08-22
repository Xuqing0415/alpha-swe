# MCP 生态

> 本文档从根 README 迁移（原「MCP 生态打通（阶段六）」），入口 `agent/mcp/`，服务器清单见 `config/mcp.yaml`。

## MCP 生态打通（阶段六）

- **动态发现与断连处理**：`MCPManager.ensure_connected()` 首次连接 + 按 `mcp.reconnect_attempts` 自动重连；
  失败服务器记录在 `failed_servers`，其工具从 `build_tools()` 降级隐藏（决策日志 `mcp.connect_failed/reconnect/degrade`）。
- **资源缓存**：`read_resource()` 带 TTL 缓存（`mcp.resource_cache_ttl`，0 = 不缓存），命中记录
  `mcp.resource_cache_hit`；`invalidate_resources()` 支持按服务器/URI 失效。
- **自定义 TypeScript 服务器**（`mcp-servers/`）：`knowledge-base`（团队知识库：`search_kb`/`add_kb_entry` +
  `kb://` 资源）、`issue-tracker`（Issue 追踪：`list_issues`/`create_issue`/`update_issue_status` + `issue://` 资源）。
  构建：`npm install && npm run build`，注册进 `config/mcp.yaml`（stdio）。

验证见 `tests/test_mcp_phase6.py`（重连/降级/缓存）与 `tests/test_mcp_ts_servers.py`（真实 TS 服务器端到端）。

