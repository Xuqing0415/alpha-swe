# -*- coding: utf-8 -*-
"""主线三 3.1：能力画像——跨会话记录各能力维度表现，时间衰减加权。

分数 = 衰减后的成功权重 / 衰减后的尝试权重（EWMA），近期表现权重更高；
每条任务记录按「任务类型 + 关键词」映射到能力维度（代码理解/代码修改/
调试定位/测试编写/文档编写/架构设计/性能优化/安全修复）。

能力画像持久化到全局目录（~/.swe-agent/capability.json），规划时以
[能力画像] 区块注入 Prompt，弱项维度提示 Agent 更谨慎。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.selfimprove.capability")

CAPABILITY_DIMENSIONS: Dict[str, str] = {
    "code_understand": "代码理解",
    "code_modify": "代码修改",
    "debug": "调试定位",
    "test_writing": "测试编写",
    "documentation": "文档编写",
    "architecture": "架构设计",
    "performance": "性能优化",
    "security": "安全修复",
}

# 任务类型 -> 能力维度（classify_task_type: fix/add/refactor/test/general）
_TASK_DIMENSION_MAP: Dict[str, List[str]] = {
    "fix": ["debug", "code_modify"],
    "add": ["code_modify"],
    "refactor": ["code_modify", "code_understand"],
    "test": ["test_writing"],
    "general": ["code_understand"],
}

# 关键词 -> 能力维度（覆盖 classify_task_type 未覆盖的维度）
_DIMENSION_KEYWORDS: Dict[str, List[str]] = {
    "documentation": ["文档", "readme", "注释", "使用说明", "document"],
    "architecture": ["架构", "设计", "api", "接口设计", "architect"],
    "performance": ["性能", "缓存", "performance", "benchmark"],
    "security": ["安全", "漏洞", "注入", "越权", "密钥", "security", "vuln"],
}

# 每次更新应用一次衰减：尝试权重收敛到 1/(1-DECAY)，旧事件指数级淡出
_DECAY = 0.9
_HISTORY_LIMIT = 20  # 保留最近 N 次结果，用于趋势告警
_WEAK_THRESHOLD = 0.6   # 成功率低于该值视为弱项
_TREND_WINDOW = 5       # 近 N 次成功率 vs 整体
_TREND_GAP = 0.2        # 下降超过该差距触发告警


def _dimensions_for(instruction: str) -> List[str]:
    """按任务类型 + 关键词推导能力维度。"""
    from agent.memory.store import classify_task_type

    dims = set(_TASK_DIMENSION_MAP.get(classify_task_type(instruction),
                                       ["code_understand"]))
    text = str(instruction or "").lower()
    for dim, kws in _DIMENSION_KEYWORDS.items():
        if any(k in text for k in kws):
            dims.add(dim)
    return sorted(dims)


class CapabilityProfile:
    """能力画像：EWMA 分数 + 近况历史 + 落盘持久化。"""

    def __init__(self, path: Optional[str] = None,
                 enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser() if path else None
        self._data: Dict[str, Dict[str, Any]] = self._load()

    # ---- 持久化 ----
    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.path is None or not self.enabled:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if self.path is None or not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            logger.warning("能力画像落盘失败: %s", e)

    # ---- 更新 ----
    def record(self, instruction: str, ok: bool) -> List[str]:
        """记录一次任务结果，返回受影响的能力维度。"""
        if not self.enabled:
            return []
        dims = _dimensions_for(instruction)
        for dim in dims:
            cur = self._data.setdefault(
                dim, {"attempts": 0.0, "successes": 0.0, "history": []})
            cur["attempts"] = cur["attempts"] * _DECAY + 1.0
            cur["successes"] = cur["successes"] * _DECAY + (1.0 if ok else 0.0)
            cur["score"] = (round(cur["successes"] / cur["attempts"], 4)
                            if cur["attempts"] > 0 else 0.0)
            hist = cur.setdefault("history", [])
            hist.append(bool(ok))
            del hist[: max(0, len(hist) - _HISTORY_LIMIT)]
            cur["samples"] = len(hist)
        self._save()
        return dims

    # ---- 读取 ----
    def score(self, dim: str) -> float:
        cur = self._data.get(dim) or {}
        return float(cur.get("score", 0.0) or 0.0)

    def profile_text(self, top: int = 3) -> str:
        """生成注入 Prompt 的画像摘要：突出弱项与改进建议。"""
        if not self.enabled or not self._data:
            return ""
        weak = []
        for dim, label in CAPABILITY_DIMENSIONS.items():
            cur = self._data.get(dim)
            if not cur or (cur.get("samples") or 0) < 2:
                continue
            score = float(cur.get("score", 0.0) or 0.0)
            if score < _WEAK_THRESHOLD:
                weak.append(f"- {label}偏弱（成功率 {score:.0%}），请在该环节更谨慎并主动验证")
        if not weak:
            return ""
        body = "\n".join(weak[:top])
        return (f"[能力画像]\n{body}\n"
                "（画像来自历史会话统计，仅提示风险，不改变任务要求）")

    def suggestions(self) -> List[str]:
        """弱项改进建议（供 TUI / 报告展示）。"""
        out = []
        for dim, label in CAPABILITY_DIMENSIONS.items():
            cur = self._data.get(dim)
            if not cur or (cur.get("samples") or 0) < 2:
                continue
            score = float(cur.get("score", 0.0) or 0.0)
            if score < _WEAK_THRESHOLD:
                out.append(f"{label}（成功率 {score:.0%}）：建议在相关任务中增加验证步骤")
        return out

    def trend_warnings(self) -> List[str]:
        """能力下降告警：近 N 次成功率明显低于整体。"""
        warns = []
        for dim, cur in self._data.items():
            hist = cur.get("history") or []
            if len(hist) < _TREND_WINDOW:
                continue
            recent = sum(1 for x in hist[-_TREND_WINDOW:] if x) / _TREND_WINDOW
            overall = float(cur.get("score", 0.0) or 0.0)
            if overall >= 0.3 and recent < overall - _TREND_GAP:
                warns.append(
                    f"{CAPABILITY_DIMENSIONS.get(dim, dim)} 能力下降"
                    f"（近 {_TREND_WINDOW} 次 {recent:.0%} vs 整体 {overall:.0%}）")
        return warns

    def summary(self) -> Dict[str, Any]:
        return {
            dim: {"score": self.score(dim), "label": label}
            for dim, label in CAPABILITY_DIMENSIONS.items()
            if dim in self._data
        }

    def close(self) -> None:
        self._save()
