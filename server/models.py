# -*- coding: utf-8 -*-
"""API 请求/响应模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TokenRequest(BaseModel):
    api_key: str = Field(..., min_length=3)


class UserCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=128)
    role: str = Field(default="developer")


class ApiKeyCreate(BaseModel):
    user_id: int = Field(..., gt=0)


class TaskCreate(BaseModel):
    instruction: str = Field(..., min_length=3, max_length=20000)
    workspace: Optional[str] = Field(default=None, max_length=2000)
    config_path: Optional[str] = Field(default=None, max_length=2000)
    timeout: Optional[float] = Field(default=None, gt=0, le=86400)
    max_cost: Optional[float] = Field(default=None, ge=0)
    max_tokens: Optional[int] = Field(default=None, ge=1000)


class SessionCreate(BaseModel):
    label: str = Field(default="", max_length=256)
