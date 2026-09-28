# 事件流 schema

> Agent 运行时事件的统一定义：TUI / Web 面板 / SSE / CLI 共用同一套类型、分组与序列化规则。
> 实现见 `agent/observability/event_schema.py`；单一事实源是该模块的 `EVENT_TYPES`，本文件与之同步维护。

## 1. 事件记录结构

AgentLoop 在每个关键节点通过 `_emit(event_type, **data)` 产出一条记录（`agent/core/loop.py`）：

```json
{"type": "tool_call", "data": {"task_id": "t0", "tool": "file_ops"}, "ts": 1730000000.123}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | str | 事件类型，见第 3 节；缺失/非字符串/空串由 `normalize_event()` 归一为 `"unknown"` |
| `data` | object | 事件负载；非对象由 `normalize_event()` 包装为 `{"value": <原值>}` |
| `ts` | float | Unix 时间戳（秒）；缺失/非数值时由 `normalize_event()` 补当前时间 |

- `normalize_event(record)` 返回**新副本**，绝不修改入参，便于多个消费方各自处理同一份记录。
- `validate_event(record)` 返回问题描述列表（结构性错误 + 未知类型提示），`is_valid(record)` 是其布尔形式。
- `describe(record)` 生成单行人类可读摘要，供日志/TUI 直接打印。

## 2. 事件分组（EventKind）

| 分组 | 说明 |
|------|------|
| `lifecycle` | 运行生命周期：开始 / 规划 / 结束 / 中断 / 优先级变化 |
| `task` | 任务粒度：完成 / 失败 / 重试 / 跳过 / 恢复 / 抢占 / 中断 |
| `thought` | 模型思考 |
| `tool` | 工具调用 |
| `state` | 会话状态快照（阶段门禁 / 防线） |
| `budget` | 预算告警与耗尽 |
| `barrier` | 阶段栅栏 |
| `error` | 错误（当前预留，`run_error` 仍归 `lifecycle`） |
| `other` | 未知类型兜底 |

## 3. 已知事件类型

| 分组 | 事件类型 |
|------|----------|
| `lifecycle` | `run_start`、`plan_created`、`dry_run_plan`、`run_done`、`run_error`、`interrupt`、`priority_changed` |
| `task` | `task_done`、`task_failed`、`task_retry`、`task_skipped`、`task_resumed`、`task_preempted`、`task_interrupted` |
| `thought` | `think` |
| `tool` | `tool_call` |
| `state` | `session_state` |
| `budget` | `budget_warning`、`budget_exhausted` |
| `barrier` | `phase_barrier_task_start`、`phase_barrier_summary` |

未知类型不会报错：`kind_of()` 返回 `EventKind.OTHER`，消费方**不得**因未知类型丢弃事件。
控制帧 `ping` / `done` / `snapshot` 不是业务事件，不登记在 `EVENT_TYPES` 中。

## 4. 消费方接线

| 消费方 | 用法 |
|--------|------|
| SSE `server/events.py` | `to_sse()` 输出 `event: <type>\ndata: <json>\n\n`；`strict_validation=True` 时逐条 `validate_event()` 并记警告，但**仍然发送**（默认 False，不产生额外行为） |
| Web 面板 `agent/observability/web.py` | `_push()` 用 `normalize_event()` 归一化并附 `kind` 字段；前端按 `kind` 做分组筛选 |
| TUI `tui/formatting.py` | 未在 `_LOG_TYPES` 登记的已知类型按 `EventKind` 分组着色，正文用 `describe()` 兜底 |

## 5. 迭代约定

- 新增事件类型：在 `EVENT_TYPES` 中登记分组；`tests/test_event_schema.py` 会覆盖全量分组的合法性。
- 新增分组：同步更新本文件的第 2 节与 TUI 的 `_KIND_STYLES`、Web 前端筛选按钮。
- 兼容性：`normalize_event()` / `kind_of()` / `validate_event()` 对任意输入都不抛异常。
