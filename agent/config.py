"""全局配置 —— Pydantic + YAML，覆盖 config/agent.yaml 与 config/mcp.yaml。

对应设计第 15 节「配置管理 | YAML + Pydantic」。
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "agent.yaml"
DEFAULT_MCP_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp.yaml"
DEFAULT_TEAM_FILE = Path(__file__).resolve().parent.parent / "config" / "team.yaml"

logger = logging.getLogger("alpha-swe.config")

# 配置加载降级记录（模块级注册表）：每发生一次降级追加一条，
# 供 AgentLoop 在 run() 时写入决策日志，让 TUI / analyze_decisions 可见。
CONFIG_FALLBACKS: List[Dict[str, str]] = []


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
    # 网络细粒度策略（本地工具层生效，Docker 用 network_mode）：
    # deny（默认，全部网络命令拦截）| allowlist（放行 network_allowed_commands）| allow（全局放行）
    network_policy: str = "deny"
    network_allowed_commands: List[str] = Field(default_factory=lambda: [
        "pip install", "pip3 install", "pip download", "apt-get", "apt ",
        "npm install", "pnpm install", "yarn add",
    ])
    fake_network: bool = False  # 假网络模式：curl/wget 返回预设响应，不产生真实请求
    fake_network_responses: Dict[str, str] = Field(default_factory=dict)  # URL 前缀 -> 响应体
    # 文件系统保护：删除/写入这些路径片段需要额外确认（默认 .git、配置文件）
    protected_paths: List[str] = Field(default_factory=lambda: [
        ".git", "config/agent.yaml", "config/mcp.yaml", "config/team.yaml",
        "*.lock", "package-lock.json", "poetry.lock", "Pipfile.lock",
    ])
    audit_dir: str = "./logs/audit"  # 文件操作审计日志（before/after diff，支持回滚）
    # 资源监控与熔断：命令内存超过阈值自动 kill（psutil）
    resource_monitor: bool = False
    memory_limit_mb: float = 512.0
    poll_interval: float = 0.2
    # Docker 容器生命周期（docker-py，docker_enabled=True 时由 DockerSandbox 使用）
    workdir: str = "/workspace"             # 容器内工作目录（卷挂载目标）
    volume_mode: str = "rw"                 # 工作区卷挂载模式（rw / ro）
    snapshot_prefix: str = "alphaswe/snap"  # docker commit 快照镜像前缀
    auto_rollback: bool = True              # 任务失败时自动回滚到任务前快照
    container_name: str = ""                # 稳定容器名（空 = docker 自动生成）

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
    embedding_model_path: str = ""  # 本地模型目录（优先于 embedding_model，离线加载）
    embedding_offline: bool = True  # 离线模式：禁止 HF 联网下载（local_files_only）
    embedding_base_url: str = ""
    embedding_api_key_env: str = "OPENAI_API_KEY"
    hybrid_weight_vector: float = 0.6
    max_code_index_chars: int = 2000
    auto_experience: bool = True  # 任务完成后自动生成经验摘要
    similarity_threshold: float = 0.0  # 检索结果相似度下限（0 = 不过滤）
    dedup_threshold: float = 0.95  # 写入前去重：相似度超过该值只更新计数
    decay_days: float = 30.0  # 记忆超过该天数未引用开始可信度衰减
    decay_factor: float = 0.1  # 每超过一个衰减周期，分数乘以该系数
    counter_example_penalty: float = 0.3  # 反例（negative）检索时的分数惩罚
    top_k_retrieval: Optional[int] = None  # 别名 -> top_k
    collection_name: Optional[str] = None  # 别名 -> collection
    # 主线一 1.3：项目级记忆分层（会话状态 > 项目知识 > 全局经验）
    layered: bool = True                  # 三层记忆开关
    global_dir: str = "~/.swe-agent/memory"  # 全局经验目录（跨项目）
    promotion_threshold: int = 3          # 项目经验在 N 个不同项目被应用后晋升全局

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
    timeout: float = 120.0     # 单次 LLM 调用超时秒数（收敛期 P0：超时管控）
    max_retries: int = 2       # 瞬时失败指数退避重试次数（耗尽抛 LLMServiceError）


class WorkerRoleConfig(BaseModel):
    """多 Agent 团队中的 Worker 角色定义（对应设计第 8 节）。"""
    name: str
    description: str = ""
    system_prompt: str = ""  # 角色提示，拼在系统 Prompt 最前
    tools: List[str] = Field(default_factory=lambda: ["terminal_execute", "file_ops"])
    max_rounds: int = 10
    read_only: bool = False  # 只读角色：file 只读 + terminal 白名单（如 reviewer）
    # 主线二 2.1：动态角色分配——LLM 规划失败/未给角色时的关键词路由回退
    routing_keywords: List[str] = Field(default_factory=list)


class TeamConfig(BaseModel):
    """多 Agent 团队配置（可插拔角色）。"""
    roles: List[WorkerRoleConfig] = Field(default_factory=list)
    concurrency: int = 1
    max_review_retries: int = 2
    message_timeout: float = 60.0  # 团队消息默认超时秒数（写进 Message.timeout）


class PlannerConfig(BaseModel):
    """任务规划配置（对应设计 3.3 节）。"""
    max_subtasks: int = 5
    allow_parallel: bool = True
    split_threshold_complexity: float = 0.0  # 0 = 总是拆分（保持旧行为）
    # 进阶 2.3：预算估算基准（按任务复杂度线性缩放）
    budget_token_base: int = 10000
    budget_time_base: float = 300.0



class ContextConfig(BaseModel):
    """上下文管理配置（对应设计 11 节）。"""
    max_tokens: int = 8000
    compression_threshold: float = 0.8
    compression_method: str = "summary"  # summary | vector_retrieval
    archive_dir: str = "./logs/archives"  # 长工具输出归档目录
    output_truncate: int = 2000  # 工具输出超过该长度触发输出压缩
    # 分级压缩压力阈值（当前 token / max_tokens）：
    # light_threshold 以下不压缩；light 只压工具输出；
    # medium 压缩旧对话（保留决策点）；heavy 递归摘要。
    light_threshold: float = 0.8
    medium_threshold: float = 0.9
    heavy_threshold: float = 1.05



class PluginConfig(BaseModel):
    """插件动态注入配置（对应设计第 10 节：文件类型/关键词/项目依赖触发）。"""
    enabled: bool = True
    dir: str = "./plugins"        # 插件目录（Markdown + YAML front-matter，mtime 热加载）
    max_active: int = 5            # 单次最多激活插件数（按 priority 截断）


class SkillConfig(BaseModel):
    """技能工作流配置（对应设计第 10.2 节：YAML 技能库展开为子任务 DAG）。"""
    enabled: bool = False  # 默认关闭；由 config/agent.yaml 的 skills.enabled 显式开启
    dir: str = "./skills/workflows"  # YAML 技能库目录（热加载）
    workflow_enabled: bool = True  # 技能命中时展开为子任务序列，替代 LLM 规划
    max_active: int = 3            # 单次最多激活技能数
    allow_fallback: bool = True    # 步骤失败允许按 fallback 回退重试
    # 阶段二 2.1：技能注册表（JSON，按技能名合并 requires/permissions/params）
    registry_file: str = "./skills/skill_manifest.json"
    usage_log: str = "./logs/skill_usage.jsonl"  # 技能使用/成败历史（版本管理）
    # 阶段二 2.4：工作流激活是否要求任务意图（keywords）命中；
    # False 时 file_ext/project_dep/project_file 单独命中也会自动展开（可能误触发）。
    # 注意：True 时 file_ext 仅作语言范围过滤，不单独激活工作流。
    require_task_intent: bool = True


class MCPOptions(BaseModel):
    """MCP 运行时选项。"""
    enabled: bool = True
    connect_timeout: float = 8.0
    tool_timeout: float = 30.0
    max_resources_per_run: int = 3
    reconnect_attempts: int = 2       # 连接失败后的自动重连次数
    reconnect_delay: float = 1.0      # 重连间隔秒数
    resource_cache_ttl: float = 60.0  # MCP 资源缓存 TTL 秒数（0 = 不缓存）


class AgentConfig(BaseModel):
    max_rounds: int = 30
    max_loop_iterations: Optional[int] = None  # 覆盖 max_rounds（用户方案字段）
    max_retries: int = 3
    max_timeout_strikes: int = 3  # 同一命令/工具连续超时熔断阈值（方案 2.1）
    snapshot_dir: str = "./logs/snapshots"  # 任务快照目录（方案 1.3 断点续跑）
    snapshot_enabled: bool = True
    snapshot_keep: int = 5  # 保留最近 N 个快照
    token_threshold: float = 0.8
    max_token_limit: int = 100_000
    keep_recent_rounds: int = 3
    # 内存事件列表上限：超出丢弃最旧事件（超长会话防泄漏，档案保留最近 N 条）
    max_events: int = 10000
    max_concurrency: int = 1
    # 进阶 2.1：任务队列与动态抢占——高优先级任务就绪时在安全点
    # 暂停低优先级任务（PAUSED），高优先级完成后自动恢复
    preemption_enabled: bool = True
    # 进阶 2.3：资源预算管理——每任务 token/时间预算（秒）
    budget_enabled: bool = True
    default_token_budget: int = 10000
    default_time_budget: float = 300.0
    budget_warn_ratio: float = 0.8       # 达到该比例发出告警
    budget_borrow_enabled: bool = True   # 高优先级可借用低优先级未用预算
    parallel_tool_calls: bool = True  # 单次响应多工具是否并行执行
    require_confirmation: List[str] = Field(default_factory=lambda: [
        "file_write", "terminal:rm", "terminal:git push",
        "git_commit", "git_push", "git_branch_delete",
    ])
    auto_approve: List[str] = Field(default_factory=lambda: [
        "file_read", "terminal:ls", "terminal:cat",
        "git_status", "git_diff", "git_log", "git_branch",
    ])
    trace_dir: str = "./logs/traces"        # OTel 风格 span JSONL 导出目录
    trace_enabled: bool = True              # 分布式追踪开关
    session_archive_dir: str = "./logs/sessions"  # 会话档案目录（事件+span+决策+指标）
    archive_enabled: bool = True            # 会话档案写入开关
    metrics_enabled: bool = True            # 实时指标注册表开关
    # 进阶 1.1：决策理由显式化——开启后强制 tool_call/final_answer 携带
    # reasoning 字段（为什么这么做），缺失则由 Parser 拒绝并要求重试
    require_reasoning: bool = False
    # 进阶 1.2：反事实分析——任务失败后归因并写入长期记忆，相似任务
    # 检索命中时以 [反事实警告] 注入 Prompt
    counterfactual_enabled: bool = True
    # 进阶 3.1：自动测试生成——代码写入且无测试覆盖时生成 test_*.py
    auto_testgen: bool = True
    auto_testgen_verify: bool = False  # 生成后立即运行 pytest 验证（子进程，默认关）
    # 进阶 3.3：回归检测——代码写入后自动运行受影响测试，测不过就停
    regression_check_enabled: bool = True
    regression_timeout: float = 60.0
    # 进阶 3.2：变异测试——验证自动生成的测试确实能捕获变异
    mutation_check_enabled: bool = True
    mutation_max_ops: int = 8
    mutation_target_rate: float = 0.8
    # 第 9 节：Web 观测面板（python -m tui --web，或置 True 自动开启）
    web_panel_enabled: bool = False
    web_panel_host: str = "127.0.0.1"
    web_panel_port: int = 8765
    # 第 10 节：OpenTelemetry/Jaeger 导出与结构化 JSON 日志
    otel_enabled: bool = False              # 是否启用 OTLP 导出
    otel_endpoint: str = ""                 # OTLP/HTTP 基址，如 http://127.0.0.1:4318
    otel_service_name: str = "alpha-swe"
    structured_log_dir: str = ""            # 空 = 关闭 JSONL 结构化日志
    # 主线一 1.1/1.2：项目状态感知与会话间工作流连续性（.swe-agent/）
    state_tracker_enabled: bool = True       # 项目状态快照与跨会话差异注入
    workspace_context_enabled: bool = True   # 会话间工作流续接（next_session_hint）


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
    plugin: PluginConfig = Field(default_factory=PluginConfig)
    skills: SkillConfig = Field(default_factory=SkillConfig)
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
    """三层降级加载配置，保证任意情况下都能得到可运行配置。

    第一层：用户显式指定的路径；
    第二层：项目根目录 config/agent.yaml；
    第三层：内置默认值。
    每一层失败都会记录 WARN 日志与 CONFIG_FALLBACKS（供决策日志汇总）。
    """
    candidates: List[Path] = []
    if path:
        candidates.append(Path(path))
    candidates.append(CONFIG_FILE)
    for cfg_path in candidates:
        data = _read_yaml_safe(cfg_path, "agent")
        if data is not None:
            return AppConfig.from_dict(data)
    return AppConfig()


def load_mcp_config(path: Optional[str] = None) -> MCPConfig:
    """加载 MCP 服务器清单；文件缺失/损坏时降级为空清单（不崩溃）。"""
    cfg_path = Path(path) if path else DEFAULT_MCP_FILE
    data = _read_yaml_safe(cfg_path, "mcp")
    if data is None:
        return MCPConfig()
    return MCPConfig(**data)


def load_team_config(path: Optional[str] = None) -> TeamConfig:
    """加载多 Agent 团队配置（config/team.yaml）；失败时降级为默认团队。"""
    cfg_path = Path(path) if path else DEFAULT_TEAM_FILE
    data = _read_yaml_safe(cfg_path, "team")
    if data is None:
        return TeamConfig()
    return TeamConfig(**data)


def _read_yaml_safe(cfg_path: Path, module: str) -> Optional[Dict[str, Any]]:
    """读取并解析 YAML 配置；任何异常都降级返回 None 并记录原因。

    覆盖 FileNotFoundError / yaml.YAMLError / PermissionError 及未知异常，
    保证「配置坏了也不能让 Agent 崩」。
    """
    def _fallback(reason: str) -> None:
        CONFIG_FALLBACKS.append({
            "module": module,
            "path": str(cfg_path),
            "reason": reason,
        })
        logger.warning("%s 配置降级: %s（%s）", module, cfg_path, reason)

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        _fallback("文件不存在")
        return None
    except PermissionError as e:
        _fallback(f"无读取权限: {e}")
        return None
    except yaml.YAMLError as e:
        _fallback(f"YAML 解析失败: {str(e)[:120]}")
        return None
    except OSError as e:
        _fallback(f"IO 错误: {e}")
        return None
    except Exception as e:
        _fallback(f"未知异常: {str(e)[:120]}")
        return None
    if not isinstance(data, dict):
        _fallback(f"顶层必须是映射，实际为 {type(data).__name__}")
        return None
    return data
