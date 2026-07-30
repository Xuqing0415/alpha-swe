"""第七关：MCP (Model Context Protocol) 自由配置
解析 config.yaml，用户可自由开关工具、配置记忆/沙箱参数。
"""
import os
import logging
import json
from typing import Dict, Any, Optional

logger = logging.getLogger("alpha-swe.mcp_config")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

DEFAULT_CONFIG = {
    "tools": {
        "terminal_execute": {"enabled": True, "timeout": 30},
        "file_ops": {"enabled": True},
        "file_search": {"enabled": True},
        "git": {"enabled": False},
        "jira": {"enabled": False},
    },
    "memory": {
        "db_path": "memory.db",
        "max_entities": 1000,
        "auto_compact_rounds": 5
    },
    "sandbox": {
        "workspace": "/tmp/workspace",
        "allowed_paths": [],
        "blocked_paths": [
            "/etc", "/sys", "/proc", "/boot", "/root",
            "C:\\Windows", "C:\\Windows\\System32"
        ],
        "block_commands": [
            "sudo", "rm -rf /", "mkfs", "dd if="
        ]
    },
    "agent": {
        "max_rounds": 30,
        "token_threshold": 0.8,
        "max_token_limit": 100000,
        "keep_recent_rounds": 3
    },
    "ui": {
        "enabled": True,
        "theme": "dark",
        "refresh_rate_ms": 100
    }
}


class MCPConfigLoader:
    """MCP 配置加载器——支持 YAML/JSON"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config: Dict[str, Any] = DEFAULT_CONFIG.copy()
        self._load()

    def _load(self):
        """加载配置文件"""
        if not os.path.exists(self.config_path):
            logger.warning(f"配置文件不存在，使用默认配置: {self.config_path}")
            self._save_default()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                if self.config_path.endswith(".yaml") or self.config_path.endswith(".yml"):
                    if HAS_YAML:
                        user_config = yaml.safe_load(f)
                    else:
                        logger.warning("yaml 未安装，使用 JSON 解析")
                        user_config = json.load(f)
                elif self.config_path.endswith(".json"):
                    user_config = json.load(f)
                else:
                    # 尝试 YAML 优先
                    try:
                        if HAS_YAML:
                            user_config = yaml.safe_load(f.read())
                        else:
                            f.seek(0)
                            user_config = json.load(f)
                    except Exception:
                        logger.warning("无法解析配置文件，使用默认配置")
                        return

            if user_config:
                self._deep_merge(self.config, user_config)
            logger.info(f"配置加载成功: {self.config_path}")
        except Exception as e:
            logger.error(f"配置加载失败: {e}")

    def _deep_merge(self, base: dict, override: dict):
        """深度合并配置"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _save_default(self):
        """保存默认配置"""
        try:
            content = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)
            if self.config_path.endswith(".yaml") or self.config_path.endswith(".yml"):
                if HAS_YAML:
                    import yaml
                    content = yaml.dump(DEFAULT_CONFIG, allow_unicode=True, default_flow_style=False)
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"默认配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"保存默认配置失败: {e}")

    def load(self) -> dict:
        """获取当前配置"""
        return self.config

    def reload(self) -> dict:
        """重新加载配置"""
        self._load()
        return self.config

    def is_tool_enabled(self, tool_name: str) -> bool:
        """检查工具是否启用"""
        return self.config.get("tools", {}).get(tool_name, {}).get("enabled", True)

    def get_tool_config(self, tool_name: str) -> dict:
        """获取工具配置"""
        return self.config.get("tools", {}).get(tool_name, {})