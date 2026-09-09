"""方案辩论协调器（主线二 2.2）——分歧锚定 / Critic 结构化评估 / 回溯验证。

深化三要素的可观测机制（纯 Python，可脱离 LLM 确定性测试）：
- 2.2A 分歧锚定：open_debate(anchor, options, question) 用一句话显式锚定
  核心分歧，并记录各方第 1 轮开场陈述（transcript）；
- 2.2B Critic 结构化评估：critic_evaluate 支持确定性评分路径（测试用）与
  可选 llm(session) -> JSON 路径；低置信 / 无法判定 / 轮次上限均升级为
  NEEDS_USER，绝不强制拍板、绝不抛异常；
- 2.2C 回溯验证：record_verification 记录判定对错，critic_misses 按
  0.1/次累计、封顶 0.5 折算 effective_confidence。

每次关键动作（open/critic/resolved/needs_user/verify）都可通过可选
decision_logger 落「team.debate」决策日志，供团队档案复盘。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("alpha-swe.multiagent.debate")

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_NEEDS_USER = "NEEDS_USER"

# 单场辩论最大 Critic 评估轮数：达到仍未决议自动升级用户决策（防无限辩论）
MAX_ROUNDS = 2
# 低于该置信度不强制拍板，升级用户决策
CONFIDENCE_THRESHOLD = 0.15
# 回溯验证失误惩罚：每失误一次扣 0.1 置信度，封顶 0.5
_MISS_PENALTY_PER_MISS = 0.1
_MISS_PENALTY_CAP = 0.5


def _penalty_for_misses(misses: int) -> float:
    return min(_MISS_PENALTY_CAP, _MISS_PENALTY_PER_MISS * max(0, misses))


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_criteria_scores(
    raw: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, int]]:
    """把外部评分收敛为 {option_id: {criterion: int}}，非法值按 0 处理。"""
    normalized: Dict[str, Dict[str, int]] = {}
    for option_id, scores in (raw or {}).items():
        if not isinstance(scores, dict):
            continue
        normalized[str(option_id)] = {
            str(criterion): _int_or_zero(value)
            for criterion, value in scores.items()
        }
    return normalized


@dataclass
class DebateOption:
    """一个备选方案：唯一 id + 一句话陈述 + 支持/反对/风险清单。"""
    id: str
    label: str = ""
    statement: str = ""
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

    def display(self) -> str:
        return self.label or self.id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "statement": self.statement,
            "pros": list(self.pros),
            "cons": list(self.cons),
            "risks": list(self.risks),
        }


@dataclass
class CriticVerdict:
    """一次 Critic 结构化评估输出（schema 齐全，供决策日志/档案）。"""
    recommendation: str  # 推荐方案 id；无法唯一判定时为空串
    confidence: float    # 0~1；低置信不拍板
    criteria_scores: Dict[str, Dict[str, int]]
    key_reasons: List[str]
    unresolved_concerns: List[str]
    method: str = "deterministic"   # deterministic | llm
    round: int = 0                  # 对应辩论轮次
    keep_open: bool = False         # llm 显式要求继续辩一轮（不拍板）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "confidence": round(self.confidence, 4),
            "criteria_scores": {
                option_id: dict(row)
                for option_id, row in self.criteria_scores.items()
            },
            "key_reasons": list(self.key_reasons),
            "unresolved_concerns": list(self.unresolved_concerns),
            "method": self.method,
            "round": self.round,
            "keep_open": self.keep_open,
        }


class DebateSession:
    """一场方案辩论的记录单元（status/rounds/transcript/critics/resolution）。"""

    def __init__(self, session_id: str, anchor: str,
                 options: List[DebateOption], question: str = "") -> None:
        self.session_id = session_id
        self.anchor = anchor
        self.question = question
        self.options = list(options)
        self.status = STATUS_OPEN
        self.rounds = 0
        self.created_at = datetime.now().isoformat(timespec="seconds")
        # transcript：每轮 statement 含 round/option_id/speaker/text
        self.transcript: List[Dict[str, Any]] = [
            {
                "round": 1,
                "option_id": option.id,
                "speaker": option.display(),
                "text": option.statement,
            }
            for option in self.options
        ]
        self.critics: List[Dict[str, Any]] = []
        self.last_verdict: Optional[CriticVerdict] = None
        self.resolution: Optional[Dict[str, Any]] = None
        self.verification: List[Dict[str, Any]] = []
        self.critic_misses = 0

    def _effective_confidence(self) -> float:
        """最近一次 Critic 置信度 × (1 - 失误惩罚)，供档案可观测。"""
        if self.last_verdict is None:
            return 0.0
        penalty = _penalty_for_misses(self.critic_misses)
        return round(
            max(0.0, self.last_verdict.confidence * (1.0 - penalty)), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "anchor": self.anchor,
            "question": self.question,
            "status": self.status,
            "rounds": self.rounds,
            "created_at": self.created_at,
            "options": [option.to_dict() for option in self.options],
            "transcript": [dict(entry) for entry in self.transcript],
            "critics": [dict(verdict) for verdict in self.critics],
            "resolution": dict(self.resolution) if self.resolution else None,
            "verification": [dict(entry) for entry in self.verification],
            "critic_misses": self.critic_misses,
            "effective_confidence": self._effective_confidence(),
        }


class DebateCoordinator:
    """方案辩论协调器：会话注册表 + 评估/决议/验证 + 决策日志。"""

    def __init__(
        self,
        llm: Optional[Callable[[DebateSession], str]] = None,
        decision_logger: Any = None,
        max_rounds: int = MAX_ROUNDS,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self.llm = llm
        self.decision_logger = decision_logger
        self.max_rounds = max_rounds
        self.confidence_threshold = confidence_threshold
        self._sessions: Dict[str, DebateSession] = {}

    # ---- 2.2A：分歧点显式锚定 ----
    def open_debate(self, anchor: str, options: List[Any],
                    question: str = "") -> DebateSession:
        """锚定核心分歧并开启一场辩论；anchor 必填、options 至少 2 个。"""
        if not anchor or not str(anchor).strip():
            raise ValueError("debate anchor 必填：请用一句话锚定核心分歧")
        parsed = [self._coerce_option(item, index)
                  for index, item in enumerate(options)]
        ids = [option.id for option in parsed]
        if len(ids) < 2:
            raise ValueError("options 至少需要 2 个备选方案")
        if len(set(ids)) != len(ids):
            raise ValueError("options 的方案 id 必须唯一")
        session = DebateSession(
            session_id=uuid.uuid4().hex[:8],
            anchor=str(anchor).strip(),
            options=parsed,
            question=str(question or "").strip(),
        )
        self._sessions[session.session_id] = session
        self._log(
            "debate.open", session,
            f"开启方案辩论，锚定分歧：{session.anchor}；备选 "
            f"{len(session.options)} 个："
            f"{', '.join(option.display() for option in session.options)}"
            + (f"；议题：{session.question}" if session.question else ""),
        )
        return session

    # ---- 2.2B：Critic 结构化评估 ----
    def critic_evaluate(
        self,
        session_id: str,
        criteria_scores: Optional[Dict[str, Dict[str, int]]] = None,
        confidence: Optional[float] = None,
    ) -> CriticVerdict:
        """对某场辩论做一轮 Critic 评估；每次评估使 rounds += 1。

        criteria_scores 非空 -> 确定性评分路径（推荐 = 总分最高者）；
        否则若配置了 llm -> llm(session) 返回 JSON（失败回退 NEEDS_USER，
        绝不抛异常）。低置信/无法判定 -> NEEDS_USER；达到 max_rounds
        仍未 RESOLVED -> 自动 NEEDS_USER。
        """
        session = self._require_session(session_id)
        if (session.status == STATUS_RESOLVED
                and session.last_verdict is not None):
            return session.last_verdict
        if (session.status == STATUS_NEEDS_USER
                and session.rounds >= self.max_rounds):
            # 已升级且轮次用尽：锁定，避免无限辩论
            return session.last_verdict  # type: ignore[return-value]

        if criteria_scores is None and self.llm is not None:
            verdict = self._evaluate_with_llm(session)
        else:
            verdict = self._evaluate_deterministic(
                session, criteria_scores, confidence)

        session.rounds += 1
        verdict.round = session.rounds
        session.critics.append(verdict.to_dict())
        session.last_verdict = verdict

        if verdict.keep_open:
            # llm 显式要求续辩：保持 OPEN，让下一轮补充论据后再次评估
            session.status = STATUS_OPEN
        elif (verdict.recommendation
              and verdict.confidence >= self.confidence_threshold):
            session.status = STATUS_RESOLVED
            option = self._option_by_id(session, verdict.recommendation)
            session.resolution = {
                "option_id": verdict.recommendation,
                "option_label": option.display() if option
                else verdict.recommendation,
                "confidence": round(verdict.confidence, 4),
                "round": verdict.round,
                "method": verdict.method,
            }
            self._log(
                "debate.resolved", session,
                f"第 {verdict.round} 轮决议：推荐方案"
                f"「{session.resolution['option_label']}」"
                f"（confidence={verdict.confidence:.2f}，"
                f"method={verdict.method}）",
            )
        else:
            self._mark_needs_user(
                session,
                f"第 {verdict.round} 轮 Critic 无法给出高置信推荐"
                f"（confidence={verdict.confidence:.2f}）",
            )

        # 轮次上限兜底：仍未决议 -> 自动升级用户决策（防无限辩论）
        if (session.status != STATUS_RESOLVED
                and session.rounds >= self.max_rounds):
            self._mark_needs_user(
                session, f"达到最大辩论轮次（{self.max_rounds}）仍未决议")

        self._log(
            "debate.critic", session,
            f"第 {verdict.round} 轮 Critic 评估（{verdict.method}）："
            f"推荐「{verdict.recommendation or '（无法唯一判定）'}」，"
            f"confidence={verdict.confidence:.2f}，"
            f"当前状态={session.status}",
        )
        return verdict

    # ---- 2.2C：回溯验证 ----
    def record_verification(self, session_id: str,
                            outcome_ok: bool) -> Dict[str, Any]:
        """记录一次回溯验证：判定正确 verified_good / 失误 misjudged。"""
        session = self._require_session(session_id)
        entry = {
            "round": session.rounds,
            "outcome_ok": bool(outcome_ok),
            "outcome": "verified_good" if outcome_ok else "misjudged",
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        session.verification.append(entry)
        if not outcome_ok:
            session.critic_misses += 1
        outcome = "判定正确(verified_good)" if outcome_ok \
            else "判定失误(misjudged)"
        self._log(
            "debate.verify", session,
            f"回溯验证：{outcome}；"
            f"critic_misses={session.critic_misses}",
        )
        return entry

    def verification_summary(self) -> Dict[str, int]:
        """跨会话聚合验证结果：{total, good, misjudged}。"""
        total = good = misjudged = 0
        for session in self._sessions.values():
            for entry in session.verification:
                total += 1
                if entry["outcome"] == "verified_good":
                    good += 1
                else:
                    misjudged += 1
        return {"total": total, "good": good, "misjudged": misjudged}

    def effective_confidence(self, session: Any) -> float:
        """effective = 最近置信度 × (1 - misses×0.1，封顶扣 0.5)。"""
        resolved = self._require_session(session)
        if resolved.last_verdict is None:
            return 0.0
        penalty = _penalty_for_misses(resolved.critic_misses)
        return round(
            max(0.0, resolved.last_verdict.confidence * (1.0 - penalty)), 4)

    def debate_summary(self) -> Dict[str, Any]:
        by_status = {
            STATUS_OPEN: 0,
            STATUS_RESOLVED: 0,
            STATUS_NEEDS_USER: 0,
        }
        total_rounds = 0
        for session in self._sessions.values():
            by_status[session.status] = by_status.get(session.status, 0) + 1
            total_rounds += session.rounds
        return {
            "sessions": len(self._sessions),
            "status": by_status,
            "total_rounds": total_rounds,
            "verification": self.verification_summary(),
        }

    # ---- 内部 ----
    def _evaluate_deterministic(
        self,
        session: DebateSession,
        criteria_scores: Optional[Dict[str, Dict[str, int]]],
        confidence: Optional[float],
    ) -> CriticVerdict:
        normalized = _normalize_criteria_scores(criteria_scores)
        totals: Dict[str, int] = {}
        for option in session.options:
            row = normalized.get(option.id, {})
            totals[option.id] = sum(row.values())
        ranked = sorted(
            session.options, key=lambda option: totals[option.id], reverse=True)
        first_option = ranked[0]
        second_option = ranked[1] if len(ranked) > 1 else None
        first = totals[first_option.id]
        second = totals[second_option.id] if second_option is not None else 0
        tied = [option for option in ranked if totals[option.id] == first]

        key_reasons = [
            f"方案「{option.display()}」总分 {totals[option.id]}"
            for option in ranked
        ]
        if first <= 0:
            recommendation = ""
            conf = 0.0
            concerns = ["所有方案均无有效评分，本轮无法判定"]
        elif len(tied) > 1:
            recommendation = ""
            conf = 0.0
            concerns = [
                "最高分平票（"
                + "、".join(option.display() for option in tied)
                + f"，总分 {first}），无法唯一判定"
            ]
        else:
            recommendation = first_option.id
            if confidence is None:
                conf = (first - second) / first if first > 0 else 0.0
            else:
                conf = float(confidence)
            conf = max(0.0, min(1.0, conf))
            if second_option is not None:
                key_reasons.append(
                    f"最高分方案「{first_option.display()}」总分 {first}，"
                    f"领先「{second_option.display()}」{first - second} 分")
            else:
                key_reasons.append(
                    f"最高分方案「{first_option.display()}」总分 {first}")
            concerns = []
            if conf < self.confidence_threshold:
                concerns.append(
                    f"推荐方案领先幅度不足（confidence={conf:.2f} < "
                    f"{self.confidence_threshold}），升级用户决策")
            concerns.extend(
                f"推荐方案「{first_option.display()}」风险：{risk}"
                for risk in (first_option.risks or [])[:3]
            )
        if not recommendation:
            key_reasons.append("无唯一高分方案，本轮无法给出推荐")
        return CriticVerdict(
            recommendation=recommendation,
            confidence=conf,
            criteria_scores=normalized,
            key_reasons=key_reasons,
            unresolved_concerns=concerns,
            method="deterministic",
        )

    def _evaluate_with_llm(self, session: DebateSession) -> CriticVerdict:
        """llm(session) -> JSON；任何解析失败都回退 NEEDS_USER，绝不抛异常。"""
        fallback = CriticVerdict(
            recommendation="",
            confidence=0.0,
            criteria_scores={},
            key_reasons=["Critic LLM 输出无法解析，回退用户决策"],
            unresolved_concerns=["LLM 未给出可用的结构化评估"],
            method="llm",
        )
        try:
            raw = self.llm(session)  # type: ignore[misc]
        except Exception as exc:
            logger.warning("辩论 Critic LLM 调用失败: %s", exc)
            return fallback
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            logger.warning("辩论 Critic LLM JSON 解析失败: %s", exc)
            return fallback
        if not isinstance(data, dict):
            return fallback

        option_ids = [option.id for option in session.options]
        recommendation = str(data.get("recommendation") or "").strip()
        if recommendation and recommendation not in option_ids:
            recommendation = ""
        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        keep_open = str(data.get("status", "")).strip().lower() == "open"
        reasons = [str(item) for item in (data.get("key_reasons") or [])]
        concerns = [
            str(item) for item in (data.get("unresolved_concerns") or [])
        ]
        return CriticVerdict(
            recommendation=recommendation,
            confidence=conf,
            criteria_scores=_normalize_criteria_scores(
                data.get("criteria_scores")),
            key_reasons=reasons or ["Critic LLM 未提供理由"],
            unresolved_concerns=concerns,
            method="llm",
            keep_open=keep_open,
        )

    def _mark_needs_user(self, session: DebateSession, reason: str) -> None:
        """升级用户决策（每次状态流转记录一次决策日志）。"""
        if session.status == STATUS_NEEDS_USER:
            return
        session.status = STATUS_NEEDS_USER
        self._log("debate.needs_user", session,
                  f"{reason}；升级用户决策，不再强制拍板")

    def _option_by_id(self, session: DebateSession,
                      option_id: str) -> Optional[DebateOption]:
        for option in session.options:
            if option.id == option_id:
                return option
        return None

    def _require_session(self, key: Any) -> DebateSession:
        session = self._sessions.get(key) if isinstance(key, str) else key
        if not isinstance(session, DebateSession):
            raise ValueError(f"辩论会话不存在: {key}")
        return session

    @staticmethod
    def _coerce_option(raw: Any, index: int) -> DebateOption:
        if isinstance(raw, DebateOption):
            return raw
        if isinstance(raw, dict):
            return DebateOption(
                id=str(raw.get("id") or raw.get("label")
                       or f"option{index}"),
                label=str(raw.get("label") or ""),
                statement=str(raw.get("statement") or ""),
                pros=[str(item) for item in (raw.get("pros") or [])],
                cons=[str(item) for item in (raw.get("cons") or [])],
                risks=[str(item) for item in (raw.get("risks") or [])],
            )
        raise ValueError(
            f"options 元素必须是 DebateOption 或 dict: {raw!r}")

    def _log(self, name: str, session: DebateSession, decision: str) -> None:
        if self.decision_logger is None:
            return
        try:
            self.decision_logger.record(
                name, "team.debate", session.session_id, decision)
        except Exception as exc:
            logger.exception("辩论决策日志记录失败: %s", exc)


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "MAX_ROUNDS",
    "CriticVerdict",
    "DebateCoordinator",
    "DebateOption",
    "DebateSession",
    "STATUS_NEEDS_USER",
    "STATUS_OPEN",
    "STATUS_RESOLVED",
]
