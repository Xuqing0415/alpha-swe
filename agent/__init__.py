"""Alpha-SWE Agent 核心包 —— 按设计文档重构的最小可扩展实现。

分层结构:
    core/     异步主循环、任务 DAG、调度器、状态机
    planner/  任务拆分（LLM / 回退）
    prompt/   动态 Prompt 拼接（Jinja2 模板）
    parser/   输出解析（JSON 代码块 / 正则回退 / 重试反馈）
    tools/    统一 Tool 接口 + Terminal / FileIO / 注册管理器
    memory/   长期记忆存储接口与 SQLite 实现
    context/  上下文管理（技能/插件激活、自动压缩）
    sandbox/  路径/命令安全策略（Docker 沙箱预留）
"""
from agent.config import AgentConfig, AppConfig, load_config

__all__ = ["AgentConfig", "AppConfig", "load_config"]
__version__ = "0.1.0"