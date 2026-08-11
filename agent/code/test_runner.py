"""测试运行器（方案 3.3）——统一接口 + 失败结构化分析。

支持 pytest / jest / go test / unittest：
- 统一 run_tests 入口（framework + target + 超时）；
- 失败分析把「失败用例名 / 断言差异 / 堆栈」结构化提取；
- 覆盖率为可选注入（pytest-cov）。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 失败行提取：pytest short summary 的 FAILED 行
PYTEST_FAILED_LINE = re.compile(
    r"FAILED\s+([^\s]+)\s*-\s*(.*)$", re.M
)
# 通用失败/断言标记（行首或前缀）
GENERIC_FAIL_MARK = re.compile(
    r"(?i)^(\s*)(FAILED|✕|✗|×|\bERROR\b|\bFAIL\b|\bAssertionError\b)"
)
# pytest E 断言差异行（E       assert ...）
ASSERTION_LINE = re.compile(r"^\s*E\s+(assert\s+.+)$", re.M)
# 期望 vs 实际
EXPECTED_ACTUAL = re.compile(
    r"(?is)(assert|expected)\s+(.+?)\s+==\s+(.+?)(\n|$)")

FAILED_TEST_MARKERS = ("FAILED", "✕", "✗", "×", "AssertionError", "Assertion failed")


@dataclass
class TestCaseFailure:
    name: str
    reason: str = ""
    assertion: str = ""
    traceback: str = ""


@dataclass
class TestResult:
    success: bool
    output: str = ""
    framework: str = ""
    duration_ms: float = 0.0
    failures: List[TestCaseFailure] = field(default_factory=list)
    coverage: Optional[float] = None

    @property
    def summary(self) -> str:
        """给 LLM 的失败摘要：用例名 + 断言差异（保留路径与行号）。"""
        if self.success:
            return f"测试通过（{self.framework}，{self.duration_ms:.0f}ms）"
        lines = [f"测试失败: {len(self.failures)} 个用例失败（{self.framework}）"]
        for f in self.failures[:10]:
            head = f"{f.name}"
            if f.assertion:
                head += f" - {f.assertion[:160]}"
            elif f.reason:
                head += f" - {f.reason[:160]}"
            lines.append(f"  {head}")
        return "\n".join(lines)


def parse_test_output(framework: str, output: str) -> List[TestCaseFailure]:
    """把测试输出解析为结构化失败列表（方案 3.3：失败分析）。"""
    failures: List[TestCaseFailure] = []
    if framework == "pytest":
        for m in PYTEST_FAILED_LINE.finditer(output):
            name = m.group(1)
            reason = (m.group(2) or "").strip()
            # 向前搜索该用例的断言块（最近一段 E assert）
            assert_block = ASSERTION_LINE.findall(output)
            assertion = ""
            for blk in reversed(assert_block):
                if output.rfind(blk, 0, m.start()) != -1:
                    assertion = blk.strip()
                    break
            failures.append(TestCaseFailure(name=name, reason=reason,
                                            assertion=assertion))
        if not failures:
            # 兜底：逐行找失败标记
            for m in GENERIC_FAIL_MARK.finditer(output):
                line = output[m.start():].splitlines()[0].strip()
                if "::" in line or "assert" in line:
                    failures.append(TestCaseFailure(name=line[:120],
                                                    reason=line[:160]))
                if len(failures) >= 10:
                    break
        return failures

    # jest / go / 通用：按行抓失败用例名
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in FAILED_TEST_MARKERS) and (
                "✕" in stripped or "✗" in stripped or "×" in stripped
                or "FAILED" in stripped or stripped.startswith("---")
                or ":" in stripped[:60]):
            name = re.split(r"\s+[-–]+\s+", stripped, maxsplit=1)
            failures.append(TestCaseFailure(
                name=name[0][:120],
                reason=name[1][:160] if len(name) > 1 else "",
            ))
        if len(failures) >= 15:
            break
    return failures


async def run_tests(framework: str, target: str, workspace: str,
                    timeout: float = 300.0,
                    env: Optional[Dict[str, str]] = None,
                    collect_coverage: bool = False) -> TestResult:
    """统一测试运行入口：返回结构化 TestResult，绝不抛异常。"""
    start = time.time()
    framework = (framework or "pytest").lower()
    if framework in ("auto", ""):
        framework = "pytest"  # 无检测时的保守默认；有 pyproject 时由调用方指定
    target = (target or "").strip()
    argv = _build_argv(framework, target, collect_coverage)
    if argv is None:
        return TestResult(success=False, framework=framework,
                          output=f"不支持的测试框架: {framework}",
                          duration_ms=(time.time() - start) * 1000)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            env={**__import__("os").environ, **(env or {}),
                 "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return TestResult(success=False, framework=framework,
                              output=f"测试超时（{timeout:.0f}s）并被终止",
                              duration_ms=(time.time() - start) * 1000)
    except FileNotFoundError as e:
        return TestResult(success=False, framework=framework,
                          output=f"找不到测试命令: {e}",
                          duration_ms=(time.time() - start) * 1000)
    except Exception as e:
        return TestResult(success=False, framework=framework,
                          output=f"启动测试失败: {e}",
                          duration_ms=(time.time() - start) * 1000)

    stdout = raw[0].decode("utf-8", errors="replace") if raw else ""
    success = proc.returncode == 0
    failures = parse_test_output(framework, stdout)
    coverage = _extract_coverage(framework, stdout)
    return TestResult(success=success, output=stdout, framework=framework,
                      duration_ms=(time.time() - start) * 1000,
                      failures=failures, coverage=coverage)


def _build_argv(framework: str, target: str,
                collect_coverage: bool) -> Optional[List[str]]:
    t = target if target else "."
    if framework == "pytest":
        argv = [sys.executable, "-m", "pytest", t, "-q", "--tb=short",
                "-p", "no:cacheprovider"]
        if collect_coverage:
            argv += ["--cov", "--cov-report=term-missing"]
        return argv
    if framework == "unittest":
        return [sys.executable, "-m", "unittest", "discover",
                "-s", t if target else "."]
    if framework == "jest":
        argv = ["npx", "jest", t, "--ci", "--silent"] if target else \
               ["npx", "jest", "--ci", "--silent"]
        return argv
    if framework == "go":
        return ["go", "test", t]
    if framework == "npm":
        return ["npm", "test"]
    return None


def _extract_coverage(framework: str, output: str) -> Optional[float]:
    """从 pytest-cov 输出提取总覆盖率（如 87%）。"""
    if framework != "pytest":
        return None
    m = re.search(r"TOTAL\s+[\d]+\s+[\d]+\s+(\d+)%", output)
    if m:
        return float(m.group(1))
    m2 = re.search(r"Coverage:\s*([\d.]+)%", output)
    return float(m2.group(1)) if m2 else None
