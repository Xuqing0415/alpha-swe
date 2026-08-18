"""方向一 3.1/3.4：issue→文件推荐、调用图影响面与相关测试选择（离线单测）。"""
import pytest

from agent.code.call_graph import build_call_graph
from agent.code.recommend import (format_recommendations, recommend_files,
                                  score_files)
from agent.code.test_select import select_related_tests


def make_repo(ws_tmp):
    ws = ws_tmp / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "login.py").write_text(
        "def do_login(user, password):\n"
        "    return verify_password(user, password)\n", encoding="utf-8")
    (ws / "src" / "crypto.py").write_text(
        "def verify_password(user, password):\n"
        "    return user.check(password)\n", encoding="utf-8")
    (ws / "src" / "views.py").write_text(
        "def render_home(): return '<html>home</html>'\n", encoding="utf-8")
    (ws / "tests" / "test_login.py").write_text(
        "def test_do_login(): pass\n", encoding="utf-8")
    (ws / "tests" / "test_views.py").write_text(
        "def test_render_home(): pass\n", encoding="utf-8")
    return ws


def test_recommend_login_issue(ws_tmp):
    ws = make_repo(ws_tmp)
    scored = recommend_files(
        "Fix login endpoint: password verification fails during login, verify_password raises an exception", str(ws))
    assert scored and scored[0]["path"] == "src/login.py"
    paths = [r["path"] for r in scored]
    assert "src/login.py" in paths
    assert "src/views.py" not in paths  # 与 issue 无关


def test_recommend_call_graph_boost(ws_tmp):
    ws = make_repo(ws_tmp)
    cg = build_call_graph(str(ws))
    scored = recommend_files(
        "Fix login endpoint password verification failure", str(ws), call_graph=cg)
    paths = [r["path"] for r in scored]
    assert "src/login.py" in paths
    # crypto.py 与 login.py 有调用关系，被影响面提升进候选
    assert "src/crypto.py" in paths
    text = format_recommendations(scored)
    assert "候选文件" in text and "src/login.py" in text


def test_recommend_no_match_returns_empty(ws_tmp):
    ws = make_repo(ws_tmp)
    assert recommend_files("qwerty zxcvbn", str(ws)) == []


def test_score_files_pure_function(ws_tmp):
    ws = make_repo(ws_tmp)
    scored = score_files("verify_password", ["src/login.py", "src/views.py"],
                         root=str(ws))
    by_path = {r["path"]: r for r in scored}
    assert by_path["src/login.py"]["content_hits"] > 0
    assert "src/views.py" not in by_path  # 0 分被过滤


def test_impact_files(ws_tmp):
    ws = make_repo(ws_tmp)
    cg = build_call_graph(str(ws))
    assert "src/crypto.py" in cg.impact_files("src/login.py")
    assert "src/login.py" in cg.impact_files("src/crypto.py")


def test_select_related_tests(ws_tmp):
    ws = make_repo(ws_tmp)
    cg = build_call_graph(str(ws))
    targets = select_related_tests(["src/login.py"], str(ws), call_graph=cg)
    assert "tests/test_login.py" in targets
    # crypto.py 的调用方（login.py）对应测试也会被选中
    targets2 = select_related_tests(["src/crypto.py"], str(ws),
                                    call_graph=cg)
    assert "tests/test_login.py" in targets2
    assert select_related_tests(["src/views.py"], str(ws)) == [
        "tests/test_views.py"]


@pytest.mark.asyncio
async def test_loop_injects_recommendations_and_test_targets(ws_tmp):
    from agent.config import (AgentConfig, AppConfig, MCPOptions,
                              MemoryConfig, SandboxConfig)
    from agent.core.loop import AgentLoop
    from agent.core.task import Task
    from agent.llm import MockLLM

    class RecordingLLM(MockLLM):
        def __init__(self, *responses):
            super().__init__()
            self._responses = list(responses)

        async def complete(self, messages):
            assert self._responses, "LLM 响应耗尽"
            return self._responses.pop(0)

    class RecPlanner:
        def __init__(self):
            self.received = {}

        async def plan(self, prompt, context="", recommended_files=""):
            self.received["recommended_files"] = recommended_files
            return [Task(id="t0", instruction=prompt, criticality="critical")]

    ws = make_repo(ws_tmp)
    cfg = AppConfig(
        agent=AgentConfig(max_rounds=4, max_retries=1, max_concurrency=1,
                          recommend_files_enabled=True, auto_test_select=True),
        sandbox=SandboxConfig(workspace=str(ws)),
        memory=MemoryConfig(db_path=str(ws_tmp / "mem.db")),
        mcp=MCPOptions(enabled=False),
    )
    llm = RecordingLLM('{"final_answer": "done"}')
    planner = RecPlanner()
    loop = AgentLoop(config=cfg, llm=llm, planner=planner)
    try:
        r = await loop.run("Fix the password verification failure in the login endpoint")
        assert r.ok
        assert "src/login.py" in planner.received.get("recommended_files", "")
        tool = loop.tools.get("run_tests")
        assert tool is not None
        assert "tests/test_login.py" in (tool.related_targets or [])
        names = [d["name"] for d in loop._decision.records()]
        assert "recommend_files" in names
    finally:
        await loop.close()
