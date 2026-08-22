# 方向三：产品化服务化部署指南

本文档说明如何把 Alpha-SWE Agent 作为多用户 HTTP 服务部署与运维。
对应 `server/` 包、`docker/Dockerfile.server`、`docker-compose.yml`。

## 1. 快速启动（本地）

```powershell
python -m pip install -r requirements-server.txt
python -X utf8 -m uvicorn server.main:create_app --factory --host 0.0.0.0 --port 8000
```

首次启动会自动创建管理员，并在控制台打印初始 API Key：

```
[Alpha-SWE] 初始管理员 API Key: as_xxxx
```

交互式 API 文档：`http://127.0.0.1:8000/docs`；
机器可读 OpenAPI：`http://127.0.0.1:8000/openapi.json`。

## 2. 核心 API

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/token` | 任意有效 Key | 校验 Key 并返回用户信息 |
| GET | `/api/v1/me` | 认证 | 当前用户 |
| POST | `/api/v1/users` | admin | 创建用户并签发 API Key |
| POST | `/api/v1/api-keys` | admin | 为用户签发新 Key |
| GET | `/api/v1/users` | admin | 用户列表 |
| POST | `/api/v1/tasks` | developer+ | 提交任务 |
| GET | `/api/v1/tasks` | 认证 | 任务列表（admin 全量，其余仅本人） |
| GET | `/api/v1/tasks/{id}` | 本人或 admin | 任务状态与结果 |
| GET | `/api/v1/tasks/{id}/events` | 本人或 admin | SSE 事件流 |
| POST | `/api/v1/tasks/{id}/cancel` | 本人或 admin | 取消任务 |
| GET/POST | `/api/v1/sessions` | developer+ | 会话管理 |
| GET | `/api/v1/audit` | admin | 审计日志 |
| GET | `/healthz` | 公开 | 健康检查 |

认证方式：请求头 `Authorization: Bearer <API Key>`（服务端只存 SHA-256 哈希）。
OpenAPI 中统一通过 `components.securitySchemes.bearerAuth`（HTTP Bearer）声明，
受保护接口的 `security` 均为 `bearerAuth`；`POST /api/v1/auth/token` 与
`GET /healthz` 为匿名接口。

角色权限：`observer`（只读） < `developer`（提交/取消任务、会话） <
`admin`（用户、Key、审计、全量查看）。

### 2.1 错误响应

| 状态码 | 场景 | 响应体 |
| --- | --- | --- |
| 401 | 未提供或无效 API Key | `{"detail": "无效或缺失 API Key"}` |
| 403 | 角色不足 / 访问他人任务 | `{"detail": "需要角色 developer 及以上"}` |
| 404 | 任务/用户不存在 | `{"detail": "任务不存在"}` |
| 409 | 用户名已存在 / 任务已结束 | `{"detail": "用户名已存在"}` |
| 422 | 请求参数校验失败 | FastAPI 默认 `ValidationError` 结构 |

### 2.2 SSE 事件格式（GET /api/v1/tasks/{id}/events）

- `Content-Type: text/event-stream`；连接保持到任务结束或客户端断开；
- 每帧：`event: <type>` + `data: <json>` + 空行；
- 事件类型：`running` / `completed` / `failed` / `timeout` / `budget` /
  `cancelled` / `error` / `done`（`done` 表示流结束）；
- 任务已结束时订阅，会立即补发终态事件（`data` 含 `final: true`）后关闭。

### 2.3 审计查询参数（GET /api/v1/audit）

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | int | 按用户过滤 |
| `task_id` | string | 按任务过滤（匹配审计明细 `task=<task_id>`） |
| `start_time` / `end_time` | ISO 8601 | 时间范围（含边界） |
| `limit` | int | 返回条数上限，默认 200，最大 1000 |
| `offset` | int | 分页偏移，默认 0 |

## 3. 架构与数据流

```
Client --HTTP/SSE--> FastAPI (server/main.py)
                       |-- store.py   SQLite(WAL) 用户/Key/任务/会话/审计
                       |-- tasks.py   进程内 asyncio 队列 + 子进程 Agent
                       |-- events.py  SSE 事件流
                           每个任务: python -m agent run "指令" --workspace <ws>
```

- 任务在独立子进程中运行 Agent，互不干扰；`max_concurrency` 控制并发；
- 工作区按用户隔离：`workspace_root/user_<id>/<task_id>`；
  绝对路径必须在 `workspace_root` 内（越权返回 403）；
- 任务结果（token/轮次/耗时/修改文件/归因）持久化在 `tasks.result_json`；
- 取消：排队任务直接标记；运行中任务 terminate 子进程。

## 4. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ASWE_HOST` / `ASWE_PORT` | 0.0.0.0 / 8000 | 监听地址 |
| `ASWE_DB_PATH` | `server.db` | SQLite 路径 |
| `ASWE_WORKSPACE_ROOT` | `./server_workspaces` | 任务工作区根目录 |
| `ASWE_CONFIG_PATH` | `config/agent.yaml` | Agent 配置 |
| `ASWE_MAX_CONCURRENCY` | 2 | 并发任务数 |
| `ASWE_ADMIN_API_KEY` | 自动生成 | 初始管理员 Key（可预设） |
| `ASWE_DOCKER` | false | 是否启用 Docker 沙箱 |
| `DEEPSEEK_API_KEY` | - | Agent LLM Key |

## 5. Docker 部署

```bash
# 单机：仅服务（SQLite + 进程内队列）
docker compose up -d --build server

# 基础设施模式：服务 + Redis + Qdrant（后续队列外置 / 向量记忆）
docker compose --profile infra up -d

# 查看初始管理员 Key
docker compose logs server | Select-String "API Key"
```

生产建议：

- 前置 Nginx/Caddy 反向代理并启用 HTTPS；
- 用 `ASWE_ADMIN_API_KEY` 预设管理员 Key，避免明文打印；
- 将 `ASWE_DB_PATH` 挂载到持久卷（compose 中已挂载 `/data`）；
- 定期备份 `server.db`（WAL 模式下备份 `server.db` + `-wal` + `-shm`）。

## 6. 运维与监控

- 健康检查：`GET /healthz`（compose 内置 HEALTHCHECK）；
- 审计：所有敏感操作（建用户、签发 Key、提交/取消任务、认证失败）写入
  `audit_logs`，管理员可查询；
- 指标：Agent 自身有 `agent/observability/metrics.py`；服务层可继续接入
  Prometheus（建议在 `main.py` 增加 `/metrics`，暴露任务成功率/排队长度/运行数）；
- 日志：uvicorn + `server.*` 日志输出到 stdout，容器内由 Docker 采集轮转。

## 7. 与方向二的协同

- SWE-bench 批量评估（`scripts/run_swebench.py`）可直接提交到本服务：
  每个实例一个任务，工作区指向克隆好的仓库；
- 服务的任务结果字段与 `swe_eval` 的 `AdapterResult` 结构对齐，便于汇总；
- 记忆共享：`agent/memory/store.py` 已支持多后端，服务多实例部署时
  将 `memory.backend` 切换到共享后端（如 Qdrant）即可实现记忆团队共享。

## 8. 后续演进（方向三未完成项）

- 队列外置：替换 `server/tasks.py` 为 Celery/arq + Redis（compose 已含 Redis）；
- 前端面板：基于 `/tasks`、`/events` 做一个简单 SPA（任务提交、状态列表、历史回放）；
- 插件/技能市场：复用 `agent/context/skill.py` 与 `plugin_loader.py`，提供
  `POST /api/v1/skills` 发布与版本管理；
- 向量记忆服务化：对接 Qdrant 命名空间（全局/团队/项目/用户）。
