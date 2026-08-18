"""Git 工具测试（方案 3.4）：只读操作、提交、分支、网络策略拦截。"""
import subprocess

import pytest

from agent.sandbox.policy import SandboxPolicy
from agent.tools.base import ExecutionContext
from agent.tools.git_tool import GitTool


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")


@pytest.fixture
def repo(ws_tmp):
    """初始化一个带一次提交的临时 git 仓库。"""
    root = ws_tmp / "repo"
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main", cwd=str(root))
    git("config", "user.name", "test", cwd=str(root))
    git("config", "user.email", "test@example.com", cwd=str(root))
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    git("add", "-A", cwd=str(root))
    git("commit", "-m", "feat: init", cwd=str(root))
    return root


@pytest.mark.asyncio
async def test_git_status_and_diff(repo):
    tool = GitTool()
    ctx = ExecutionContext(workspace=str(repo))
    r = await tool.execute({"action": "status"}, ctx)
    assert r.success
    # 修改文件后 diff 可见
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    r = await tool.execute({"action": "diff"}, ctx)
    assert r.success
    assert "-v1" in r.output and "+v2" in r.output


@pytest.mark.asyncio
async def test_git_log_and_commit(repo):
    tool = GitTool()
    ctx = ExecutionContext(workspace=str(repo))
    r = await tool.execute({"action": "log"}, ctx)
    assert r.success
    assert "feat: init" in r.output

    (repo / "a.txt").write_text("v3\n", encoding="utf-8")
    r = await tool.execute({"action": "commit",
                            "message": "fix: bump a.txt"}, ctx)
    assert r.success, r.error
    r = await tool.execute({"action": "log"}, ctx)
    assert "fix: bump a.txt" in r.output


@pytest.mark.asyncio
async def test_git_branch_and_delete(repo):
    tool = GitTool()
    ctx = ExecutionContext(workspace=str(repo))
    r = await tool.execute({"action": "branch", "branch": "feature/x"}, ctx)
    assert r.success, r.error
    r = await tool.execute({"action": "branch"}, ctx)
    assert r.success
    assert "feature/x" in r.output
    # 删除当前所在分支应被拒绝
    r = await tool.execute({"action": "branch_delete", "branch": "feature/x"}, ctx)
    assert r.success is False
    # 切回 main 后可以删除
    git("checkout", "main", cwd=str(repo))
    r = await tool.execute({"action": "branch_delete", "branch": "feature/x"}, ctx)
    assert r.success, r.error


@pytest.mark.asyncio
async def test_git_commit_requires_message(repo):
    tool = GitTool()
    ctx = ExecutionContext(workspace=str(repo))
    r = await tool.execute({"action": "commit"}, ctx)
    assert r.success is False


def test_git_push_blocked_without_network(repo):
    """网络禁用时 git push 被沙箱策略拦截。"""
    policy = SandboxPolicy(workspace=str(repo), network_enabled=False,
                           network_policy="deny")
    ctx = ExecutionContext(workspace=str(repo))
    ok, reason = policy.check("git_ops", {"action": "push"}, ctx)
    assert ok is False
    assert "网络" in reason
    # 网络启用时放行
    policy2 = SandboxPolicy(workspace=str(repo), network_enabled=True)
    ok2, _ = policy2.check("git_ops", {"action": "push"}, ctx)
    assert ok2 is True
    # 只读操作不受网络策略影响
    ok3, _ = policy.check("git_ops", {"action": "status"}, ctx)
    assert ok3 is True