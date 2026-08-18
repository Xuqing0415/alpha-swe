# -*- coding: utf-8 -*-
"""Alpha-SWE 产品化服务层（方向三）。

- server.main     FastAPI 应用工厂与路由
- server.store    SQLite 存储（用户/API Key/任务/会话/审计）
- server.auth     API Key 认证与角色权限
- server.tasks    异步任务队列（子进程运行 Agent）+ 事件总线
- server.events   SSE 事件流
- server.config   服务配置（环境变量可覆盖）
"""

__version__ = "0.1.0"
