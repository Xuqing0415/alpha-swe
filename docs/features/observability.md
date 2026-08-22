# 可观测性

> 本文档从根 README 迁移（原「可观测性与 TUI 交互升级（阶段七 / 阶段八）」的追踪/指标/档案/Web 面板/OTLP 部分），入口 `agent/observability/`。

## 可观测性与 TUI 交互升级（阶段七 / 阶段八）

### 分布式追踪（`agent/observability/trace.py`）
- OTel 风格 `Span` / `Tracer`：`run -> task -> llm/tool` 调用链（父子 span_id），
  结束后导出 JSONL 到 `agent.trace_dir`（默认 `./logs/traces`；`trace_enabled: false` 时零成本跳过）。
- 每次 `AgentLoop.run()` 生成 `run` 根 span；每个任务、每次 LLM 调用、每个工具调用各生成一个 span，
  记录耗时、状态、错误与关键属性（token、轮次、参数摘要）。

### 实时指标（`agent/observability/metrics.py`）
- `MetricsRegistry` 计数/采样：token 用量与速率、工具调用与成功率、LLM 调用、
  压缩/重试/中断次数、任务完成/失败、当前阶段与轮次；`alerts()` 按阈值告警
  （token 速率过快、连续失败 ≥3 次、轮次逼近上限），供 TUI 监控视图渲染。

### 会话档案与回放（`agent/observability/archive.py`）
- `SessionArchive.write()` 把事件 + span + 决策日志 + 指标 + 结果打包为单个 JSON
  （`agent.session_archive_dir`，默认 `./logs/sessions`；`archive_enabled` 控制）。
- `SessionReplay` 按时间戳合并排序生成时间线，支持单步回放；CLI 用法：
  `python -m tui --replay logs/sessions/session_xxx.json`

### Web 观测面板（第 9 节，`agent/observability/web.py`）
- `ObservabilityHub` 把 AgentLoop 运行时状态聚合成只读快照
  （status / metrics / spans / decisions / events / sessions），线程安全；
- `ObservabilityServer`（标准库 ThreadingHTTPServer）提供 JSON API、SSE 实时
  事件流与单文件 HTML 面板（无构建链、无第三方依赖）：概览指标卡片、Span
  甘特图与树、决策明细、事件流、会话档案浏览；
- 启动：`python -m tui --web "任务提示词"`（或 `agent.web_panel_enabled: true`），
  默认 `http://127.0.0.1:8765`（`web_panel_host` / `web_panel_port` 可配）。

### OpenTelemetry/Jaeger 导出与结构化日志（第 10 节，`agent/observability/otel.py`）
- `OtlpExporter` 把 span 映射为 OTLP/HTTP JSON 导出到 Collector / Jaeger v2 /
  Tempo 的 `/v1/traces`（`agent.otel_endpoint`，如 `http://127.0.0.1:4318`；
  `otel_enabled` 开关）；失败静默降级并记录 `otel.export` 决策点，本地 JSONL 始终保留；
- `JsonLinesLogHandler` 把 logging 记录写成结构化 JSONL
  （`agent.structured_log_dir`），供 Loki/ELK 接入。

