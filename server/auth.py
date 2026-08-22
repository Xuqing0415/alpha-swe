# -*- coding: utf-8 -*-
"""API Key 认证与角色权限（方向三·阶段二）。

- Bearer Token 即 API Key（服务端只存 SHA-256 哈希）；
- 角色：admin(3) / developer(2) / observer(1)；
- 权限按角色等级判定，工具级权限由 Agent 沙箱配置控制。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from server.store import ROLE_LEVEL, Store, User

logger = logging.getLogger("server.auth")

BEARER_PREFIX = "Bearer "


def parse_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    if authorization.startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX):].strip()
    return authorization.strip()


def authenticate(store: Store, authorization: Optional[str],
                 audit: bool = True) -> Optional[User]:
    key = parse_bearer(authorization)
    if not key:
        return None
    user = store.authenticate(key)
    if user is None and audit:
        store.audit(None, "auth_failed", f"无效 API Key（前缀 {key[:8]}...）")
    return user


def get_store(request: Request) -> Store:
    return request.app.state.store


def current_user(
    request: Request,
    authorization: Optional[str] = Header(
        default=None, include_in_schema=False),
) -> User:
    store: Store = request.app.state.store
    user = authenticate(store, authorization)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "无效或缺失 API Key",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def require_role(min_role: str) -> Callable:
    """返回 FastAPI 依赖：要求当前用户角色 >= min_role。"""

    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_LEVEL.get(user.role, 0) < ROLE_LEVEL.get(min_role, 0):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"需要角色 {min_role} 及以上")
        return user

    return dep
