# -*- coding: utf-8 -*-
"""3.2C：晋升后观察期与回归测试（临时禁用 / 对比 / 降级）。"""
from agent.selfimprove import (ProposalStore, STATUS_LOCAL,
                               STATUS_PROMOTED)


def _promote(store, category="tool", action="增强超时管控",
             instruction="部署任务超时"):
    """相似 + 相关两个场景各验证成功一次后晋升（默认阈值 3 次成功）。"""
    pid = store.create_or_bump(category, instruction, action)
    assert store.verify(pid, ok=True,
                        instruction="服务部署任务超时处理") == "pending"
    assert store.verify(pid, ok=True,
                        instruction="任务调度超时重试") == "pending"
    assert store.verify(pid, ok=True,
                        instruction=instruction) == STATUS_PROMOTED
    return pid


def test_promotion_auto_attaches_watch(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = _promote(store)
    watch = store.watch_status(pid)
    assert watch["phase"] == "watching"
    assert watch["window"] == 10
    assert watch["applied"] == 0
    assert watch["proposal_ok"] == 0
    assert watch["system_fail"] == 0
    assert watch["suspended"] is False
    assert watch["completed"] is False
    store.close()


def test_observe_application_accounting_separate_and_noop(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = _promote(store)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    store.observe_application(pid, proposal_ok=False, system_ok=False)
    store.observe_application(pid, proposal_ok=True)  # system_ok 缺省同 proposal
    store.observe_application(pid, proposal_ok=False)  # 缺省 -> system 失败
    watch = store.watch_status(pid)
    assert watch["applied"] == 4
    assert watch["proposal_ok"] == 2
    assert watch["system_fail"] == 2
    # 未晋升的提议记账无效
    pending = store.create_or_bump("tool", "缓存性能下降", "清理缓存")
    assert store.observe_application(pending, True) == {}
    # 已 suspend 的提议记账无效
    assert store.suspend(pid) is True
    before = store.watch_status(pid)
    store.observe_application(pid, proposal_ok=False, system_ok=False)
    after = store.watch_status(pid)
    assert after["applied"] == before["applied"]
    assert after["system_fail"] == before["system_fail"]
    store.close()


def test_system_failure_ratio_suspends_even_when_proposal_ok(ws_tmp):
    """提议本身全成功，但系统整体失败占比达阈值仍临时禁用。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          watch_window=4, watch_disable_threshold=0.5)
    pid = _promote(store)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    store.observe_application(pid, proposal_ok=True, system_ok=False)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    watch = store.observe_application(pid, proposal_ok=True, system_ok=False)
    assert watch["phase"] == "suspended"
    assert watch["suspended"] is True
    assert watch["applied"] == 4 and watch["system_fail"] == 2
    assert watch["proposal_ok"] == 4
    assert watch["regression"] is True
    store.close()


def test_early_suspend_after_three_system_failures(ws_tmp):
    """applied 未到窗口但 system_fail>=3 时提前触发回溯。"""
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          watch_window=10, watch_disable_threshold=0.5)
    pid = _promote(store)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    store.observe_application(pid, proposal_ok=True, system_ok=False)
    store.observe_application(pid, proposal_ok=True, system_ok=False)
    watch = store.observe_application(pid, proposal_ok=True, system_ok=False)
    assert watch["applied"] == 5 < watch["window"]
    assert watch["system_fail"] == 3
    assert watch["phase"] == "suspended"
    store.close()


def test_watch_completes_without_regression(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          watch_window=3, watch_disable_threshold=0.5)
    pid = _promote(store)
    for _ in range(3):
        watch = store.observe_application(pid, proposal_ok=True,
                                          system_ok=True)
    assert watch["phase"] == "completed"
    assert watch["completed"] is True
    assert watch["suspended"] is False
    assert watch["regression"] is False
    store.close()


def test_resume_reopens_watch_window(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          watch_window=3, watch_disable_threshold=0.5)
    pid = _promote(store)
    store.observe_application(pid, proposal_ok=True, system_ok=True)
    store.observe_application(pid, proposal_ok=False, system_ok=False)
    watch = store.observe_application(pid, proposal_ok=False, system_ok=False)
    assert watch["phase"] == "suspended"
    assert store.resume(pid) is True
    watch = store.watch_status(pid)
    assert watch["phase"] == "watching"
    assert watch["applied"] == 0
    assert watch["proposal_ok"] == 0
    assert watch["system_fail"] == 0
    assert watch["suspended"] is False and watch["completed"] is False
    # resume 后重新计数，可再次走完观察期
    for _ in range(3):
        store.observe_application(pid, proposal_ok=True, system_ok=True)
    assert store.watch_status(pid)["phase"] == "completed"
    store.close()


def test_demote_promoted_to_local_with_reason(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = _promote(store)
    assert store.demote(pid) is True
    local = [p for p in store.list(status=STATUS_LOCAL) if p["id"] == pid]
    assert local
    assert local[0]["demoted_reason"] == "watch_regression"
    assert local[0].get("demoted_at")
    assert store.demote(pid) is False, "已 LOCAL 不应重复降级"
    pending = store.create_or_bump("tool", "缓存性能下降", "清理缓存")
    assert store.demote(pending) is False, "非 PROMOTED 无法降级"
    assert store.demote("missing") is False
    pid2 = _promote(store, category="planning", action="重构调度逻辑")
    assert store.demote(pid2, reason="人工评审") is True
    local2 = [p for p in store.list(status=STATUS_LOCAL) if p["id"] == pid2]
    assert local2 and local2[0]["demoted_reason"] == "人工评审"
    store.close()


def test_watch_report_and_status_fields(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"),
                          watch_window=5)
    pid = _promote(store)
    _promote(store, category="planning", action="增强失败恢复")
    store.observe_application(pid, proposal_ok=True, system_ok=False)
    report = store.watch_report()
    assert len(report) == 2
    keys = {"pid", "category", "phase", "suspended", "applied",
            "proposal_ok", "system_fail", "window", "regression"}
    assert all(set(row) == keys for row in report)
    row = next(r for r in report if r["pid"] == pid)
    assert row["applied"] == 1 and row["system_fail"] == 1
    assert row["window"] == 5 and row["phase"] == "watching"
    watch = store.watch_status(pid)
    assert watch["started_at"] and watch["phase"] == "watching"
    assert store.watch_status("missing") == {}
    store.close()


def test_manual_promote_also_attaches_watch(ws_tmp):
    store = ProposalStore(path=str(ws_tmp / "proposals.json"))
    pid = store.create_or_bump("tool", "缓存性能下降", "清理缓存")
    assert store.promote(pid) is True
    watch = store.watch_status(pid)
    assert watch and watch["phase"] == "watching"
    assert [p["pid"] for p in store.watch_report()] == [pid]
    store.close()
