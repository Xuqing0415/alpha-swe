"""主线二 2.3：用户超级角色 —— 否决原因与决策里程碑标记。

用户在团队中作为「超级角色」参与协作：
- 插话：以 USER_MESSAGE 广播（最高优先级），各收听者可逐轮消费；
- 决策：否决 / 批准 / 改派 / 中断以 USER_DECISION 广播，附结构化原因；
- 里程碑：每次关键决策写入决策日志（user.milestone）并在黑板用户通道
  留痕，供会话时间线 / 回放（SessionReplay.milestones）快速跳转。

否决原因强制结构化（设计 2.3B），并映射「调整策略而非盲目重试」指引：
- direction 方向错误   -> 回到原始需求重新理解，必要时重新规划；
- method    方案不当   -> 换一种实现方法，不用相同方法重试；
- risk      风险不可接受 -> 更安全做法，先验证缩小影响面；
- timing    时机不对   -> 暂停稍后再做，不自动重试；
- other     其他       -> 暂停相关操作并等待用户补充说明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from agent.core.decision_logger import DecisionLogger
from agent.multiagent.blackboard import Blackboard
from agent.multiagent.messages import (
    USER_PRIORITY,
    USER_SENDER,
    Message,
    MsgType,
)

_VETO_LABELS: Dict[str, str] = {
    "direction": "方向错误（重新理解任务）",
    "method": "方案不当（需要不同方法）",
    "risk": "风险不可接受（需要更安全的做法）",
    "timing": "时机不对（稍后再做）",
    "other": "其他（手动输入）",
}

_VETO_GUIDANCE: Dict[str, str] = {
    "direction": "回到原始任务理解需求，必要时重新规划，不要在现有方案上继续推进",
    "method": "换一种实现方法推进，不要用相同方法盲目重试",
    "risk": "改用更安全的做法（缩小影响面/沙箱限制/先验证再落地），不得直接重试高风险操作",
    "timing": "暂停该操作稍后再做，不自动重试",
    "other": "先暂停相关操作，等待用户补充说明",
}


class VetoReason(str, Enum):
    """用户否决原因（结构化枚举，供 TUI 决策面板选择）。"""

    DIRECTION = "direction"
    METHOD = "method"
    RISK = "risk"
    TIMING = "timing"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _VETO_LABELS[self.value]

    @property
    def guidance(self) -> str:
        return _VETO_GUIDANCE[self.value]

    @classmethod
    def choices(cls) -> List[Dict[str, str]]:
        """决策面板可选原因列表（顺序固定 1..5）。"""
        return [{"code": r.value, "label": r.label} for r in cls]


class MilestoneKind(str, Enum):
    """用户关键决策里程碑类型。"""

    VETO = "veto"
    APPROVE = "approve"
    REASSIGN = "reassign"
    INTERRUPT = "interrupt"
    NOTE = "note"

    @property
    def label(self) -> str:
        return {
            MilestoneKind.VETO: "用户否决",
            MilestoneKind.APPROVE: "用户批准",
            MilestoneKind.REASSIGN: "用户改派",
            MilestoneKind.INTERRUPT: "用户中断",
            MilestoneKind.NOTE: "用户插话",
        }[self]


@dataclass
class UserMilestone:
    """一次用户关键决策的里程碑记录。"""

    kind: str
    summary: str
    detail: str = ""
    refs: List[str] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
            "refs": list(self.refs),
            "at": self.at,
            "human": True,
        }


class UserRole:
    """用户超级角色：结构化决策 + 里程碑 + 黑板高优通道。

    负责记录与广播；是否按否决原因中止某 Agent 的自动重试由
    OrchestratorAgent 在调度点查询 is_vetoed/veto_reason 决定。
    """

    def __init__(self, blackboard: Optional[Blackboard] = None,
                 decision_logger: Optional[DecisionLogger] = None) -> None:
        self.blackboard = blackboard or Blackboard()
        self.decision_logger = decision_logger or DecisionLogger()
        self._milestones: List[UserMilestone] = []
        # action_ref -> reason code（否决生效中）
        self._vetoed: Dict[str, str] = {}

    # ---- 用户消息通道（2.3A） ----
    def post_note(self, text: str, receiver: str = "*") -> Message:
        """用户插话：USER_MESSAGE 最高优先级广播，各收听者独立消费。"""
        msg = Message(
            sender=USER_SENDER,
            receiver=receiver or "*",
            type=MsgType.USER_MESSAGE.value,
            payload={"text": str(text)},
            priority=USER_PRIORITY,
        )
        self.blackboard.post_user(msg)
        return msg

    def _post_decision(self, decision: str, action_ref: str,
                       payload_extra: Dict[str, Any]) -> Message:
        payload = {"decision": decision, "action_ref": action_ref}
        payload.update(payload_extra)
        msg = Message(
            sender=USER_SENDER,
            receiver="*",
            type=MsgType.USER_DECISION.value,
            payload=payload,
            priority=USER_PRIORITY,
        )
        self.blackboard.post_user(msg)
        return msg

    def _milestone(self, kind: MilestoneKind, summary: str,
                   detail: str = "",
                   refs: Optional[List[str]] = None) -> UserMilestone:
        m = UserMilestone(kind=kind.value, summary=summary,
                          detail=detail, refs=list(refs or []))
        self._milestones.append(m)
        suffix = f" | {detail}" if detail else ""
        self.decision_logger.record(
            "user.milestone", "team.users", kind.value,
            f"{kind.label} | {summary}{suffix}",
        )
        return m

    # ---- 决策（2.3B） ----
    def veto(self, action_ref: str, reason: str,
             note: str = "") -> UserMilestone:
        """用户否决某操作：原因必须来自 VetoReason，否则抛 ValueError。"""
        try:
            code = VetoReason(reason)
        except ValueError:
            choices = ", ".join(f"{r.value}({r.label})" for r in VetoReason)
            raise ValueError(
                f"无效否决原因: {reason!r}；可选: {choices}") from None
        self._vetoed[action_ref] = code.value
        self._post_decision("veto", action_ref, {
            "reason": code.value,
            "reason_label": code.label,
            "guidance": code.guidance,
            "note": str(note or ""),
        })
        summary = f"用户否决 {action_ref}（原因: {code.label}）"
        detail = code.guidance + (f"；补充说明: {note}" if note else "")
        return self._milestone(MilestoneKind.VETO, summary,
                               detail=detail, refs=[action_ref])

    def approve(self, action_ref: str, note: str = "") -> UserMilestone:
        """用户批准某操作；同时解除该操作的否决标记（可恢复自动重试）。"""
        self._vetoed.pop(action_ref, None)
        self._post_decision("approve", action_ref, {"note": str(note or "")})
        return self._milestone(
            MilestoneKind.APPROVE, f"用户批准 {action_ref}",
            detail=str(note or ""), refs=[action_ref])

    def reassign(self, action_ref: str, target_role: str,
                 note: str = "") -> UserMilestone:
        """用户改派：把某操作移交给指定角色执行。"""
        target_role = (target_role or "").strip()
        if not target_role:
            raise ValueError("改派目标角色不能为空")
        self._post_decision("reassign", action_ref, {
            "target_role": target_role,
            "note": str(note or ""),
        })
        return self._milestone(
            MilestoneKind.REASSIGN,
            f"用户改派 {action_ref} -> {target_role}",
            detail=str(note or ""), refs=[action_ref])

    def interrupt(self, action_ref: str, note: str = "") -> UserMilestone:
        """用户中断：要求停止当前操作（是否中止执行由上层调度决定）。"""
        self._post_decision("interrupt", action_ref, {"note": str(note or "")})
        return self._milestone(
            MilestoneKind.INTERRUPT, f"用户中断 {action_ref}",
            detail=str(note or ""), refs=[action_ref])

    # ---- 否决查询 ----
    def is_vetoed(self, action_ref: str) -> bool:
        return action_ref in self._vetoed

    def veto_reason(self, action_ref: str) -> Optional[str]:
        return self._vetoed.get(action_ref)

    # ---- 里程碑（2.3C） ----
    def milestones(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._milestones]

    def last_milestone(self) -> Optional[Dict[str, Any]]:
        return self._milestones[-1].to_dict() if self._milestones else None

    def pending_inputs(self, consumer: str) -> List[Dict[str, Any]]:
        """某收听者待处理的用户消息（按收听者独立游标消费）。"""
        return [m.to_dict() for m in
                self.blackboard.pending_user_messages(consumer)]


__all__ = ["UserRole", "UserMilestone", "VetoReason", "MilestoneKind"]
