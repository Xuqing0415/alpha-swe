"""models 单元测试。"""
import pytest

from taskboard.models import Priority, Status, Task


def test_priority_parse_case_insensitive():
    assert Priority.parse("HIGH") == Priority.HIGH
    assert Priority.parse("critical") == Priority.CRITICAL


def test_priority_parse_invalid_raises():
    with pytest.raises(ValueError):
        Priority.parse("urgent")


def test_status_parse_underscore():
    assert Status.parse("in progress") == Status.IN_PROGRESS
    assert Status.parse("in_progress") == Status.IN_PROGRESS


def test_task_defaults():
    t = Task(title="写测试")
    assert t.priority == Priority.MEDIUM
    assert t.status == Status.TODO
    assert t.id
    assert t.tags == []


def test_task_complete_records_spent():
    t = Task(title="完成事项", total_estimate=60)
    t.complete(spent=45)
    assert t.is_done()
    assert t.spent == 45
    assert t.updated_at >= t.created_at


def test_task_roundtrip_dict():
    t = Task(title="往返", priority=Priority.HIGH, tags=["a", "b"],
             total_estimate=30)
    d = t.to_dict()
    t2 = Task.from_dict(d)
    assert t2.title == t.title
    assert t2.priority == t.priority
    assert t2.tags == ["a", "b"]
    assert t2.total_estimate == 30
