# -*- coding: utf-8 -*-
"""主线三：自我评估与持续进化。

- 3.1 capability.py   能力画像：跨会话记录各能力维度表现（时间衰减加权）
- 3.2 proposals.py    失败驱动改进循环：归因 -> 提议 -> 待验证队列 -> 晋升自学策略
- 3.3 benchmark.py    基准集自动更新：代表性任务提取 + 版本化台账 + 趋势告警
"""
from agent.selfimprove.benchmark import BenchmarkExtractor
from agent.selfimprove.capability import (CAPABILITY_DIMENSIONS,
                                          CapabilityProfile)
from agent.selfimprove.proposals import (STATUS_PENDING, STATUS_PROMOTED,
                                         STATUS_REJECTED, ProposalStore)

__all__ = [
    "CapabilityProfile", "CAPABILITY_DIMENSIONS",
    "ProposalStore", "STATUS_PENDING", "STATUS_PROMOTED", "STATUS_REJECTED",
    "BenchmarkExtractor",
]
