"""事件总线：生产者-消费者模型，解耦 UI 渲染与 Worker 核心逻辑
Worker 线程生产事件 -> queue.Queue -> UI 线程消费事件，UI 永不阻塞
"""
import queue
import uuid
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class AgentEvent:
    """Agent 事件"""
    event_type: str  # step_start/step_end/tool_call/tool_result/error/compress/status
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


class EventBus:
    """线程安全的事件总线"""

    def __init__(self, max_size: int = 1000):
        self._queue = queue.Queue(maxsize=max_size)

    def publish(self, event: AgentEvent):
        """发布事件（非阻塞，队列满时丢弃最旧事件）"""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 丢弃最旧事件，放入新事件
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:
                pass

    def consume(self, timeout: float = 0.05) -> Optional[AgentEvent]:
        """消费事件（非阻塞）"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def consume_all(self) -> list:
        """消费所有积压事件"""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def size(self) -> int:
        """当前队列大小"""
        return self._queue.qsize()

    def clear(self):
        """清空队列"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


# 全局事件总线实例
event_bus = EventBus()


def publish_event(event_type: str, **data):
    """便捷发布函数"""
    event_bus.publish(AgentEvent(event_type=event_type, data=data))


def consume_events(timeout: float = 0.05) -> list:
    """便捷消费函数"""
    events = []
    e = event_bus.consume(timeout=timeout)
    while e:
        events.append(e)
        e = event_bus.consume(timeout=0)
    return events