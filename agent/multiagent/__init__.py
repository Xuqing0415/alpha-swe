"""多 Agent 协作（对应设计第 8 节）。

分层协作 + 黑板模式：Orchestrator 规划/派发/仲裁，Worker 角色化执行，
共享 Blackboard 承载成果与消息日志。
"""
from agent.multiagent.blackboard import Artifact, Blackboard
from agent.multiagent.messages import Message, MsgType
from agent.multiagent.orchestrator import (
    OrchestratorAgent,
    ReviewRecord,
    TeamPlanner,
    TeamResult,
)
from agent.multiagent.workers import WorkerAgent, WorkerResult

__all__ = [
    "Artifact",
    "Blackboard",
    "Message",
    "MsgType",
    "OrchestratorAgent",
    "ReviewRecord",
    "TeamPlanner",
    "TeamResult",
    "WorkerAgent",
    "WorkerResult",
]