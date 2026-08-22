# 长期记忆

> 本文档从根 README 迁移（原「长期记忆（第 7 节）」），入口 `agent/memory/`。

## 长期记忆（第 7 节）

- **写入**：任务完成后 LLM 自动生成经验摘要（problem/steps/solution/outcome/key_files），
  失败时记录错误记忆（错误类型 + 上下文），文件写入/读取后自动索引代码（路径 + 符号 + 片段）。
- **检索**：任务指令触发混合检索（向量相似度 + 关键词打分，`hybrid_weight_vector` 调权重），
  优先按任务类型（fix/add/refactor/test）过滤同类型经验，命中为空再放宽全量；
  结果注入 Prompt 的「检索到的历史记忆」区块。
- **闭环**：
  - 去重：写入前 `find_similar` 检查，相似度 ≥ `memory.dedup_threshold`（默认 0.95）只更新引用计数（`memory.dedup`）；
  - 反例：错误记忆标记 `negative=true`，检索时正例优先、反例降权（`counter_example_penalty`）；
  - 衰减：超过 `memory.decay_days` 未引用，分数按 `decay_factor` 指数衰减；被引用次数越多越可信（use_count 加成）；
  - 决策日志：`memory.write` / `memory.dedup` / `memory.retrieve` / `retrieval_skip` 记录写入与检索路径。
- **后端**：`config/agent.yaml` 的 `memory.backend`：
  - `auto`：有 `chromadb` 用之，否则有 `qdrant-client` 用之，否则本地 `hybrid`（TF-IDF，零新依赖）；
  - `sqlite`：纯关键词（最轻）；`hybrid`：本地向量 + 关键词；`chroma` / `qdrant`：真实向量库。
- **嵌入器**：`memory.embedder` 支持 `tfidf`（默认）、`sentence-transformers`（本地模型）、
  `openai`（Embeddings API，`embedding_api_key_env` 指定密钥环境变量）。


验证：`tests/test_memory_closed_loop.py`（A/A' 复用验证）等记忆相关测试。

