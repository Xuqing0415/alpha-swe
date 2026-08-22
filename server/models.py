# -*- coding: utf-8 -*-
"""API 请求/响应模型（方向三）。

- 请求模型：TokenRequest / UserCreate / ApiKeyCreate / TaskCreate / SessionCreate；
- 响应模型：与路由真实返回结构一一对应，并通过 ``json_schema_extra.example``
  提供示例，保证 /docs 文档与后端行为一致。
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# 角色枚举（与 server/store.py 的 ROLES 保持一致）
Role = Literal["admin", "developer", "observer"]


class TokenRequest(BaseModel):
    api_key: str = Field(
        ..., min_length=3, max_length=256,
        description="API Key（仅用于换取访问凭证，服务端只存 SHA-256 哈希）",
        examples=["as_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"],
    )


class UserCreate(BaseModel):
    name: str = Field(
        ..., min_length=2, max_length=128,
        description="用户名（唯一）", examples=["zhang_san"])
    role: Role = Field(
        default="developer",
        description="角色：admin / developer / observer",
        examples=["developer"],
    )


class ApiKeyCreate(BaseModel):
    user_id: int = Field(
        ..., gt=0, description="目标用户 ID", examples=[1])


class TaskCreate(BaseModel):
    instruction: str = Field(
        ..., min_length=3, max_length=20000,
        description="任务指令（Agent 的工作描述）",
        examples=["修复 UserService.validate 的空指针风险并补充单元测试"],
    )
    workspace: Optional[str] = Field(
        default=None, max_length=2000,
        description="任务工作区。为 null 时使用按用户隔离的默认目录"
                    "（workspace_root/user_<id>/<task_id>）；相对路径基于该"
                    "目录解析；绝对路径必须在 workspace_root 之内，否则 403。",
        examples=[""],
    )
    config_path: Optional[str] = Field(
        default=None, max_length=2000,
        description="Agent 配置文件路径。为 null 时使用服务端默认配置"
                    "（ASWE_CONFIG_PATH，默认 config/agent.yaml）。",
        examples=[None],
    )
    timeout: Optional[float] = Field(
        default=None, gt=0, le=86400,
        description="任务超时秒数。为 null 时使用服务端默认值（1800 秒）。",
        examples=[1800.0],
    )
    max_cost: Optional[float] = Field(
        default=None, ge=0,
        description="最大成本（美元）。null 表示不限制。", examples=[None])
    max_tokens: Optional[int] = Field(
        default=None, ge=1000,
        description="最大 token 预算。null 表示不限制。", examples=[65000])


class SessionCreate(BaseModel):
    label: str = Field(
        default="", max_length=256,
        description="会话标签（如 sprint-1）", examples=["sprint-1"])


# ---------------- 响应模型 ----------------

class TokenUser(BaseModel):
    id: int = Field(..., examples=[1])
    name: str = Field(..., examples=["admin"])
    role: Role = Field(..., examples=["admin"])


class TokenResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "access_token": "as_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "token_type": "bearer",
            "user": {"id": 1, "name": "admin", "role": "admin"},
        }
    })

    access_token: str = Field(..., description="访问凭证（即 API Key 明文）")
    token_type: str = Field(default="bearer", description="固定为 bearer")
    user: TokenUser


class UserOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"id": 2, "name": "zhang_san", "role": "developer"},
    })

    id: int
    name: str
    role: Role


class MeOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"id": 1, "name": "admin", "role": "admin"},
    })

    id: int
    name: str
    role: Role


class UserWithKey(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user": {"id": 2, "name": "zhang_san", "role": "developer"},
            "api_key": "as_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "note": "请立即保存 api_key（仅此一次明文展示）",
        }
    })

    user: UserOut
    api_key: str = Field(
        ..., description="明文 API Key，仅在创建时展示一次，后续不可查询")
    note: str


class ApiKeyIssued(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user_id": 2,
            "api_key": "as_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "note": "请立即保存 api_key（仅此一次明文展示）",
        }
    })

    user_id: int
    api_key: str = Field(
        ..., description="明文 API Key，仅在签发时展示一次，后续不可查询")
    note: str


class UserListItem(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": 1, "name": "admin", "role": "admin",
            "created_at": "2026-08-22T00:00:00+00:00",
        }
    })

    id: int
    name: str
    role: Role
    created_at: str


class TaskSubmitOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "task_0123456789abcdef0123456789abcdef",
            "status": "queued",
            "workspace":
                "server_workspaces/user_1/task_0123456789abcdef0123456789abcdef",
        }
    })

    id: str
    status: Literal["queued"]
    workspace: str


class TaskOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "task_0123456789abcdef0123456789abcdef",
            "user_id": 1,
            "instruction": "修复登录空指针",
            "workspace": "server_workspaces/user_1/task_xxx",
            "config_path": "config/agent.yaml",
            "status": "completed",
            "exit_code": 0,
            "error": None,
            "timeout": 1800.0,
            "max_cost": None,
            "max_tokens": None,
            "cancelled": False,
            "created_at": "2026-08-22T00:00:00+00:00",
            "started_at": "2026-08-22T00:00:01+00:00",
            "finished_at": "2026-08-22T00:00:02+00:00",
            "result": {"ok": True, "tokens": 100, "rounds": 2},
        }
    })

    id: str
    user_id: int
    instruction: str
    workspace: str
    config_path: str
    status: str = Field(
        ..., description="任务状态：queued / running / completed / failed / "
        "cancelled / timeout / budget（或 Agent 上报的自定义终态）")
    exit_code: Optional[int]
    error: Optional[str]
    timeout: float
    max_cost: Optional[float]
    max_tokens: Optional[int]
    cancelled: bool
    created_at: str
    started_at: str
    finished_at: str
    result: Dict[str, Any]


class CancelOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "task_0123456789abcdef0123456789abcdef",
            "status": "cancelled",
        }
    })

    id: str
    status: Literal["cancelled"]


class SessionOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "ses_0123456789abcdef0123456789abcdef",
            "label": "sprint-1", "user_id": 2,
            "created_at": "2026-08-22T00:00:00+00:00",
        }
    })

    id: str
    label: str
    user_id: int
    created_at: str


class AuditOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": 1, "user_id": 1, "action": "task_submit",
            "detail": "task=task_xxx timeout=1800",
            "created_at": "2026-08-22T00:00:00+00:00",
        }
    })

    id: int
    user_id: Optional[int]
    action: str
    detail: str
    created_at: str


class HealthOut(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"ok": True, "tasks_running": 1},
    })

    ok: bool
    tasks_running: int
