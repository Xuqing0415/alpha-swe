"""全局配置 —— Pydantic + YAML，覆盖 config/agent.yaml 与 config/mcp.yaml。

对应设计第 15 节「配置管理 | YAML + Pydantic」。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "agent.yaml"
DEFAULT_MCP_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp.yaml"
DEFAULT_TEAM_FILE = Path(__file__).resolve().parent.parent / "config" / "team.yaml"


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
    read_only_root: bool = True  # 容器根文件系统只读（挂载卷可写）
    no_network: bool = True
    network_enabled: Optional[bool] = None  # 显式设置时覆盖 no_network
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    timeout_seconds: int = 300

    @model_validator(mode="before")
    @classmethod
    def _normalize_network(cls, data):
        """network_enabled 与 no_network 互斥归一。"""
        if isinstance(data, dict) and data.get("network_enabled") is not None:
            data = {**data, "no_network": not bool(data["network_enabled"])}
        return data

    @property
    def is_network_enabled(self) -> bool:
        return not self.no_network

    @property
    def network_mode(self) -> str:
        """容器网络模式：bridge / none。"""
        return "bridge" if self.is_network_enabled else "none"


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
    similarity_threshold: float = 0.0  # 检索结果相似度下限（0 = 不过滤）
    top_k_retrieval: Optional[int] = None  # 别名 -> top_k
    collection_name: Optional[str] = None  # 别名 -> collection

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data):
        """归一化用户方案中的别名（collection_name/top_k_retrieval/chromadb）。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("collection_name") and not data.get("collection"):
            data["collection"] = data["collection_name"]
        if data.get("top_k_retrieval") is not None and "top_k" not in data:
            data["top_k"] = data["top_k_retrieval"]
        if data.get("backend") == "chromadb":
            data["backend"] = "chroma"
        return data


class LLMProvider(str, Enum):
    """LLM 提供商（决定系统提示风格与客户端路由）。"""
    MOCK = "mock"
    LITELLM = "litellm"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class LLMConfig(BaseModel):
    provider: LLMProvider = LLMProvider.MOCK  # mock | litellm | openai | anthropic | ollama
    model: str = "mock"
    base_url: str = ""
    api_base: Optional[str] = None  # 别名，兼容 litellm api_base
    api_key_env: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = 2048


class WorkerRoleConfig(BaseModel):
    """多 Agent 团队中的 Worker 角色定义（对应设计第 8 节）。"""
    name: str
    description: str = ""
    system_prompt: str = ""  # 角色提示，拼在系统 Prompt 最前
    tools: List[str] = Field(default_factory=lambda: ["terminal_execute", "file_ops"])
    max_rounds: int = 10


class TeamConfig(BaseModel):
    """多 Agent 团队配置（可插拔角色）。"""
    roles: List[WorkerRoleConfig] = Field(default_factory=list)
    concurrency: int = 1
    max_review_retries: int = 2


class PlannerConfig(BaseModel):
    """任务规划配置（对应设计 3.3 节）。"""
    max_subtasks: int = 5
    allow_parallel: bool = True
    split_threshold_complexity: float = 0.0  # 0 = 总是拆分（保持旧行为）



class ContextConfig(BaseModel):
    """上下文管理配置（对应设计 11 节）。"""
    max_tokens: int = 8000
    compression_threshold: float = 0.8
    compression_method: str = "summary"  # summary | vector_retrieval



class MCPOptions(BaseModel):
    """MCP 运行时选项。"""
    enabled: bool = True
    connect_timeout: float = 8.0
    tool_timeout: float = 30.0
    max_resources_per_run: int = 3


class AgentConfig(BaseModel):
    max_rounds: int = 30
    max_loop_iterations: Optional[int] = None  # 覆盖 max_rounds（用户方案字段）
    max_retries: int = 3
    token_threshold: float = 0.8
    max_token_limit: int = 100_000
    keep_recent_rounds: int = 3
    max_concurrency: int = 1
    parallel_tool_calls: bool = True  # 单次响应多工具是否并行执行
    require_confirmation: List[str] = Field(default_factory=lambda: [
        "file_write", "terminal:rm", "terminal:git push",
    ])
    auto_approve: List[str] = Field(default_factory=lambda: [
        "file_read", "terminal:ls", "terminal:cat",
    ])
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
    team: TeamConfig = Field(default_factory=TeamConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)
    active_plugins: List[str] = Field(default_factory=list)
    active_skills: List[str] = Field(default_factory=list)
    decision_log_path: str = ""  # 空 = 仅内存决策日志

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


def load_team_config(path: Optional[str] = None) -> TeamConfig:
    """加载多 Agent 团队配置（config/team.yaml）。"""
    cfg_path = Path(path) if path else DEFAULT_TEAM_FILE
    if not cfg_path.exists():
        return TeamConfig()
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return TeamConfig(**data)
