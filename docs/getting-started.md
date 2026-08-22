# 快速开始

> 本文档从根 README 迁移：首次运行引导（离线可用）、最小演示与接入真实模型/MCP。

### 首次运行引导（离线可用）

- **默认即离线可跑**：`config/agent.yaml` 出厂为 `memory.backend: hybrid` +
  `embedder: tfidf`，不依赖任何外部模型/API，长期记忆持久有效。
- **完全离线演示**：`python -m agent run "任务" --config config/offline.yaml`，
  使用内置 MockLLM + hybrid 记忆，零网络零 Key，适合本地自检与 CI。
- **启用向量检索（可选）**：先运行 `scripts/download_embedding_model.py` 下载本地
  sentence-transformers 模型，再把 `memory.backend` 改回 `chroma`、`embedder` 改回
  `sentence-transformers`；否则嵌入器回退 TF-IDF 会因维度不匹配触发集合重建清空记忆。
- **线上模型**：`config/agent.yaml` 默认 `llm.provider: litellm`（DeepSeek），
  运行前设置 `DEEPSEEK_API_KEY`；离线/无 Key 环境请使用 `config/offline.yaml`。


## 快速开始

```powershell
# 安装依赖（已安装则跳过）
pip install pyyaml pydantic jinja2 pytest pytest-asyncio litellm
# 可选：真实向量库后端
pip install qdrant-client    # 或 chromadb
# 可选：JS/TS 精确代码解析（未安装时回退正则）
pip install tree-sitter tree-sitter-javascript tree-sitter-typescript

# 运行测试（终端工具测试会真实创建子进程）
python -X utf8 -m pytest tests -q

# 最小演示：脚本化 LLM 驱动一次完整 ReAct（terminal -> final_answer）
python -X utf8 examples/quick_demo.py
```


## 接入真实模型与 MCP

- 修改 `config/agent.yaml`：`llm.provider: litellm`，填写 `model`（如 `openai/gpt-4o`）
  与 `api_key_env`（如 `OPENAI_API_KEY`）。
- MCP 服务器在 `config/mcp.yaml` 登记（stdio / sse）。`AgentLoop` 启动时经
  `agent/mcp/manager.py` 连接全部服务器、握手、合并工具，并按任务关键词订阅
  资源注入 Prompt（`agent/mcp/client.py`、`agent/mcp/tool.py`）。
- Docker 沙箱（`config/agent.yaml` 的 `sandbox.docker_enabled`）启用后，
  终端/文件工具将路由到容器内执行；当前由 `agent/sandbox/policy.py` 提供
  进程级防护作为默认兜底。


相关文档：[configuration.md](configuration.md)（配置系统）、[features/mcp.md](features/mcp.md)（MCP 生态）。

