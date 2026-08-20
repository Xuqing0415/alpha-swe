"""配置驱动的决策点追踪 —— 记录每个配置键在运行时的实际决策。

每条记录形如 {name, config_key, config_value, decision, timestamp}，追加写入 JSONL；
同时保留内存副本供 summary() / analyze 使用。
路径优先级：构造参数 > 环境变量 DECISION_LOG_PATH > 默认 decision_log.jsonl。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.decision")

DEFAULT_LOG_PATH = Path("decision_log.jsonl")
ENV_LOG_PATH = "DECISION_LOG_PATH"


@dataclass
class DecisionPoint:
    """一次配置驱动的决策。"""
    name: str
    config_key: str
    config_value: Any
    decision: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "config_key": self.config_key,
            "config_value": repr(self.config_value),
            "decision": self.decision,
            "timestamp": self.timestamp,
        }


class DecisionLogger:
    """线程安全的决策日志（内存 + 可选 JSONL 文件）。

    enabled=False 时仅计数丢弃，方便测试关闭日志。
    """

    def __init__(self, log_path: Optional[str] = None, enabled: bool = True,
                 max_memory_records: Optional[int] = 1000):
        # 环境变量用于 A/B 对比时覆盖日志路径，优先级最高
        env_path = os.environ.get(ENV_LOG_PATH)
        if env_path:
            log_path = env_path
        self.log_path: Optional[Path] = Path(log_path) if log_path else None
        self.enabled = enabled
        self.decisions: List[DecisionPoint] = []
        self.max_memory_records = max_memory_records
        self._lock = threading.Lock()

    def record(self, name: str, config_key: str, config_value: Any,
               decision: str) -> None:
        """记录一个决策点；写文件失败不阻塞主流程。"""
        if not self.enabled:
            return
        dp = DecisionPoint(name, config_key, config_value, decision)
        with self._lock:
            self.decisions.append(dp)
            if self.log_path is not None:
                self._append(dp)
                # 收敛期 P2：内存只保留最近 N 条（更早的已落盘 JSONL），
                # 避免超长会话下决策日志无限累积；未配置落盘时不做裁剪。
                if self.max_memory_records is not None:
                    overflow = len(self.decisions) - self.max_memory_records
                    if overflow > 0:
                        del self.decisions[:overflow]

    def _append(self, dp: DecisionPoint) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dp.to_dict(), ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as e:
            # OSError=磁盘/权限；TypeError/ValueError=决策值不可序列化
            logger.warning("决策日志写入失败 %s: %s", self.log_path, e)

    def records(self) -> List[Dict[str, Any]]:
        """全部决策记录（dict 列表）。"""
        with self._lock:
            return [dp.to_dict() for dp in self.decisions]

    def find(self, config_key: Optional[str] = None,
             name: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dp.to_dict() for dp in self.decisions
                if (config_key is None or dp.config_key == config_key)
                and (name is None or dp.name == name)
            ]

    def summary(self) -> Dict[str, List[str]]:
        """按配置项聚合所有决策，方便检查配置是否生效。"""
        result: Dict[str, List[str]] = {}
        with self._lock:
            for dp in self.decisions:
                result.setdefault(dp.config_key, []).append(dp.decision)
        return result

    def clear(self) -> None:
        with self._lock:
            self.decisions.clear()


__all__ = ["DecisionLogger", "DecisionPoint", "DEFAULT_LOG_PATH", "ENV_LOG_PATH"]