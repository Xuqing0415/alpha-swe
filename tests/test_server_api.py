# -*- coding: utf-8 -*-
"""方向三：FastAPI 服务层 API 测试（认证/角色/任务生命周期/SSE/审计）。"""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app

ADMIN_KEY = "as_admin_test_key_1234567890"
DEV_KEY = "as_dev_test_key_1234567890"
OBS_KEY = "as_obs_test_key_1234567890"


def _make_payload(exit_code=0, status="completed"):
    return {
        "ok": exit_code == 0, "status": status, "exit_code": exit_code,
        "final_answer": "done", "rounds": 2, "tokens": 100,
        "llm_calls": 3, "elapsed_s": 1.0, "files_modified": ["a.py"],
    }


@pytest.fixture
def slow_api(ws_tmp):
    """慢 runner：模拟长任务，便于测试取消。"""
    async def runner(record):
        await asyncio.sleep(5)
        return _make_payload(), None, "completed"

    cfg = ServerConfig(
        db_path=str(ws_tmp / "server_slow.db"),
        workspace_root=str(ws_tmp / "ws_slow"),
        config_path="config/agent.yaml",
        max_concurrency=1, admin_api_key=ADMIN_KEY)
    app = create_app(cfg, runner=runner)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api(ws_tmp):
    """带假 runner 的应用（写文件、立即可完成）。"""
    async def runner(record):
        (ws_tmp / "runner_called.txt").open("a", encoding="utf-8").write(
            record.id + "\n")
        return _make_payload(), None, "completed"

    cfg = ServerConfig(
        db_path=str(ws_tmp / "server.db"),
        workspace_root=str(ws_tmp / "ws"),
        config_path="config/agent.yaml",
        max_concurrency=2, admin_api_key=ADMIN_KEY)
    app = create_app(cfg, runner=runner)
    with TestClient(app) as client:
        yield client, ws_tmp


def _auth(client, key):
    r = client.post("/api/v1/auth/token", json={"api_key": key})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {key}"}


def _wait_task(client, headers, task_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("completed", "failed", "cancelled",
                              "timeout", "budget"):
            return data
        time.sleep(0.05)
    raise AssertionError("任务未在超时内结束")


def test_admin_token_and_health(api):
    client, _ = api
    headers = _auth(client, ADMIN_KEY)
    assert client.get("/healthz").json()["ok"] is True
    me = client.get("/api/v1/me", headers=headers)
    assert me.json()["role"] == "admin"


def test_auth_required(api):
    client, _ = api
    assert client.get("/api/v1/tasks").status_code == 401
    assert client.post("/api/v1/tasks",
                       json={"instruction": "hi"}).status_code == 401


def test_create_users_and_roles(api):
    client, _ = api
    admin_h = _auth(client, ADMIN_KEY)
    for name, role, key in (("dev", "developer", DEV_KEY),
                            ("obs", "observer", OBS_KEY)):
        r = client.post("/api/v1/users", headers=admin_h,
                        json={"name": name, "role": role})
        assert r.status_code == 201, r.text
        # 覆盖为测试固定 Key：直接删掉随机 key 并注入固定 key
        uid = r.json()["user"]["id"]
        # 通过 issue_key 拿新 key 不可控，改为审计跳过；直接用 admin 签发
    obs = client.get("/api/v1/users", headers=admin_h).json()
    assert any(u["name"] == "obs" for u in obs)


def _fixed_key_user(client, admin_h, name, role, key):
    r = client.post("/api/v1/users", headers=admin_h,
                    json={"name": name, "role": role})
    assert r.status_code == 201
    uid = r.json()["user"]["id"]
    # 用 admin 的 store 无法直连，因此直接调用 auth/token 验证随机 key 可用
    k = r.json()["api_key"]
    assert client.post("/api/v1/auth/token",
                       json={"api_key": k}).status_code == 200
    return k


def test_observer_cannot_submit(api):
    client, _ = api
    admin_h = _auth(client, ADMIN_KEY)
    obs_key = _fixed_key_user(client, admin_h, "obs1", "observer", OBS_KEY)
    obs_h = {"Authorization": f"Bearer {obs_key}"}
    r = client.post("/api/v1/tasks", headers=obs_h,
                    json={"instruction": "do something"})
    assert r.status_code == 403


def test_task_lifecycle(api):
    client, ws_tmp = api
    admin_h = _auth(client, ADMIN_KEY)
    dev_key = _fixed_key_user(client, admin_h, "dev1", "developer", DEV_KEY)
    dev_h = {"Authorization": f"Bearer {dev_key}"}
    r = client.post("/api/v1/tasks", headers=dev_h,
                    json={"instruction": "fix the bug"})
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    assert r.json()["status"] == "queued"
    assert "user_" in r.json()["workspace"]

    data = _wait_task(client, dev_h, task_id)
    assert data["status"] == "completed"
    assert data["result"]["tokens"] == 100
    assert data["result"]["files_modified"] == ["a.py"]
    assert (ws_tmp / "runner_called.txt").exists()


def test_task_cancel(slow_api):
    client = slow_api
    admin_h = _auth(client, ADMIN_KEY)
    dev_key = _fixed_key_user(client, admin_h, "dev2", "developer", DEV_KEY)
    dev_h = {"Authorization": f"Bearer {dev_key}"}
    r = client.post("/api/v1/tasks", headers=dev_h,
                    json={"instruction": "long task"})
    task_id = r.json()["id"]
    # 立即取消（此时大概率仍在排队或刚开始）
    r2 = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=dev_h)
    assert r2.status_code == 200
    data = _wait_task(client, dev_h, task_id)
    assert data["status"] == "cancelled"


def test_task_visibility_isolation(api):
    client, _ = api
    admin_h = _auth(client, ADMIN_KEY)
    dev_key = _fixed_key_user(client, admin_h, "dev3", "developer", DEV_KEY)
    dev_h = {"Authorization": f"Bearer {dev_key}"}
    r = client.post("/api/v1/tasks", headers=dev_h,
                    json={"instruction": "task A"})
    task_id = r.json()["id"]
    _wait_task(client, dev_h, task_id)
    # 其他开发者看不到该任务
    dev2_key = _fixed_key_user(client, admin_h, "dev4", "developer", DEV_KEY)
    dev2_h = {"Authorization": f"Bearer {dev2_key}"}
    assert client.get("/api/v1/tasks", headers=dev2_h).json() == []
    assert client.get(f"/api/v1/tasks/{task_id}",
                      headers=dev2_h).status_code == 403
    # admin 可见
    assert len(client.get("/api/v1/tasks", headers=admin_h).json()) >= 1


def test_sessions_and_audit(api):
    client, _ = api
    admin_h = _auth(client, ADMIN_KEY)
    dev_key = _fixed_key_user(client, admin_h, "dev5", "developer", DEV_KEY)
    dev_h = {"Authorization": f"Bearer {dev_key}"}
    r = client.post("/api/v1/sessions", headers=dev_h,
                    json={"label": "sprint-1"})
    assert r.status_code == 201
    assert len(client.get("/api/v1/sessions", headers=dev_h).json()) == 1
    r = client.post("/api/v1/tasks", headers=dev_h,
                    json={"instruction": "audit me"})
    assert r.status_code == 201
    _wait_task(client, dev_h, r.json()["id"])
    # 审计含 task_submit / user_create
    audit = client.get("/api/v1/audit", headers=admin_h).json()
    actions = {a["action"] for a in audit}
    assert "user_create" in actions
    assert "task_submit" in actions


def test_events_sse(api):
    client, _ = api
    admin_h = _auth(client, ADMIN_KEY)
    dev_key = _fixed_key_user(client, admin_h, "dev6", "developer", DEV_KEY)
    dev_h = {"Authorization": f"Bearer {dev_key}"}
    r = client.post("/api/v1/tasks", headers=dev_h,
                    json={"instruction": "watch me"})
    task_id = r.json()["id"]
    _wait_task(client, dev_h, task_id)
    # 任务已结束：订阅应立即收到终态 + done
    with client.stream("GET", f"/api/v1/tasks/{task_id}/events",
                       headers=dev_h) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert "event: completed" in body
    assert "event: done" in body
