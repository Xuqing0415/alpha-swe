"""Board 单元测试。"""
import pytest

from taskboard.board import Board
from taskboard.models import Priority


def make_board(tmp_path):
    return Board(path=str(tmp_path / "tasks.json"))


def test_add_creates_task(tmp_path):
    b = make_board(tmp_path)
    t = b.add("写文档", priority="high", tags=["docs"])
    assert t.id
    assert b.get(t.id).title == "写文档"
    assert b.all()[0].priority == Priority.HIGH


def test_add_parse_invalid_priority_raises(tmp_path):
    b = make_board(tmp_path)
    with pytest.raises(ValueError):
        b.add("非法优先级", priority="urgent")


def test_update_fields(tmp_path):
    b = make_board(tmp_path)
    t = b.add("改标题", tags=["old"])
    b.update(t.id, title="新标题", priority="critical", tags=["new"])
    got = b.get(t.id)
    assert got.title == "新标题"
    assert got.priority == Priority.CRITICAL
    assert got.tags == ["new"]


def test_update_missing_returns_none(tmp_path):
    b = make_board(tmp_path)
    assert b.update("no-such-id", title="x") is None


def test_complete_and_delete(tmp_path):
    b = make_board(tmp_path)
    t = b.add("收尾")
    assert b.complete(t.id, spent=20)
    assert b.get(t.id).is_done()
    assert b.delete(t.id)
    assert b.get(t.id) is None
    assert not b.delete(t.id)


def test_search_finds_substring(tmp_path):
    b = make_board(tmp_path)
    b.add("修复登录页空指针")
    b.add("优化查询性能")
    hits = b.search("登录")
    assert len(hits) == 1
    assert hits[0].title == "修复登录页空指针"


def test_filter_by_status_and_priority(tmp_path):
    b = make_board(tmp_path)
    b.add("低优任务", priority="low")
    b.add("高优任务", priority="high")
    b.complete(b.all()[0].id)
    assert len(b.filter_by(status="done")) == 1
    assert len(b.filter_by(priority="high")) == 1
    assert len(b.filter_by(status="todo", priority="high")) == 1


def test_find_by_tag(tmp_path):
    b = make_board(tmp_path)
    b.add("任务A", tags=["bug", "frontend"])
    b.add("任务B", tags=["bug"])
    assert len(b.find_by_tag("bug")) == 2
    assert len(b.find_by_tag("frontend")) == 1
    assert len(b.find_by_tag("missing")) == 0


def test_stats(tmp_path):
    b = make_board(tmp_path)
    b.add("任务1", priority="high")
    b.add("任务2", priority="low")
    b.all()[0].total_estimate = 60
    b.store.save(b.all())
    s = b.stats()
    assert s["total"] == 2
    assert s["by_priority"]["high"] == 1
    assert s["avg_estimate_minutes"] == 30.0
