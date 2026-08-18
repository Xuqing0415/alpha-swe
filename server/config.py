# -*- coding: utf-8 -*-
"""服务配置：环境变量优先，缺省可本地直接运行。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

ENV_PREFIX = "ASWE_"


class ServerConfig(BaseModel):
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    db_path: str = Field(default="server.db")
    workspace_root: str = Field(default="./server_workspaces")
    config_path: str = Field(default="config/agent.yaml")
    max_concurrency: int = Field(default=2)
    default_timeout: float = Field(default=1800.0)
    default_max_cost: Optional[float] = Field(default=None)
    default_max_tokens: Optional[int] = Field(default=None)
    docker: bool = Field(default=False)
    # 启动时若不存在管理员，用该 Key 创建（不设置则自动生成并打印）
    admin_api_key: Optional[str] = Field(default=None)
    # 运行 Agent 的 Python 解释器（默认当前进程的 python）
    agent_python: Optional[str] = Field(default=None)
    log_level: str = Field(default="INFO")

    @classmethod
    def from_env(cls, **overrides) -> "ServerConfig":
        data = {}
        for name in cls.model_fields:
            env_key = ENV_PREFIX + name.upper()
            if env_key in os.environ:
                data[name] = os.environ[env_key]
        data.update(overrides)
        return cls(**data)

    def ensure_dirs(self) -> None:
        Path(self.workspace_root).mkdir(parents=True, exist_ok=True)
        db = Path(self.db_path)
        if str(db.parent) not in ("", "."):
            db.parent.mkdir(parents=True, exist_ok=True)
