"""Git 工具 —— 方案 3.4：状态/差异/历史（只读）+ 提交/推送/分支（写，需确认）。

- 只读操作：status / diff / log / branch（列表）；
- 写操作：commit / push / branch_delete，由循环层 require_confirmation 把关；
- git push 通过网络策略拦截（沙箱 network_policy）；
- 统一 GIT_TERMINAL_PROMPT=0，避免交互卡死。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.tools.base import ErrorCategory, ExecutionContext, Tool, ToolResult

GIT_TIMEOUT = 30.0  # 单条 git 命令超时秒数


def _decode(data: bytes) -> str:
    candidates = ["utf-8"]
    try:
        import locale
        enc = locale.getpreferredencoding(False)
        if enc and enc.lower() not in ("utf-8", "utf8"):
            candidates.append(enc)
    except Exception:
        pass
    candidates += ["gbk", "cp936", "big5", "cp1252"]
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class GitTool(Tool):
    """封装 git 常用操作；写操作必须由上层确认策略放行。"""
    name = "git_ops"
    description = ("Git 版本管理: status 查看变更、diff 查看差异、log 查看历史、"
                   "branch 列出/创建分支、commit 提交变更、push 推送、"
                   "branch_delete 删除分支。写操作（commit/push/branch_delete）"
                   "需要用户确认。")
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "log", "branch", "commit",
                         "push", "branch_delete"],
            },
            "message": {"type": "string",
                        "description": "提交信息（action=commit），遵循 conventional commits"},
            "branch": {"type": "string",
                       "description": "分支名（action=branch 创建 / branch_delete / push）"},
            "path": {"type": "string",
                     "description": "限定 diff/log 的范围路径（可选）"},
        },
        "required": ["action"],
    }

    def __init__(self, decision_logger=None):
        self.decision_logger = decision_logger

    def _log(self, name: str, config_key: str, config_value: Any,
             decision: str) -> None:
        if self.decision_logger is not None:
            try:
                self.decision_logger.record(name, config_key, config_value,
                                            decision)
            except Exception:
                pass

    async def _run(self, args: List[str], workspace: str,
                   timeout: float = GIT_TIMEOUT) -> Tuple[int, str, str]:
        """执行 git 命令；超时返回错误分类为 TRANSIENT 的结果。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args, cwd=workspace,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     "GIT_ASKPASS": "", "PYTHONUTF8": "1"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise
        return proc.returncode, _decode(out).strip(), _decode(err).strip()

    async def _current_branch(self, workspace: str) -> Optional[str]:
        code, out, _ = await self._run(["rev-parse", "--abbrev-ref", "HEAD"],
                                       workspace)
        return out if code == 0 and out and out != "HEAD" else None

    async def _verify_branch(self, branch: str, workspace: str) -> bool:
        """校验分支真实创建：ref 存在，或 HEAD 为指向该分支的 unborn 状态。

        Windows 沙箱下 checkout -b 曾出现 exit 0 但分支 ref 未创建的问题；
        而全新仓库（无首次提交）中分支 ref 本身合法地不存在，此时用
        symbolic-ref 校验 HEAD 是否已指向新分支，两种场景都能覆盖。
        """
        code, _, _ = await self._run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            workspace)
        if code == 0:
            return True
        code2, head, _ = await self._run(
            ["symbolic-ref", "--quiet", "HEAD"], workspace)
        return code2 == 0 and head.strip() == f"refs/heads/{branch}"

    async def execute(self, params: Dict[str, Any],
                      context: ExecutionContext) -> ToolResult:
        action = str(params.get("action", ""))
        start = time.time()
        workspace = context.workspace
        try:
            if action == "status":
                code, out, err = await self._run(["status", "--short", "--branch"],
                                                 workspace)
                detail = ""
                if code == 0:
                    code2, stat, _ = await self._run(
                        ["diff", "--stat"], workspace)
                    detail = ("\n" + stat) if code2 == 0 and stat else ""
                self._log("git.status", "tools.git_ops", action,
                          "git status 查询变更")
                return self._result(code, out + detail, err, start, "status")

            if action == "diff":
                args = ["diff"]
                path = str(params.get("path", "") or "").strip()
                if path:
                    args.append("--")
                    args.append(path)
                code, out, err = await self._run(args, workspace)
                self._log("git.diff", "tools.git_ops", action,
                          f"git diff 查询变更（path={path or '全部'}）")
                return self._result(code, out, err, start, "diff")

            if action == "log":
                args = ["log", "--oneline", "-20", "--decorate"]
                path = str(params.get("path", "") or "").strip()
                if path:
                    args.append("--")
                    args.append(path)
                code, out, err = await self._run(args, workspace)
                self._log("git.log", "tools.git_ops", action,
                          "git log 查询提交历史")
                return self._result(code, out, err, start, "log")

            if action == "branch":
                if params.get("branch"):
                    branch = str(params["branch"]).strip()
                    if not branch:
                        return ToolResult(
                            success=False, error="branch 创建需要非空分支名",
                            elapsed_ms=(time.time() - start) * 1000,
                            error_category=ErrorCategory.PERMANENT,
                        )
                    code, out, err = await self._run(
                        ["switch", "-c", branch], workspace)
                    if code == 0 and not await self._verify_branch(
                            branch, workspace):
                        return ToolResult(
                            success=False,
                            error=(f"分支 {branch} 创建后校验失败："
                                   f"show-ref 未找到且 HEAD 未指向该分支"),
                            output=out,
                            elapsed_ms=(time.time() - start) * 1000,
                            error_category=ErrorCategory.PERMANENT,
                        )
                    self._log("git.branch_create", "tools.git_ops", action,
                              f"创建并切换分支: {branch}")
                    return self._result(code, out, err, start, "branch_create")
                code, out, err = await self._run(
                    ["branch", "--list", "--no-color"], workspace)
                self._log("git.branch", "tools.git_ops", action,
                          "git branch 列出分支")
                return self._result(code, out, err, start, "branch")

            if action == "branch_delete":
                branch = str(params.get("branch", "")).strip()
                if not branch:
                    return ToolResult(
                        success=False, error="branch_delete 需要 branch 参数",
                        elapsed_ms=(time.time() - start) * 1000,
                        error_category=ErrorCategory.PERMANENT,
                    )
                current = await self._current_branch(workspace)
                if current == branch:
                    return ToolResult(
                        success=False,
                        error=f"不能删除当前所在分支: {branch}（先切换分支）",
                        elapsed_ms=(time.time() - start) * 1000,
                        error_category=ErrorCategory.PERMANENT,
                    )
                code, out, err = await self._run(
                    ["branch", "-D", branch], workspace)
                self._log("git.branch_delete", "tools.git_ops", action,
                          f"删除分支: {branch}")
                return self._result(code, out, err, start, "branch_delete")

            if action == "commit":
                message = str(params.get("message", "")).strip()
                if not message:
                    return ToolResult(
                        success=False, error="commit 需要 message 参数",
                        elapsed_ms=(time.time() - start) * 1000,
                        error_category=ErrorCategory.PERMANENT,
                    )
                code, out, err = await self._run(["add", "-A"], workspace)
                if code != 0:
                    return self._result(code, out, err, start, "commit.add")
                code, out, err = await self._run(
                    ["commit", "-m", message], workspace)
                self._log("git.commit", "tools.git_ops", action,
                          f"提交变更（conventional commits）: {message[:120]}")
                return self._result(code, out, err, start, "commit")

            if action == "push":
                branch = str(params.get("branch", "") or "").strip()
                if not branch:
                    branch = await self._current_branch(workspace)
                if not branch:
                    return ToolResult(
                        success=False, error="无法确定推送分支（非 git 仓库或 HEAD 分离）",
                        elapsed_ms=(time.time() - start) * 1000,
                        error_category=ErrorCategory.PERMANENT,
                    )
                code, out, err = await self._run(
                    ["push", "origin", branch], workspace)
                self._log("git.push", "tools.git_ops", action,
                          f"推送分支 origin/{branch}")
                return self._result(code, out, err, start, "push")

            return ToolResult(
                success=False, error=f"未知操作: {action}",
                elapsed_ms=(time.time() - start) * 1000,
                error_category=ErrorCategory.PERMANENT,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"git {action} 执行超时（{GIT_TIMEOUT:.0f}s）并被终止",
                elapsed_ms=(time.time() - start) * 1000,
                metadata={"timed_out": True},
                error_category=ErrorCategory.TRANSIENT,
            )
        except Exception as e:
            return ToolResult(
                success=False, error=f"git {action} 执行异常: {e}",
                elapsed_ms=(time.time() - start) * 1000,
                error_category=ErrorCategory.UNKNOWN,
            )

    def _result(self, code: int, out: str, err: str, start: float,
                action: str) -> ToolResult:
        elapsed = (time.time() - start) * 1000
        if code == 0:
            return ToolResult(
                success=True, output=out or "(无输出)",
                metadata={"git_action": action, "exit_code": code},
                elapsed_ms=elapsed,
            )
        return ToolResult(
            success=False, output=out, error=err or f"git 退出码 {code}",
            metadata={"git_action": action, "exit_code": code},
            elapsed_ms=elapsed,
            error_category=ErrorCategory.PERMANENT,
        )
