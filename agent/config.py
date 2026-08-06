"""全局配置 —— Pydantic + YAML，覆盖 config/agent.yaml 与 config/mcp.yaml。

对应设计第 15 节「配置管理 | YAML + Pydantic」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "agent.yaml"
DEFAULT_MCP_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp.yaml"


class ToolEntry(BaseModel):
    """单个工具的开关与默认参数。"""
    enabled: bool = True
    timeout: float = 30.0
    extra: Dict[str, Any] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    terminal_execute: ToolEntry = Field(default_factory=ToolEntry)
    file_ops: ToolEntry = Field(default_factory=ToolEntry)
    file_search: ToolEntry = Field(default_factory=ToolEntry)


class SandboxConfig(BaseModel):
    workspace: str = "./workspace"
    allowed_paths: List[str] = Field(default_factory=list)
    blocked_paths: List[str] = Field(
        default_factory=lambda: [
            "/etc", "/sys", "/proc", "/boot", "/root",
            "C:\\Windows", "C:\\Windows\\System32",
        ]
    )
    block_commands: List[str] = Field(
        default_factory=lambda: ["sudo", "rm -rf /", "mkfs", "dd if="]
    )
    # Docker 沙箱（docker-py）预留配置，默认关闭
    docker_enabled: bool = False
    image: str = "alphaswe/dev:latest"
    read_only: bool = False
    no_network: bool = True


class MemoryConfig(BaseModel):
    backend: str = "auto"  # auto | sqlite | hybrid | chroma | qdrant
    db_path: str = "memory.db"
    max_entities: int = 1000
    top_k: int = 5
    collection: str = "alpha_swe_memories"
    embedder: str = "auto"  # auto | tfidf | sentence-transformers | openai
    embedding_model: str = ""  # 例如 all-MiniLM-L6-v2 / text-embedding-3-small
    embedding_base_url: str = ""
    embedding_api_key_env: str = "OPENAI_API_KEY"
    hybrid_weight_vector: float = 0.6
    max_code_index_chars: int = 2000
    auto_experience: bool = True  # 任务完成后自动生成经验摘要


class LLMConfig(BaseModel):
    provider: str = "mock"  # mock | litellm
    model: str = "mock"
    base_url: str = ""
    api_key_env: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048


class MCPOptions(BaseModel):
    """MCP 运行时选项。"""
    enabled: bool = True
    connect_timeout: float = 8.0
    tool_timeout: float = 30.0
    max_resources_per_run: int = 3


class AgentConfig(BaseModel):
    max_rounds: int = 30
    max_retries: int = 3
    token_threshold: float = 0.8
    max_token_limit: int = 100_000
    keep_recent_rounds: int = 3
    max_concurrency: int = 1
    trace_dir: str = "./logs/traces"


class MCPClientConfig(BaseModel):
    """MCP 服务器配置条目（对应 config/mcp.yaml）。"""
    name: str
    transport: str = "stdio"  # stdio | sse
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)


class MCPConfig(BaseModel):
    mcp_servers: List[MCPClientConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    """聚合配置根节点。"""
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcp: MCPOptions = Field(default_factory=MCPOptions)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        # 只取顶层认识的键，其余忽略，保证 YAML 向前兼容
        known = {k: v for k, v in data.items() if k in cls.model_fields}
        return cls(**known)


def load_config(path: Optional[str] = None) -> AppConfig:
    """从 YAML 加载配置；文件缺失或部分缺失时回退到默认值。"""
    cfg_path = Path(path) if path else CONFIG_FILE
    if not cfg_path.exists():
        return AppConfig()
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.from_dict(data)


def load_mcp_config(path: Optional[str] = None) -> MCPConfig:
    """加载 MCP 服务器清单。"""
    cfg_path = Path(path) if path else DEFAULT_MCP_FILE
    if not cfg_path.exists():
        return MCPConfig()
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return MCPConfig(**data)