# -*- coding: utf-8 -*-
"""方向三：OpenAPI 文档与后端实现一致性测试。

确保 /openapi.json 满足第三方对接要求：
- 统一 Bearer 鉴权声明（components.securitySchemes.bearerAuth）；
- 受保护接口全部标注 security；
- 成功/错误响应在文档中均有 schema/示例；
- 审计接口查询参数、task_id 路径格式约束完整。
"""
import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app

ADMIN_KEY = "as_admin_test_key_1234567890"

# 匿名接口（无需 Bearer）
ANONYMOUS = {("/healthz", "get"), ("/api/v1/auth/token", "post")}


@pytest.fixture
def client(ws_tmp):
    async def runner(record):
        return ({"ok": True, "status": "completed", "exit_code": 0,
                 "final_answer": "done", "rounds": 1, "tokens": 10,
                 "llm_calls": 1, "elapsed_s": 0.1, "files_modified": []},
                None, "completed")

    cfg = ServerConfig(
        db_path=str(ws_tmp / "openapi.db"),
        workspace_root=str(ws_tmp / "ws"),
        config_path="config/agent.yaml",
        max_concurrency=1, admin_api_key=ADMIN_KEY)
    app = create_app(cfg, runner=runner)
    with TestClient(app) as c:
        yield c


def _spec(client):
    return client.get("/openapi.json").json()


def test_security_scheme_declared(client):
    spec = _spec(client)
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_protected_routes_declare_bearer(client):
    spec = _spec(client)
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            key = (path, method)
            if key in ANONYMOUS:
                assert "security" not in op or op["security"] == [], key
            else:
                assert op.get("security") == [{"bearerAuth": []}], key


def test_success_responses_have_schema_or_example(client):
    spec = _spec(client)
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            for code in ("200", "201"):
                if code not in op.get("responses", {}):
                    continue
                content = op["responses"][code].get("content", {})
                if "text/event-stream" in content:
                    assert content["text/event-stream"].get("example"), \
                        f"{method.upper()} {path} SSE 示例缺失"
                else:
                    schema = content.get("application/json", {}).get("schema")
                    assert schema, f"{method.upper()} {path} {code} 缺 schema"


def test_error_codes_documented_on_tasks(client):
    spec = _spec(client)
    op = spec["paths"]["/api/v1/tasks/{task_id}"]["get"]
    codes = {int(k) for k in op["responses"]}
    assert {401, 403, 404, 422} <= codes


def test_auth_token_401_documented(client):
    spec = _spec(client)
    op = spec["paths"]["/api/v1/auth/token"]["post"]
    assert 401 in {int(k) for k in op["responses"]}


def test_audit_query_params_documented(client):
    spec = _spec(client)
    op = spec["paths"]["/api/v1/audit"]["get"]
    params = {p["name"]: p for p in op.get("parameters", [])}
    assert set(params) >= {"user_id", "task_id", "start_time", "end_time",
                           "limit", "offset"}


def test_task_id_path_pattern_documented(client):
    spec = _spec(client)
    op = spec["paths"]["/api/v1/tasks/{task_id}"]["get"]
    params = {p["name"]: p for p in op.get("parameters", [])}
    pattern = params["task_id"].get("schema", {}).get("pattern", "")
    assert pattern.startswith("^task_")


def test_role_enum_and_task_status_documented(client):
    spec = _spec(client)
    user_schema = spec["components"]["schemas"]["UserCreate"]
    assert set(user_schema["properties"]["role"]["enum"]) == {
        "admin", "developer", "observer"}
    status_prop = spec["components"]["schemas"]["TaskOut"]["properties"][
        "status"]
    assert "description" in status_prop
