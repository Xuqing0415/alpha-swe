"""实时指标注册表 —— token/工具/任务/压缩等计数器与采样，支持告警。

MetricsRegistry.snapshot() 供 TUI 监控视图渲染；alerts() 按阈值给出醒目提示：
- token 速率过快（token_rate > alert_token_rate）；
- 工具连续失败 >= alert_consecutive_failures 次；
- 主循环轮次接近上限（rounds >= max_rounds * alert_round_ratio）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

DEFAULT_ALERT_TOKEN_RATE = 5000.0      # tokens/秒
DEFAULT_ALERT_CONSECUTIVE_FAILURES = 3
DEFAULT_ALERT_ROUND_RATIO = 0.9


class MetricsRegistry:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._counters: Dict[str, float] = {}
        self._gauges: Dict[str, Any] = {}
        self._samples: Dict[str, List[tuple]] = {}
        self._started = time.time()
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    # ---- 写入 ----
    def inc(self, name: str, value: float = 1.0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def add(self, name: str, value: float) -> None:
        self.inc(name, value)

    def set(self, name: str, value: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._gauges[name] = value

    def sample(self, name: str, value: float, max_points: int = 60) -> None:
        """时间序列采样（如内存使用趋势）。"""
        if not self.enabled:
            return
        with self._lock:
            points = self._samples.setdefault(name, [])
            points.append((time.time(), value))
            if len(points) > max_points:
                del points[: len(points) - max_points]

    def record_token_usage(self, delta: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters["token_usage"] = self._counters.get("token_usage", 0.0) + delta

    def record_tool_result(self, success: bool) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._counters["tool_calls"] = self._counters.get("tool_calls", 0.0) + 1
            self._consecutive_failures = 0 if success else self._consecutive_failures + 1
            if not success:
                self._counters["tool_failures"] = self._counters.get("tool_failures", 0.0) + 1

    # ---- 读取 ----
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            elapsed = max(now - self._started, 0.001)
            counters = dict(self._counters)
            tool_calls = counters.get("tool_calls", 0.0)
            failures = counters.get("tool_failures", 0.0)
            return {
                "counters": counters,
                "gauges": dict(self._gauges),
                "samples": {k: list(v) for k, v in self._samples.items()},
                "derived": {
                    "elapsed_s": round(elapsed, 1),
                    "token_total": round(counters.get("token_usage", 0.0)),
                    "token_rate": round(counters.get("token_usage", 0.0) / elapsed, 1),
                    "tool_calls": int(tool_calls),
                    "tool_success_rate": round(
                        (tool_calls - failures) / tool_calls, 3) if tool_calls else None,
                    "consecutive_failures": self._consecutive_failures,
                    "active_tasks": self._gauges.get("active_tasks", 0),
                    "phase": self._gauges.get("phase", ""),
                    "rounds": self._gauges.get("rounds", 0),
                    "compressions": int(counters.get("compressions", 0.0)),
                    "retries": int(counters.get("retries", 0.0)),
                    "interrupts": int(counters.get("interrupts", 0.0)),
                    "llm_calls": int(counters.get("llm_calls", 0.0)),
                },
            }

    def alerts(self, max_rounds: int = 30,
               token_rate: float = DEFAULT_ALERT_TOKEN_RATE,
               consecutive_failures: int = DEFAULT_ALERT_CONSECUTIVE_FAILURES,
               round_ratio: float = DEFAULT_ALERT_ROUND_RATIO) -> List[str]:
        derived = self.snapshot()["derived"]
        out: List[str] = []
        if derived["token_rate"] > token_rate:
            out.append(f"token 消耗过快: {derived['token_rate']:.0f}/s > {token_rate:.0f}/s")
        if derived["consecutive_failures"] >= consecutive_failures:
            out.append(f"工具连续失败 {derived['consecutive_failures']} 次")
        if max_rounds and derived["rounds"] >= max_rounds * round_ratio:
            out.append(f"循环轮次接近上限 {derived['rounds']}/{max_rounds}")
        return out

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._samples.clear()
            self._consecutive_failures = 0
            self._started = time.time()


__all__ = ["MetricsRegistry"]
