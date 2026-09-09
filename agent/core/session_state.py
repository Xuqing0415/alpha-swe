"""会话状态显式生命周期（主线一 1.3C）。

phase-barrier 负责 0-6 阶段证据校验与五道防线（需求模板 / 双模型复核 /
形式化校验 / 行为审计 / 人工复核）的判定；alpha-swe 的 AgentLoop 负责调用。
此前两侧没有统一的「会话状态」对象：TUI 无法感知当前阶段、哪些防线已通过 /
被拦截、任务是否在等待人工复核。

本模块提供：
- ``Stage``：与 phase-barrier ``STAGES`` 对齐的 7 阶段枚举（0..6）；
- ``DefenseStatus`` / ``DefenseState``：五道防线的状态机；
- ``SessionState``：一次任务的显式生命周期（session_id / 阶段 / 防线 / 风险 /
  证据引用 / 最近事件 / 时间戳），可 JSON 序列化并落盘 ``.agent_gate``，
  供会话中断后恢复。

``SessionState`` 不依赖任何 Agent 组件；AgentLoop 负责推进与记录，
TUI 通过 ``agent/core/events.py`` 事件总线或
``loop._emit("session_state", ...)`` 订阅变更。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.session_state")

# phase-barrier 阶段名（与 anti_shortcut.config.STAGES 对齐）
STAGE_LABELS: Dict[int, str] = {
    0: "需求记录",
    1: "Spec 设计",
    2: "测试编写",
    3: "实现代码",
    4: "运行测试",
    5: "修复回归",
    6: "交付",
}

# 五道防线：编号 -> (phase-barrier DefenseLine.name, 展示名)。
# 顺序与 phase-barrier BUILTIN_DEFENSE_LINES 一致。
DEFENSE_LINES: Dict[int, Dict[str, str]] = {
    1: {"name": "requirement_template", "label": "需求模板"},
    2: {"name": "dual_review", "label": "双模型复核"},
    3: {"name": "formal_check", "label": "形式化校验"},
    4: {"name": "behavior_audit", "label": "行为审计"},
    5: {"name": "human_review", "label": "人工复核"},
}
DEFENSE_LINE_BY_NAME: Dict[str, int] = {
    info["name"]: num for num, info in DEFENSE_LINES.items()
}

# 落盘文件名（位于工作区 .agent_gate/ 下）
SESSION_FILE_NAME = "alpha_swe_session.json"


def gate_dir(workspace: Any) -> Path:
    """门禁目录：工作区下的 ``.agent_gate``。"""
    return Path(str(workspace)) / ".agent_gate"


def session_state_path(workspace: Any) -> Path:
    """会话状态快照路径（供中断恢复 / TUI 读取）。"""
    return gate_dir(workspace) / SESSION_FILE_NAME


class Stage(int, Enum):
    """phase-barrier 阶段（0..6）。与 STAGES 编号一致。"""

    REQUIREMENT = 0
    SPEC = 1
    TESTS = 2
    IMPLEMENTATION = 3
    TEST_RUN = 4
    FIX = 5
    DELIVERY = 6

    @property
    def label(self) -> str:
        return STAGE_LABELS.get(int(self), f"阶段 {int(self)}")

    @classmethod
    def from_int(cls, value: Any) -> "Stage":
        try:
            return cls(int(value))
        except (TypeError, ValueError):
            return cls.REQUIREMENT


class DefenseStatus(str, Enum):
    """一道防线的状态。"""

    NOT_TRIGGERED = "not_triggered"
    PASSED = "passed"
    FAILED = "failed"
    WAITING_REVIEW = "waiting_review"

    @property
    def label(self) -> str:
        return {
            DefenseStatus.NOT_TRIGGERED: "未触发",
            DefenseStatus.PASSED: "通过",
            DefenseStatus.FAILED: "失败",
            DefenseStatus.WAITING_REVIEW: "待人工复核",
        }[self]


def defense_line_info(line: int) -> Dict[str, str]:
    """防线编号 -> {name, label}；未知编号返回占位信息。"""
    return DEFENSE_LINES.get(line, {
        "name": f"defense_{line}",
        "label": f"防线 {line}",
    })


def record_defense_checks(
    state: "SessionState",
    checks: List[Dict[str, Any]],
    workspace: Any = "",
) -> Dict[str, Any]:
    """把 phase-barrier 返回的 ``defense_checks`` 应用到会话状态。

    phase-barrier 阶段推进（``advance_stage``）的 evidence 结构::

        {"defense": {"defense_checks": [
            {"line": "dual_review", "ok": False, "message": "...",
             "evidence": {...}, "request_id": "..."},
        ]}}

    映射规则：
    - ``ok=True``            -> 防线 PASSED；
    - ``human_review`` 命中人工复核且未批准 -> WAITING_REVIEW（附复核命令提示）；
    - 其余 ``ok=False``      -> 防线 FAILED（fail-closed，阻止阶段推进）。

    :return: ``{"summaries": [...], "waiting_review": bool,
                "review_hint": str, "risk_score": Optional[int]}``
    """
    summaries: List[str] = []
    waiting_review = False
    review_hint = ""
    risk_score: Optional[int] = None
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        name = str(check.get("line") or "")
        line = DEFENSE_LINE_BY_NAME.get(name)
        if line is None:
            continue
        detail = str(check.get("message") or "")
        ev = check.get("evidence")
        request_id = str(check.get("request_id") or "")
        if check.get("ok"):
            state.record_defense(line, DefenseStatus.PASSED, detail=detail)
        elif name == "human_review":
            waiting_review = True
            review_hint = (
                f"python -m anti_shortcut review-approve"
                f" --request-id {request_id} --workspace {str(workspace) or state.workspace}"
            )
            state.record_defense(
                line,
                DefenseStatus.WAITING_REVIEW,
                detail=detail,
                evidence=ev if isinstance(ev, dict) else None,
                request_id=request_id,
                message=f"防线 {line} 人工复核 等待人工复核：{detail[:160]}",
            )
            if isinstance(ev, dict):
                score = ev.get("risk_score")
                if isinstance(score, (int, float)):
                    risk_score = int(score)
                    state.set_risk(risk_score, ev.get("risk_breakdown"))
        else:
            state.record_defense(
                line,
                DefenseStatus.FAILED,
                detail=detail,
                evidence=ev if isinstance(ev, dict) else None,
                request_id=request_id,
            )
        summaries.append(defense_summary(state.defense(line)))
    return {
        "summaries": summaries,
        "waiting_review": waiting_review,
        "review_hint": review_hint,
        "risk_score": risk_score,
    }


def defense_summary(record: DefenseState) -> str:
    """把一道防线记录渲染为一行中文摘要（TUI / 事件日志用）。"""
    label = defense_line_info(record.line)["label"]
    if record.status == DefenseStatus.PASSED:
        return f"防线 {record.line} {label} 通过"
    if record.status == DefenseStatus.WAITING_REVIEW:
        return f"防线 {record.line} {label} 等待人工复核"
    if record.status == DefenseStatus.FAILED:
        detail = str(record.detail or "").strip()
        return f"防线 {record.line} {label} 失败" + (
            f"：{detail[:120]}" if detail else ""
        )
    return f"防线 {record.line} {label} {record.status.label}"


@dataclass
class DefenseState:
    """单道防线的一次记录。"""

    line: int
    name: str = ""
    status: DefenseStatus = DefenseStatus.NOT_TRIGGERED
    detail: str = ""
    evidence: Optional[Dict[str, Any]] = None
    request_id: str = ""
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.name:
            self.name = defense_line_info(self.line)["name"]
        if self.timestamp is None:
            self.timestamp = time.time()

    @property
    def label(self) -> str:
        return defense_line_info(self.line)["label"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line": self.line,
            "name": self.name,
            "status": self.status.value,
            "detail": str(self.detail or "")[:500],
            "evidence": self.evidence,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DefenseState":
        return cls(
            line=int(data.get("line", 0)),
            name=str(data.get("name") or ""),
            status=DefenseStatus(str(data.get("status")
                                     or DefenseStatus.NOT_TRIGGERED.value)),
            detail=str(data.get("detail") or ""),
            evidence=data.get("evidence"),
            request_id=str(data.get("request_id") or ""),
            timestamp=data.get("timestamp"),
        )


@dataclass
class SessionState:
    """一次 Agent 任务的显式生命周期状态。

    :param session_id: 会话唯一 ID（hex 12）
    :param stage: 当前门禁阶段（与 phase-barrier 对齐）
    :param defenses: {防线编号: DefenseState}
    :param risk_score / risk_breakdown: 防线 5 风险评分（可选）
    :param evidence: 阶段证据 / 复核请求等引用（字符串键值，JSON 安全）
    :param recent_events: 有界最近事件（kind + message + ts）
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace: str = ""
    prompt: str = ""
    gate_enabled: bool = False
    stage: Stage = Stage.REQUIREMENT
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    defenses: Dict[int, DefenseState] = field(default_factory=dict)
    risk_score: int = 0
    risk_breakdown: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    complete: bool = False
    error: str = ""
    _max_events: int = 12

    # ---------- 状态机 ----------

    def transition(self, stage: Any, message: str = "") -> bool:
        """推进 / 回退到指定阶段。返回阶段是否发生变化。"""
        target = stage if isinstance(stage, Stage) else Stage.from_int(stage)
        changed = int(self.stage) != int(target)
        self.stage = target
        self.updated_at = time.time()
        if changed:
            self.add_event(
                "transition",
                message or f"进入阶段 {int(target)}（{target.label}）",
            )
        return changed

    def sync_from_gate(self, current_stage: Any, message: str = "") -> bool:
        """按 phase-barrier inspect 的 current_stage 对齐阶段。"""
        if current_stage is None:
            return False
        return self.transition(Stage.from_int(current_stage), message=message)

    def defense(self, line: int) -> DefenseState:
        """获取某防线当前状态（未记录时返回默认 NOT_TRIGGERED，不写入）。"""
        if line in self.defenses:
            return self.defenses[line]
        return DefenseState(line=line, status=DefenseStatus.NOT_TRIGGERED)

    def record_defense(
        self,
        line: int,
        status: Any,
        detail: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        request_id: str = "",
        message: str = "",
    ) -> DefenseState:
        """记录一道防线结果并追加到最近事件。"""
        status = status if isinstance(status, DefenseStatus) \
            else DefenseStatus(str(status))
        info = defense_line_info(line)
        record = DefenseState(
            line=line,
            name=info["name"],
            status=status,
            detail=str(detail or "")[:1000],
            evidence=evidence,
            request_id=request_id,
            timestamp=time.time(),
        )
        self.defenses[line] = record
        self.updated_at = time.time()
        summary = message or self._defense_summary(record)
        self.add_event("defense", summary)
        return record

    def set_risk(self, score: int,
                 breakdown: Optional[Dict[str, Any]] = None,
                 message: str = "") -> None:
        """写入防线 5 风险评分（0-100）。"""
        try:
            self.risk_score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            self.risk_score = 0
        if breakdown:
            self.risk_breakdown = dict(breakdown)
        self.updated_at = time.time()
        self.add_event(
            "risk",
            message or f"风险评分: {self.risk_score}/100",
        )

    def mark_finished(self, complete: bool, error: str = "",
                      message: str = "") -> None:
        """会话收尾：记录结束状态（幂等）。"""
        self.finished = True
        self.complete = bool(complete)
        self.error = str(error or "")[:500]
        self.updated_at = time.time()
        if self.complete:
            self.add_event("finished", message or "会话结束：已交付")
        else:
            self.add_event(
                "finished",
                message or (f"会话结束：未交付（{self.error or '任务失败'}）"),
            )

    def add_event(self, kind: str, message: str) -> None:
        """追加一条最近事件（有界队列，截断到 _max_events 条）。"""
        self.recent_events.append({
            "kind": kind,
            "message": str(message or "")[:300],
            "ts": time.time(),
        })
        limit = int(getattr(self, "_max_events", 12) or 12)
        if len(self.recent_events) > limit:
            del self.recent_events[: len(self.recent_events) - limit]

    def recent(self, limit: int = 5) -> List[Dict[str, Any]]:
        """最近的 N 条事件（时间倒序，便于 TUI 直接渲染）。"""
        return list(reversed(self.recent_events[-limit:]))

    # ---------- 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "prompt": self.prompt,
            "gate_enabled": self.gate_enabled,
            "stage": int(self.stage),
            "stage_name": self.stage.label,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "defenses": {
                str(line): state.to_dict()
                for line, state in sorted(self.defenses.items())
            },
            "risk_score": self.risk_score,
            "risk_breakdown": self.risk_breakdown,
            "evidence": dict(self.evidence),
            "recent_events": self.recent_events,
            "finished": self.finished,
            "complete": self.complete,
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        stage = Stage.from_int(data.get("stage"))
        defenses: Dict[int, DefenseState] = {}
        for key, value in (data.get("defenses") or {}).items():
            if isinstance(value, dict):
                try:
                    defenses[int(key)] = DefenseState.from_dict(value)
                except (TypeError, ValueError):
                    continue
        return cls(
            session_id=str(data.get("session_id")
                           or uuid.uuid4().hex[:12]),
            workspace=str(data.get("workspace") or ""),
            prompt=str(data.get("prompt") or ""),
            gate_enabled=bool(data.get("gate_enabled", False)),
            stage=stage,
            started_at=float(data.get("started_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            defenses=defenses,
            risk_score=int(data.get("risk_score") or 0),
            risk_breakdown=dict(data.get("risk_breakdown") or {}),
            evidence=dict(data.get("evidence") or {}),
            recent_events=list(data.get("recent_events") or []),
            finished=bool(data.get("finished", False)),
            complete=bool(data.get("complete", False)),
            error=str(data.get("error") or ""),
        )

    # ---------- 落盘 ----------

    def save(self, path: Any = None) -> Optional[Path]:
        """把快照写到 workspace/.agent_gate/alpha_swe_session.json。

        写失败只记 WARN，绝不抛异常（观测层不影响主流程）。
        """
        try:
            target = Path(path) if path is not None \
                else session_state_path(self.workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.to_json(), encoding="utf-8")
            return target
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话状态落盘失败: %s", exc)
            return None

    @classmethod
    def load(cls, workspace: Any) -> Optional["SessionState"]:
        """从 workspace/.agent_gate/alpha_swe_session.json 恢复快照。"""
        try:
            path = session_state_path(workspace)
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return cls.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话状态读取失败: %s", exc)
            return None

    # ---------- 内部 ----------

    @staticmethod
    def _defense_summary(record: DefenseState) -> str:
        label = defense_line_info(record.line)["label"]
        if record.status == DefenseStatus.PASSED:
            return f"防线 {record.line} {label} 通过"
        if record.status == DefenseStatus.WAITING_REVIEW:
            return f"防线 {record.line} {label} 等待人工复核"
        if record.status == DefenseStatus.FAILED:
            detail = record.detail.strip()
            return f"防线 {record.line} {label} 失败" + (
                f"：{detail[:120]}" if detail else ""
            )
        return f"防线 {record.line} {label} {record.status.label}"


__all__ = [
    "DefenseState",
    "DefenseStatus",
    "DEFENSE_LINES",
    "DEFENSE_LINE_BY_NAME",
    "SESSION_FILE_NAME",
    "STAGE_LABELS",
    "SessionState",
    "Stage",
    "defense_summary",
    "defense_line_info",
    "gate_dir",
    "record_defense_checks",
    "session_state_path",
]
