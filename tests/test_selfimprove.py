# -*- coding: utf-8 -*-
"""主线三：自我评估与持续进化（3.1 能力画像 / 3.2 改进提议 / 3.3 基准提取 + 集成）。"""
from types import SimpleNamespace

from agent.config import (AgentConfig, AppConfig, MCPOptions, MemoryConfig,
                          SandboxConfig)
from agent.core.loop import AgentLoop
from agent.core.task import Task
from agent.llm import MockLLM
from agent.selfimprove import (BenchmarkExtractor, CapabilityProfile,
                               ProposalStore, STATUS_LOCAL,
                               STATUS_PROMOTED, STATUS_REJECTED,
                               scene_bucket, scene_similarity)


# ---- 3.1 能力画像 ----

def test_capability_record_updates_score(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(3):
        dims = prof.record("修复登录模块空指针崩溃", ok=True)
    assert "debug" in dims and "code_modify" in dims
    prof.record("修复登录模块空指针崩溃", ok=False)
    assert 0.0 < prof.score("debug") < 1.0
    prof.close()


def test_capability_profile_text_highlights_weak(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(4):
        prof.record("修复崩溃并定位根因", ok=False)
    text = prof.profile_text()
    assert "[能力画像]" in text
    assert "调试定位偏弱" in text
    prof.close()


def test_capability_trend_warning_after_decline(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(6):
        prof.record("修复缓存性能问题", ok=True)
    for _ in range(5):
        prof.record("修复缓存性能问题", ok=False)
    warns = prof.trend_warnings()
    assert warns, "连续失败后应给出能力下降告警"
    assert any("性能优化" in w for w in warns)
    prof.close()


def test_capability_persists_across_reopen(ws_tmp):
    path = str(ws_tmp / "capability.json")
    prof = CapabilityProfile(path=path)
    prof.record("为模块编写测试", ok=True)
    prof.close()
    reopened = CapabilityProfile(path=path)
    assert reopened.score("test_writing") > 0.0
    reopened.close()


# ---- 3.1B 滑动窗口评分与置信区间 ----

def test_capability_sliding_window_recent_weight(ws_tmp):
    """近期连续失败应显著拉低合成分数，且低于历史总体成功率。"""
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(6):
        prof.record("修复缓存性能问题", ok=True)
    for _ in range(3):
        prof.record("修复缓存性能问题", ok=False)
    assert prof.score("performance") < prof.overall("performance")
    assert 0.0 < prof.score("performance") < 0.6
    prof.close()


def test_capability_confidence_insufficient_data(ws_tmp):
    """样本 < 5 标记“数据不足”，不进入可视化报告。"""
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(4):
        prof.record("修复登录空指针崩溃", ok=True)
    assert prof.reliable("debug") is False
    assert "数据不足" in prof.confidence_text("debug")
    assert prof.confidence_report() == [], "样本不足不参与可视化展示"
    prof.close()


def test_capability_confidence_low_samples_band(ws_tmp):
    """5~9 样本：给出分数但标注“样本较少，评估可信度低”。"""
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(8):
        prof.record("修复登录空指针崩溃", ok=True)
    assert prof.reliable("debug") is True
    assert "样本较少" in prof.confidence_text("debug")
    assert prof.confidence_report()
    prof.close()


def test_capability_confidence_margin_after_enough_samples(ws_tmp):
    """样本 >= 10 后展示 95% 置信区间（±半宽）。"""
    prof = CapabilityProfile(path=str(ws_tmp / "capability.json"))
    for _ in range(12):
        prof.record("修复登录空指针崩溃", ok=True)
    assert prof.reliable("debug") is True
    assert prof.margin("debug") > 0.0
    assert "±" in prof.confidence_text("debug")
    info = prof.score_with_confidence("debug")
    assert info["samples"] == 12 and info["reliable"] is True
    assert info["margin"] == prof.margin("debug")
    prof.close()


# ---- 3.2 改进提议 ----

def test_proposal_create_and_match(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("planning", "修复空指针崩溃", "增强规划器")
    assert pid in [p["id"] for p in store.list()]
    assert store.match("修复登录空指针崩溃") == [pid]
    store.close()


def test_scene_similarity_buckets():
    """3.2A：相似（>=0.7）/ 相关（0.4~0.7）/ 不相关（<0.4）分桶。"""
    assert scene_similarity("部署任务超时", "服务部署任务超时处理") >= 0.7
    sim = scene_similarity("修复登录空指针崩溃", "登录空指针导致服务崩溃")
    assert 0.4 <= sim < 0.7
    assert scene_similarity("修复登录空指针崩溃", "优化缓存性能") < 0.4
    assert scene_bucket(0.8) == "similar"
    assert scene_bucket(0.5) == "related"
    assert scene_bucket(0.1) == "distant"


def test_proposal_promoted_after_two_scene_successes(ws_tmp):
    """3.2A：相似 + 相关两个场景各验证成功一次后晋升。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    # 相似场景成功
    assert store.verify(pid, ok=True,
                        instruction="服务部署任务超时处理") == "pending"
    # 相关场景成功（任务/超时 两个核心词重叠）
    assert store.verify(pid, ok=True,
                        instruction="任务调度超时重试") == "pending"
    # 第三次成功（任意场景）达到阈值且已覆盖两个场景 -> 晋升
    assert store.verify(pid, ok=True,
                        instruction="部署任务超时") == STATUS_PROMOTED
    report = store.scene_report(pid)
    assert report["similar"] >= 1 and report["related"] >= 1
    assert report["generalized"] is True
    assert store.list(status=STATUS_PROMOTED)
    store.close()


def test_proposal_single_scene_demoted_to_local(ws_tmp):
    """3.2A：仅相似场景反复成功不晋升，应用超限后降级为项目级经验。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"), reject_after=5)
    pid = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    for _ in range(4):
        assert store.verify(pid, ok=True,
                            instruction="服务部署任务超时处理") == "pending"
    assert store.verify(pid, ok=True,
                        instruction="服务部署任务超时处理") == STATUS_LOCAL
    assert store.list(status=STATUS_LOCAL)
    report = store.scene_report(pid)
    assert report["similar"] == 5 and report["related"] == 0
    assert report["generalized"] is False
    store.close()


def test_proposal_legacy_mode_without_generalization(ws_tmp):
    """require_generalization=False 时保持旧的“连续成功即晋升”语义。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          require_generalization=False)
    pid = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    for _ in range(2):
        assert store.verify(pid, ok=True) == "pending"
    assert store.verify(pid, ok=True) == STATUS_PROMOTED
    store.close()


def test_proposal_rejected_after_application_ceiling(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"), reject_after=5)
    pid = store.create_or_bump("retrieval", "找不到符号定义", "增强代码搜索")
    for _ in range(4):
        store.verify(pid, ok=False)
    assert store.verify(pid, ok=False) == STATUS_REJECTED
    store.close()


def test_proposal_user_reject(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("context", "长任务信息丢失", "优化压缩策略")
    assert store.reject(pid) is True
    assert store.list(status=STATUS_REJECTED)
    store.close()


# ---- 3.2B 提议冲突检测 ----

def test_proposal_conflict_requires_higher_threshold(ws_tmp):
    """与已晋升策略冲突时需更高层级验证（默认 5 次）才能覆盖。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          conflict_threshold=5, reject_after=6)
    old = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    store.promote(old)
    pid = store.create_or_bump("tool", "任务超时重试", "升级超时熔断")
    assert store.conflicts_with(pid) == [old]
    # 相似 + 相关各成功一次（达到泛化要求）
    store.verify(pid, ok=True, instruction="重试任务超时处理")
    store.verify(pid, ok=True, instruction="超时重试并记录日志")
    # 第 3 次成功达到常规阈值，但被冲突拦截
    assert store.verify(pid, ok=True,
                        instruction="重试任务超时处理") == "pending"
    report = store.conflict_report(pid)
    assert report["conflicts"] == [old]
    assert report["threshold"] == 5
    # 达到冲突阈值 -> 晋升覆盖旧策略
    assert store.verify(pid, ok=True,
                        instruction="超时重试并记录日志") == "pending"
    assert store.verify(pid, ok=True,
                        instruction="重试任务超时处理") == STATUS_PROMOTED
    assert store.list(status=STATUS_PROMOTED)
    store.close()


def test_proposal_conflict_detector_callback(ws_tmp):
    """自定义冲突检测器（LLM 判定）优先于确定性规则。"""
    def detector(pending, promoted):
        return [q["id"] for q in promoted if q.get("action") == "禁止缓存"]
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          conflict_detector=detector)
    old = store.create_or_bump("tool", "缓存性能下降", "禁止缓存")
    store.promote(old)
    pid = store.create_or_bump("tool", "缓存性能下降", "清理缓存")
    assert store.conflicts_with(pid) == [old]
    store.close()


def test_proposal_no_conflict_when_action_same(ws_tmp):
    """同类别同措施不视为冲突（只是重复登记）。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    old = store.create_or_bump("tool", "部署任务超时", "增强超时管控")
    store.promote(old)
    pid = store.create_or_bump("tool", "任务超时重试", "增强超时管控")
    assert store.conflicts_with(pid) == [], "同措施不应判为冲突"
    store.close()


# ---- 3.3B 基准集类别平衡 ----

def test_benchmark_category_balance(ws_tmp):
    """确认条目形成过载类别后，同类任务降权、低占比类别加分。"""
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    ev = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
          "params": {"action": "edit", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    # 3 条同类别（修复类）任务确认后该类别过载
    for prompt in ("修复登录模块空指针崩溃",
                   "修复订单支付失败",
                   "修复数据同步报错"):
        e = store.evaluate(prompt, _result(ev, tasks))
        assert e and not e.get("duplicate"), "应登记新条目"
        store.confirm(e["id"])
    dist = store.category_distribution()
    assert dist, "已确认条目应产出类别分布"
    assert any(v > 0.4 for v in dist.values()), "单一类别应过载"
    # 过载类别的新任务被降权
    adj, reason = store._category_balance("新增用户接口")
    assert adj < 0 and "过载" in reason
    # 覆盖低占比类别的新任务被加分
    adj2, reason2 = store._category_balance("为登录模块编写文档")
    assert adj2 > 0 and "低占比" in reason2
    # 端到端：过载任务代表性分数低于未过载基线
    fresh = BenchmarkExtractor(path=str(ws_tmp / "b2.json"))
    s0, _ = fresh._representative_score("新增用户接口", ev, tasks, ok=True)
    s1, _ = store._representative_score("新增用户接口", ev, tasks, ok=True)
    assert s1 < s0, "过载类别应拉低代表性分数"
    fresh.close()
    store.close()


# ---- 3.3 基准集提取 ----


def _result(events, tasks, phase="completed"):
    return SimpleNamespace(events=events, tasks=tasks, phase=phase,
                           final_answer="ok")


def test_benchmark_extract_representative_task(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "write", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    entry = store.evaluate("实现登录接口并补充测试", _result(events, tasks))
    assert entry and not entry.get("duplicate")
    assert entry["score"] >= 0.6
    assert store.entries(status="pending")
    store.close()


def test_benchmark_dedup_on_same_instruction(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "edit", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    first = store.evaluate("重构数据层接口", _result(events, tasks))
    second = store.evaluate("重构数据层接口", _result(events, tasks))
    assert first and second
    assert second["duplicate"] is True
    assert len(store.entries()) == 1
    store.close()


def test_benchmark_confirm_and_reject(ws_tmp):
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"))
    events = [{"type": "tool_call", "data": {"success": True, "tool": "file_ops",
              "params": {"action": "append", "path": "a.py"}}}]
    tasks = [SimpleNamespace(id="s0"), SimpleNamespace(id="s1")]
    entry = store.evaluate("新增 API 文档说明", _result(events, tasks))
    assert store.confirm(entry["id"]) is True
    assert store.entries(status="confirmed")
    store.close()


def test_benchmark_trend_warning(ws_tmp):
    prof = CapabilityProfile(path=str(ws_tmp / "cap.json"))
    store = BenchmarkExtractor(path=str(ws_tmp / "bench.json"), profile=prof)
    for _ in range(6):
        prof.record("修复空指针崩溃", ok=True)
    store.update_baseline()
    for _ in range(5):
        prof.record("修复空指针崩溃", ok=False)
    warns = store.trend_warnings()
    assert warns and any("调试定位" in w for w in warns)
    store.close()
    prof.close()


# ---- 集成：AgentLoop 会话结束后画像/提议自动更新 ----

def _make_config(ws_tmp):
    return AppConfig(
        agent=AgentConfig(max_rounds=6, max_retries=1, max_concurrency=1),
        sandbox=SandboxConfig(workspace=str(ws_tmp / "ws")),
        memory=MemoryConfig(backend="sqlite", db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )


class StubPlanner:
    async def plan(self, prompt, context="", call_graph=None,
                   project_context="", capability_profile=""):
        return [Task(id="t0", instruction=prompt, max_retries=0,
                     criticality="critical")]


def test_loop_failure_registers_proposal_and_capability(ws_tmp):
    cfg = _make_config(ws_tmp)
    llm = MockLLM(responder=lambda msgs: "这不是合法输出")
    loop = AgentLoop(config=cfg, llm=llm, planner=StubPlanner())

    async def run():
        try:
            return await loop.run("修复登录模块空指针崩溃")
        finally:
            await loop.close()

    import asyncio
    result = asyncio.run(run())
    assert result.phase.value == "failed"
    names = [d["name"] for d in loop._decision.records()]
    assert "capability.updated" in names
    assert "selflearn.proposal" in names, "失败任务应登记改进提议"
    assert loop.proposals is not None and loop.proposals.list(
        status="pending")
    prof = loop.capability
    assert prof is not None, "能力画像应被装配"
    assert "debug" in prof.summary(), "失败任务应记录调试维度"
    assert prof.score("debug") == 0.0, "单次失败的成功率应为 0"
