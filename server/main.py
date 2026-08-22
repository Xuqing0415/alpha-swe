# -*- coding: utf-8 -*-
"""Alpha-SWE 产品化服务：FastAPI 应用工厂与路由（方向三）。

启动：
    uvicorn server.main:app --host 0.0.0.0 --port 8000

API（前缀 /api/v1）：
    POST /auth/token          API Key 换取访问信息（Bearer 即 Key）
    POST /users               创建用户（admin）
    POST /api-keys            为用户签发新 Key（admin）
    GET  /tasks               任务列表（admin 看全部，其余看自己）
    POST /tasks               提交任务（developer+）
    GET  /tasks/{id}          任务状态
    GET  /tasks/{id}/events   SSE 事件流
    POST /tasks/{id}/cancel   取消任务
    GET  /sessions            会话列表
    POST /sessions            创建会话
    GET  /audit               审计日志（admin）
    GET  /healthz             健康检查
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi import Path as ApiPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, StreamingResponse

from server.auth import current_user, require_role
from server.config import ServerConfig
from server.events import sse_generator
from server.models import (
    ApiKeyCreate,
    ApiKeyIssued,
    AuditOut,
    CancelOut,
    HealthOut,
    MeOut,
    SessionCreate,
    SessionOut,
    TaskCreate,
    TaskOut,
    TaskSubmitOut,
    TokenRequest,
    TokenResponse,
    UserCreate,
    UserListItem,
    UserWithKey,
)
from server.store import (ADMIN_ROLE, DEVELOPER_ROLE, Store,
                          User, utc_iso)
from server.tasks import DONE_EVENT, TaskQueue

logger = logging.getLogger("server.main")
API_PREFIX = "/api/v1"

# ---------- 通用错误响应（OpenAPI 文档示例，与后端真实行为一致） ----------
RESP_401 = {
    "description": "未提供或无效的 API Key（Bearer Token）",
    "content": {
        "application/json": {
            "example": {"detail": "无效或缺失 API Key"},
        }
    },
}
RESP_403 = {
    "description": "已认证但权限不足：角色等级不够，或无权访问他人的任务",
    "content": {
        "application/json": {
            "example": {"detail": "需要角色 developer 及以上"},
        }
    },
}
RESP_404 = {
    "description": "资源不存在（任务 / 用户等）",
    "content": {
        "application/json": {
            "example": {"detail": "任务不存在"},
        }
    },
}
RESP_409 = {
    "description": "资源冲突：用户名已存在，或任务已结束无法取消",
    "content": {
        "application/json": {
            "example": {"detail": "用户名已存在"},
        }
    },
}
RESP_422 = {
    "description": "请求参数校验失败（FastAPI 默认 ValidationError 结构）",
    "content": {
        "application/json": {
            "example": {
                "detail": [
                    {"loc": ["body", "instruction"], "msg": "field required",
                     "type": "value_error.missing"},
                ]
            },
        }
    },
}
RESP_SSE_200 = {
    "description": (
        "SSE 事件流（text/event-stream），连接保持到任务结束或客户端断开。"
        "每帧格式：`event: <type>` 换行 `data: <json>` 再空行。"
        "事件类型：running / completed / failed / timeout / budget / "
        "cancelled / error / done（done 表示流结束）。"
    ),
    "content": {
        "text/event-stream": {
            "example": (
                "event: running\n"
                "data: {\"id\": \"task_xxx\","
                " \"instruction\": \"修复登录空指针\"}\n\n"
                "event: completed\n"
                "data: {\"id\": \"task_xxx\", \"error\": null,"
                " \"payload\": {\"ok\": true,"
                " \"status\": \"completed\"}}\n\n"
                "event: done\n"
                "data: {}\n\n"
            )
        }
    },
}


class AppState:
    def __init__(self) -> None:
        self.config: Optional[ServerConfig] = None
        self.store: Optional[Store] = None
        self.queue: Optional[TaskQueue] = None


def _workspace_for(state: AppState, user: User, task_id: str,
                   workspace_arg: Optional[str]) -> str:
    """解析任务工作区：默认用户隔离目录；绝对路径必须在 workspace_root 内。"""
    root = Path(state.config.workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    user_dir = root / f"user_{user.id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    if not workspace_arg:
        ws = user_dir / task_id
    else:
        p = Path(workspace_arg)
        if p.is_absolute():
            if not str(p.resolve()).startswith(str(root)):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "工作区必须在 workspace_root 之内")
            ws = p.resolve()
        else:
            ws = (user_dir / p).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: AppState = app.state.aswe
    state.config.ensure_dirs()
    await state.queue.start()
    try:
        yield
    finally:
        await state.queue.stop()
        if state.store:
            state.store.close()


def create_app(config: Optional[ServerConfig] = None,
               store: Optional[Store] = None,
               runner=None) -> FastAPI:
    """应用工厂；runner 供测试注入（替代真实子进程）。"""
    cfg = config or ServerConfig.from_env()
    st = store or Store(cfg.db_path)
    queue = TaskQueue(st, cfg, runner=runner)

    state = AppState()
    state.config = cfg
    state.store = st
    state.queue = queue

    app = FastAPI(
        title="Alpha-SWE Agent Service",
        version="0.1.0",
        description=(
            "SWE Agent 产品化服务：多用户任务提交、SSE 进度、审计。\n\n"
            "鉴权：除 POST /auth/token、GET /healthz 外，所有接口需在请求头"
            "携带 Authorization: Bearer <token>（token 由 API Key 换取）。"
            "角色：observer < developer < admin。"
        ),
        lifespan=lifespan,
        openapi_tags=[
            {"name": "auth",
             "description": "认证：API Key 换取访问凭证、当前用户"},
            {"name": "admin",
             "description": "管理员：用户、API Key、审计"},
            {"name": "tasks",
             "description": "任务：提交、查询、SSE 事件流、取消"},
            {"name": "sessions", "description": "会话管理"},
            {"name": "ops", "description": "运维：健康检查"},
        ],
    )
    app.state.aswe = state
    app.state.store = st
    app.state.config = cfg
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"])

    _seed_admin(st, cfg)

    # ---------------- 根路径引导页（避免访问 / 时 404） ----------------
    @app.get("/", tags=["ops"], response_class=HTMLResponse,
             include_in_schema=False)
    def index() -> str:
        return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Alpha-SWE Agent Service</title>
<style>
  body { font-family: "Cascadia Code", Consolas, monospace; background: #111;
         color: #ddd; margin: 0; padding: 40px; }
  h1 { color: #7ee787; font-size: 22px; }
  a { color: #79c0ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { background: #222; padding: 1px 6px; border-radius: 4px; color: #ffa657; }
  .card { background: #181818; border: 1px solid #333; border-radius: 8px;
          padding: 16px 20px; margin: 12px 0; max-width: 720px; }
  .muted { color: #8b949e; font-size: 13px; }
</style>
</head>
<body>
  <h1>Alpha-SWE Agent Service</h1>
  <p class="muted">SWE Agent 产品化服务：多用户任务提交、SSE 进度、审计</p>
  <div class="card">
    <p><a href="/docs">/docs</a> - Swagger API 文档（可交互调试）</p>
    <p><a href="/redoc">/redoc</a> - ReDoc 文档</p>
    <p><a href="/healthz">/healthz</a> - 健康检查</p>
    <p><a href="/openapi.json">/openapi.json</a> - OpenAPI 定义</p>
  </div>
  <div class="card">
    <p>API 前缀：<code>/api/v1</code>（如 <code>POST /api/v1/auth/token</code> 换 Key，
    <code>POST /api/v1/tasks</code> 提交任务）</p>
    <p>启动方式：<code>uvicorn server.main:create_app --factory --host 0.0.0.0 --port 8000</code></p>
  </div>
</body>
</html>"""

    # ---------------- 认证与用户 ----------------
    @app.post(f"{API_PREFIX}/auth/token", tags=["auth"],
              response_model=TokenResponse,
              summary="API Key 换取访问凭证",
              description=(
                  "用 API Key 换取访问凭证（access_token 即该 Key 明文）。"
                  "后续请求头携带 Authorization: Bearer <access_token>。"
                  "服务端只存 SHA-256 哈希，Key 无法二次查询。"),
              responses={401: RESP_401, 422: RESP_422})
    def exchange_token(body: TokenRequest):
        user = st.authenticate(body.api_key)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效 API Key")
        return {"access_token": body.api_key, "token_type": "bearer",
                "user": {"id": user.id, "name": user.name,
                         "role": user.role}}

    @app.get(f"{API_PREFIX}/me", tags=["auth"], response_model=MeOut,
             summary="当前用户信息", openapi_extra={"security": [{"bearerAuth": []}]},
             responses={401: RESP_401})
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "name": user.name, "role": user.role}

    @app.post(f"{API_PREFIX}/users", status_code=201, tags=["admin"],
              response_model=UserWithKey, summary="创建用户并签发 API Key",
              openapi_extra={"security": [{"bearerAuth": []}]},
              description="仅 admin。返回的 api_key 仅在创建时明文展示一次。",
              responses={401: RESP_401, 403: RESP_403, 409: RESP_409,
                         422: RESP_422})
    def create_user(body: UserCreate, _: User = Depends(require_role(ADMIN_ROLE))):
        try:
            user, api_key = st.create_user(body.name, body.role)
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
        st.audit(_.id, "user_create", f"name={body.name} role={body.role}")
        return {"user": {"id": user.id, "name": user.name, "role": user.role},
                "api_key": api_key,
                "note": "请立即保存 api_key（仅此一次明文展示）"}

    @app.post(f"{API_PREFIX}/api-keys", status_code=201, tags=["admin"],
              response_model=ApiKeyIssued, summary="为用户签发新 API Key",
              openapi_extra={"security": [{"bearerAuth": []}]},
              description="仅 admin。返回的 api_key 仅在签发时明文展示一次。",
              responses={401: RESP_401, 403: RESP_403, 404: RESP_404,
                         422: RESP_422})
    def issue_key(body: ApiKeyCreate, admin: User = Depends(require_role(ADMIN_ROLE))):
        target = st.get_user(body.user_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
        key = st.issue_api_key(target.id)
        st.audit(admin.id, "api_key_issue", f"user_id={target.id}")
        return {"user_id": target.id, "api_key": key,
                "note": "请立即保存 api_key（仅此一次明文展示）"}

    @app.get(f"{API_PREFIX}/users", tags=["admin"],
             response_model=List[UserListItem], summary="用户列表",
             openapi_extra={"security": [{"bearerAuth": []}]},
             responses={401: RESP_401, 403: RESP_403})
    def list_users(_: User = Depends(require_role(ADMIN_ROLE))):
        return [{"id": u.id, "name": u.name, "role": u.role,
                 "created_at": utc_iso(u.created_at)}
                for u in st.list_users()]

    # ---------------- 任务 ----------------
    @app.post(f"{API_PREFIX}/tasks", status_code=201, tags=["tasks"],
              response_model=TaskSubmitOut, summary="提交任务",
              openapi_extra={"security": [{"bearerAuth": []}]},
              description=(
                  "将任务加入队列，在独立子进程中运行 Agent。"
                  "config_path 为 null 时使用服务端默认配置。"),
              responses={401: RESP_401, 403: RESP_403, 422: RESP_422})
    def submit_task(body: TaskCreate,
                    user: User = Depends(require_role(DEVELOPER_ROLE))):
        timeout = body.timeout or cfg.default_timeout or 1800.0
        task_id = st.create_task(
            user_id=user.id, instruction=body.instruction,
            workspace="", config_path=body.config_path or cfg.config_path,
            timeout=timeout, max_cost=body.max_cost or cfg.default_max_cost,
            max_tokens=body.max_tokens or cfg.default_max_tokens)
        try:
            ws = _workspace_for(state, user, task_id, body.workspace)
        except HTTPException:
            st.update_task(task_id, status="failed", error="工作区越权")
            raise
        st.update_task(task_id, workspace=ws)
        queue.submit(task_id)
        st.audit(user.id, "task_submit",
                 f"task={task_id} timeout={timeout:g}")
        return {"id": task_id, "status": "queued", "workspace": ws}

    @app.get(f"{API_PREFIX}/tasks", tags=["tasks"],
             response_model=List[TaskOut], summary="任务列表",
             openapi_extra={"security": [{"bearerAuth": []}]},
             description="admin 可见全部任务，其余角色仅可见自己的任务。",
             responses={401: RESP_401})
    def list_tasks(user: User = Depends(current_user)):
        rows = st.list_tasks(user_id=None if user.role == ADMIN_ROLE
                             else user.id)
        return [r.to_dict() for r in rows]

    def _can_access_task(user: User, task_id: str):
        rec = st.get_task(task_id)
        if rec is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
        if user.role != ADMIN_ROLE and rec.user_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该任务")
        return rec

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}", tags=["tasks"],
             response_model=TaskOut, summary="查询任务状态与结果",
             openapi_extra={"security": [{"bearerAuth": []}]},
             responses={401: RESP_401, 403: RESP_403, 404: RESP_404,
                        422: RESP_422})
    def get_task(
        task_id: str = ApiPath(
            ..., pattern="^task_[0-9a-f]{32}$",
            description="任务 ID（task_ 前缀 + 32 位十六进制）"),
        user: User = Depends(current_user)):
        return _can_access_task(user, task_id).to_dict()

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/events", tags=["tasks"],
             summary="订阅任务 SSE 事件流", openapi_extra={"security": [{"bearerAuth": []}]},
             description=(
                 "以 text/event-stream 持续推送任务进度；任务结束后推送终态"
                 "事件与 done 事件并关闭连接。若任务已结束，订阅后会立即补发"
                 "终态事件（data 含 final: true）。"),
             responses={200: RESP_SSE_200, 401: RESP_401, 403: RESP_403,
                        404: RESP_404, 422: RESP_422})
    async def task_events(
        task_id: str = ApiPath(
            ..., pattern="^task_[0-9a-f]{32}$",
            description="任务 ID（task_ 前缀 + 32 位十六进制）"),
        user: User = Depends(current_user)):
        _can_access_task(user, task_id)
        rec = st.get_task(task_id)
        queue_events = queue.bus.subscribe(task_id)
        # 若任务已结束，直接补发当前状态再结束
        if rec.status in ("completed", "failed", "cancelled", "timeout",
                          "budget"):
            await queue.bus.publish(task_id, rec.status,
                                    {"id": task_id, "final": True})
            await queue.bus.publish(task_id, DONE_EVENT, {})
        return StreamingResponse(
            sse_generator(queue_events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"})

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel", tags=["tasks"],
              response_model=CancelOut, summary="取消任务",
              openapi_extra={"security": [{"bearerAuth": []}]},
              description="排队中的任务直接标记取消；运行中的任务终止子进程。",
              responses={401: RESP_401, 403: RESP_403, 404: RESP_404,
                         409: RESP_409, 422: RESP_422})
    async def cancel_task(
        task_id: str = ApiPath(
            ..., pattern="^task_[0-9a-f]{32}$",
            description="任务 ID（task_ 前缀 + 32 位十六进制）"),
        user: User = Depends(current_user)):
        _can_access_task(user, task_id)
        ok = await queue.cancel(task_id)
        if not ok:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "任务已结束或正在收尾")
        st.audit(user.id, "task_cancel", f"task={task_id}")
        return {"id": task_id, "status": "cancelled"}

    # ---------------- 会话 ----------------
    @app.get(f"{API_PREFIX}/sessions", tags=["sessions"],
             response_model=List[SessionOut], summary="会话列表",
             openapi_extra={"security": [{"bearerAuth": []}]},
             description="admin 可见全部会话，其余角色仅可见自己的会话。",
             responses={401: RESP_401})
    def list_sessions(user: User = Depends(current_user)):
        rows = st.list_sessions(user_id=None if user.role == ADMIN_ROLE
                                else user.id)
        return [{"id": r.id, "label": r.label, "user_id": r.user_id,
                 "created_at": utc_iso(r.created_at)} for r in rows]

    @app.post(f"{API_PREFIX}/sessions", status_code=201, tags=["sessions"],
              response_model=SessionOut, summary="创建会话",
              openapi_extra={"security": [{"bearerAuth": []}]},
              description="developer 及以上可创建会话。",
              responses={401: RESP_401, 403: RESP_403, 422: RESP_422})
    def create_session(body: SessionCreate,
                       user: User = Depends(require_role(DEVELOPER_ROLE))):
        rec = st.create_session(user.id, body.label)
        return {"id": rec.id, "label": rec.label, "user_id": rec.user_id,
                "created_at": utc_iso(rec.created_at)}

    # ---------------- 审计与健康 ----------------
    @app.get(f"{API_PREFIX}/audit", tags=["admin"],
             response_model=List[AuditOut], summary="审计日志",
             openapi_extra={"security": [{"bearerAuth": []}]},
             description=(
                 "仅 admin。支持按用户、任务、时间范围过滤与分页。"
                 "task_id 过滤基于审计明细中的 task=<task_id> 匹配。"),
             responses={401: RESP_401, 403: RESP_403, 422: RESP_422})
    def list_audit(
        _: User = Depends(require_role(ADMIN_ROLE)),
        user_id: Optional[int] = Query(
            default=None, gt=0, description="按用户 ID 过滤"),
        task_id: Optional[str] = Query(
            default=None, max_length=64,
            description="按任务 ID 过滤（匹配审计明细 task=<task_id>）"),
        start_time: Optional[datetime] = Query(
            default=None, description="起始时间（ISO 8601，含）"),
        end_time: Optional[datetime] = Query(
            default=None, description="结束时间（ISO 8601，含）"),
        limit: int = Query(
            default=200, ge=1, le=1000, description="返回条数上限"),
        offset: int = Query(
            default=0, ge=0, description="分页偏移")):
        return [{"id": r.id, "user_id": r.user_id, "action": r.action,
                 "detail": r.detail, "created_at": utc_iso(r.created_at)}
                for r in st.list_audit(
                    user_id=user_id, task_id=task_id,
                    start_time=start_time, end_time=end_time,
                    limit=limit, offset=offset)]

    @app.get("/healthz", tags=["ops"], response_model=HealthOut,
             summary="健康检查")
    def healthz():
        return {"ok": True, "tasks_running": len(queue._procs)}

    # ---------- OpenAPI 规范化：声明统一 Bearer 鉴权方案 ----------
    def _openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "opaque",
                "description": (
                    "请求头携带 Authorization: Bearer <token>；"
                    "token 通过 POST /auth/token 用 API Key 换取。"),
            }
        }
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi

    return app


def _seed_admin(store: Store, cfg: ServerConfig) -> None:
    """启动时确保存在管理员；未配置 Key 则自动生成。"""
    if store.user_count() > 0:
        return
    admin = store.get_user_by_name("admin")
    if admin is None:
        key = cfg.admin_api_key or os.environ.get("ASWE_ADMIN_API_KEY")
        if key:
            # 用指定 Key 创建：先建用户再注入该 Key（跳过随机签发）
            try:
                admin, _ = store.create_user("admin", ADMIN_ROLE)
                # 直接以明文写入自定义 Key 的哈希
                from server.store import ApiKey, hash_api_key
                with store.Session() as s:
                    s.add(ApiKey(user_id=admin.id, prefix=key[:8],
                                 key_hash=hash_api_key(key)))
                    s.commit()
            except ValueError:
                pass
        else:
            admin, key = store.create_user("admin", ADMIN_ROLE)
            print("=" * 60, flush=True)
            print(f"[Alpha-SWE] 初始管理员 API Key: {key}", flush=True)
            print("[Alpha-SWE] 角色: admin  用户名: admin", flush=True)
            print("=" * 60, flush=True)


if __name__ == "__main__":
    import uvicorn
    cfg = ServerConfig.from_env()
    uvicorn.run("server.main:create_app", host=cfg.host, port=cfg.port,
                log_level=cfg.log_level.lower(), factory=True)
