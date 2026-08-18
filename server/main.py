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
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from server.auth import current_user, require_role
from server.config import ServerConfig
from server.events import sse_generator
from server.models import ApiKeyCreate, SessionCreate, TaskCreate, TokenRequest, UserCreate
from server.store import (ADMIN_ROLE, DEVELOPER_ROLE, Store,
                          User, utc_iso)
from server.tasks import DONE_EVENT, TaskQueue

logger = logging.getLogger("server.main")
API_PREFIX = "/api/v1"


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
        description="SWE Agent 产品化服务：多用户任务提交、SSE 进度、审计",
        lifespan=lifespan,
    )
    app.state.aswe = state
    app.state.store = st
    app.state.config = cfg
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"])

    _seed_admin(st, cfg)

    # ---------------- 认证与用户 ----------------
    @app.post(f"{API_PREFIX}/auth/token", tags=["auth"])
    def exchange_token(body: TokenRequest):
        user = st.authenticate(body.api_key)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效 API Key")
        return {"access_token": body.api_key, "token_type": "bearer",
                "user": {"id": user.id, "name": user.name,
                         "role": user.role}}

    @app.get(f"{API_PREFIX}/me", tags=["auth"])
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "name": user.name, "role": user.role}

    @app.post(f"{API_PREFIX}/users", status_code=201, tags=["admin"])
    def create_user(body: UserCreate, _: User = Depends(require_role(ADMIN_ROLE))):
        try:
            user, api_key = st.create_user(body.name, body.role)
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
        st.audit(_.id, "user_create", f"name={body.name} role={body.role}")
        return {"user": {"id": user.id, "name": user.name, "role": user.role},
                "api_key": api_key,
                "note": "请立即保存 api_key（仅此一次明文展示）"}

    @app.post(f"{API_PREFIX}/api-keys", status_code=201, tags=["admin"])
    def issue_key(body: ApiKeyCreate, admin: User = Depends(require_role(ADMIN_ROLE))):
        target = st.get_user(body.user_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
        key = st.issue_api_key(target.id)
        st.audit(admin.id, "api_key_issue", f"user_id={target.id}")
        return {"user_id": target.id, "api_key": key,
                "note": "请立即保存 api_key（仅此一次明文展示）"}

    @app.get(f"{API_PREFIX}/users", tags=["admin"])
    def list_users(_: User = Depends(require_role(ADMIN_ROLE))):
        return [{"id": u.id, "name": u.name, "role": u.role,
                 "created_at": utc_iso(u.created_at)}
                for u in st.list_users()]

    # ---------------- 任务 ----------------
    @app.post(f"{API_PREFIX}/tasks", status_code=201, tags=["tasks"])
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

    @app.get(f"{API_PREFIX}/tasks", tags=["tasks"])
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}", tags=["tasks"])
    def get_task(task_id: str, user: User = Depends(current_user)):
        return _can_access_task(user, task_id).to_dict()

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/events", tags=["tasks"])
    async def task_events(task_id: str,
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

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel", tags=["tasks"])
    async def cancel_task(task_id: str,
                          user: User = Depends(current_user)):
        _can_access_task(user, task_id)
        ok = await queue.cancel(task_id)
        if not ok:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "任务已结束或正在收尾")
        st.audit(user.id, "task_cancel", f"task={task_id}")
        return {"id": task_id, "status": "cancelled"}

    # ---------------- 会话 ----------------
    @app.get(f"{API_PREFIX}/sessions", tags=["sessions"])
    def list_sessions(user: User = Depends(current_user)):
        rows = st.list_sessions(user_id=None if user.role == ADMIN_ROLE
                                else user.id)
        return [{"id": r.id, "label": r.label, "user_id": r.user_id,
                 "created_at": utc_iso(r.created_at)} for r in rows]

    @app.post(f"{API_PREFIX}/sessions", status_code=201, tags=["sessions"])
    def create_session(body: SessionCreate,
                       user: User = Depends(require_role(DEVELOPER_ROLE))):
        rec = st.create_session(user.id, body.label)
        return {"id": rec.id, "label": rec.label, "user_id": rec.user_id,
                "created_at": utc_iso(rec.created_at)}

    # ---------------- 审计与健康 ----------------
    @app.get(f"{API_PREFIX}/audit", tags=["admin"])
    def list_audit(_: User = Depends(require_role(ADMIN_ROLE))):
        return [{"id": r.id, "user_id": r.user_id, "action": r.action,
                 "detail": r.detail, "created_at": utc_iso(r.created_at)}
                for r in st.list_audit()]

    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"ok": True, "tasks_running": len(queue._procs)}

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
