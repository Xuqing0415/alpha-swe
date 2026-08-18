"""store 单元测试。"""
import json

from taskboard.models import Priority, Task
from taskboard.store import JsonStore


def test_store_load_empty_when_missing(tmp_path):
    store = JsonStore(str(tmp_path / "nope.json"))
    assert store.load() == []


def test_store_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "tasks.json"
    store = JsonStore(str(p))
    tasks = [Task(title="a"), Task(title="b", priority=Priority.HIGH)]
    store.save(tasks)
    loaded = JsonStore(str(p)).load()
    assert [t.title for t in loaded] == ["a", "b"]
    assert loaded[1].priority.value == "high"


def test_store_load_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "tasks.json"
    p.write_text("{broken json", encoding="utf-8")
    assert JsonStore(str(p)).load() == []


def test_store_save_is_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "tasks.json"
    store = JsonStore(str(p))
    store.save([Task(title="x")])
    leftovers = [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
    assert leftovers == []
