"""第二关增强：Critic Agent——执行后验证，避免盲目继续
在每个步骤执行后，Critic 验证输出是否符合预期，给出通过/修正/回退 建议。
"""
import json
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("alpha-swe.critic")

CRITIC_SYSTEM_PROMPT = """你是一个代码审查员（Critic Agent）。你的职责是：
1. 验证 Executor 的输出是否符合预期
2. 检查是否有潜在错误或遗漏
3. 给出 verdict: pass/retry/revert

输出格式:
{
  "verdict": "pass|retry|revert",
  "confidence": 0.0-1.0,
  "reason": "判断理由",
  "suggestion": "修正建议（仅 retry/revert 时）"
}"""


@dataclass
class CriticVerdict:
    """评审结果"""
    verdict: str  # pass / retry / revert
    confidence: float = 1.0
    reason: str = ""
    suggestion: str = ""


class CriticAgent:
    """评审员 Agent——验证执行结果"""

    def __init__(self, llm_call=None):
        self.llm_call = llm_call
        self.review_history: list = []

    def review(self, task: dict, result: dict) -> CriticVerdict:
        """评审单步执行结果"""
        # 1. 快速规则检查
        quick = self._quick_check(task, result)
        if quick:
            verdict = quick
        # 2. LLM 深度检查（如有）
        elif self.llm_call:
            verdict = self._llm_review(task, result)
        # 3. 默认通过
        elif result.get("success"):
            verdict = CriticVerdict(verdict="pass", confidence=0.9, reason="执行成功")
        else:
            verdict = CriticVerdict(
                verdict="retry",
                confidence=0.7,
                reason=f"执行失败: {result.get('error', '未知错误')}",
                suggestion="建议修正参数后重试"
            )

        # 记录评审历史
        self.review_history.append({
            "task": task,
            "result": result,
            "verdict": verdict.verdict,
            "confidence": verdict.confidence
        })
        return verdict

    def _quick_check(self, task: dict, result: dict) -> Optional[CriticVerdict]:
        """快速规则检查（零 token 开销）"""
        error = str(result.get("error", "")).lower()
        output = str(result.get("output", "")).lower()

        # 空输出检查
        if not output.strip() and not error:
            return CriticVerdict(
                verdict="retry",
                confidence=0.8,
                reason="空输出，可能工具未正确执行",
                suggestion="检查命令参数是否正确"
            )

        # 权限拒绝
        if "permission denied" in error or "sandbox" in error:
            return CriticVerdict(
                verdict="revert",
                confidence=0.95,
                reason=f"权限被拒绝: {error}",
                suggestion="改用安全路径或替代命令"
            )

        # 文件不存在
        if "no such file" in error or "文件不存在" in error:
            return CriticVerdict(
                verdict="retry",
                confidence=0.9,
                reason=f"目标文件不存在: {error}",
                suggestion="先搜索文件位置，再读取"
            )

        # 命令不存在
        if "not recognized" in error or "command not found" in error:
            return CriticVerdict(
                verdict="retry",
                confidence=0.85,
                reason=f"命令不可用: {error}",
                suggestion="使用跨平台兼容命令或检查环境"
            )

        # 执行超时
        if "timeout" in error or "timed out" in error or "超时" in error:
            return CriticVerdict(
                verdict="retry",
                confidence=0.8,
                reason=f"执行超时: {error}",
                suggestion="增加超时时间或改用更快的命令"
            )

        return None

    def _llm_review(self, task: dict, result: dict) -> CriticVerdict:
        """LLM 深度评审"""
        prompt = f"""评审以下任务执行结果:

任务: {json.dumps(task, ensure_ascii=False)}
结果: {json.dumps(result, ensure_ascii=False)}

请输出 verdict JSON:"""

        try:
            resp = self.llm_call(prompt)
            data = json.loads(resp)
            verdict = CriticVerdict(
                verdict=data.get("verdict", "pass"),
                confidence=data.get("confidence", 0.5),
                reason=data.get("reason", ""),
                suggestion=data.get("suggestion", "")
            )
        except Exception:
            verdict = CriticVerdict(
                verdict="pass" if result.get("success") else "retry",
                confidence=0.5,
                reason="LLM 评审失败，使用默认判断"
            )

        return verdict

    def should_continue(self, task: dict, result: dict, max_retries: int = 3) -> tuple:
        """决定是否继续执行、重试或回退"""
        verdict = self.review(task, result)

        if verdict.verdict == "pass":
            return ("continue", verdict)
        elif verdict.verdict == "retry" and result.get("retry_count", 0) < max_retries:
            return ("retry", verdict)
        else:
            return ("revert", verdict)

    def get_stats(self) -> dict:
        total = len(self.review_history)
        passes = sum(1 for r in self.review_history if r["verdict"] == "pass")
        return {
            "total_reviews": total,
            "passes": passes,
            "failures": total - passes,
            "pass_rate": f"{passes / total * 100:.1f}%" if total > 0 else "0%"
        }