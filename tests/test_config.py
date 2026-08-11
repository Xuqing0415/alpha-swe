"""配置加载测试。"""
from pathlib import Path

import yaml

from agent.config import (AppConfig, CONFIG_FILE, ToolsConfig,
                        load_config, load_mcp_config)

WS_ROOT = Path(__file__).resolve().parent.parent / "test_workspace"


def test_defaults_when_file_missing():
    cfg = load_config(str(WS_ROOT / "missing_config.yaml"))
    assert cfg.agent.max_rounds == 30
    assert cfg.tools.terminal_execute.enabled is True
    assert cfg.llm.provider == "mock"


def test_load_project_config():
    cfg = load_config()  # 读取 config/agent.yaml
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    # 与 YAML 内容对比，确保解析器忠实地读取了用户配置（不硬编码 provider）
    assert cfg.llm.provider == (raw.get("llm") or {}).get("provider", "mock")
    assert cfg.sandbox.workspace.endswith("workspace")
    assert cfg.agent.max_retries == 3


def test_from_dict_overrides():
    cfg = AppConfig.from_dict({
        "agent": {"max_rounds": 5, "max_retries": 1},
        "tools": {"terminal_execute": {"enabled": False, "timeout": 10}},
    })
    assert cfg.agent.max_rounds == 5
    assert cfg.agent.max_retries == 1
    assert cfg.tools.terminal_execute.enabled is False
    assert cfg.tools.terminal_execute.timeout == 10
    # 未提供的字段保持默认
    assert cfg.sandbox.docker_enabled is False


def test_tools_model_dump_keys():
    cfg = ToolsConfig()
    raw = cfg.model_dump()
    assert set(raw.keys()) >= {"terminal_execute", "file_ops", "file_search"}
    assert raw["terminal_execute"]["enabled"] is True


def test_load_mcp_config():
    mcp = load_mcp_config()
    names = [s.name for s in mcp.mcp_servers]
    # github / custom-knowledge 在默认配置中已注释（需外网 GITHUB_TOKEN / 本地 SSE 服务），
    # 避免启动时报错噪音；本地自研服务器应保留。
    assert "github" not in names
    assert "custom-knowledge" not in names
    assert "knowledge-base" in names
    assert "issue-tracker" in names