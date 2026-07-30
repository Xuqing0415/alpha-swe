"""第五关：Context Auto-Compression（智能截断）
当 Token 预估超过阈值 80% 时触发紧急压缩：
1. 保留最新 3 轮完整对话
2. 将之前的观察结果进行摘要浓缩
3. 输出中包含占位符标记，Parser 可识别
"""
import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger("alpha-swe.compressor")


class ContextCompressor:
    """上下文智能压缩器"""

    def __init__(self, threshold: float = 0.8, max_token_limit: int = 100000,
                 keep_recent: int = 3, llm_call=None):
        self.threshold = threshold
        self.max_token_limit = max_token_limit
        self.keep_recent = keep_recent  # 保留最近 N 轮
        self.llm_call = llm_call  # 压缩用的 LLM
        self.compression_count = 0
        self.compression_history: List[dict] = []

    def should_compress(self, estimated_tokens: int) -> bool:
        """判断是否需要压缩"""
        return estimated_tokens > self.max_token_limit * self.threshold

    def compress(self, history: List[dict]) -> str:
        """压缩历史对话"""
        if len(history) <= self.keep_recent:
            return ""

        self.compression_count += 1
        logger.info(f"第 {self.compression_count} 次压缩: 共 {len(history)} 轮")

        # 分离近期和远期
        recent = history[-self.keep_recent:]
        old = history[:-self.keep_recent]

        # 摘要浓缩
        if self.llm_call:
            summary = self._llm_compress(old)
        else:
            summary = self._simple_compress(old)

        # 记录压缩
        self.compression_history.append({
            "count": self.compression_count,
            "old_rounds": len(old),
            "recent_rounds": len(recent),
            "summary_length": len(summary)
        })

        return summary

    def _simple_compress(self, old_history: List[dict]) -> str:
        """简单压缩（不调用 LLM）"""
        lines = ["[COMPRESSED_SUMMARY] 以下为历史轮次摘要:"]

        actions = []
        errors = []
        files = []

        for h in old_history:
            step = h.get("step", "")
            action = h.get("action", "")
            result = h.get("result", "")

            actions.append(f"- {action}: {step}")

            if "error" in str(result).lower() or "失败" in str(result):
                errors.append(f"- {step}: {result[:100]}")

            # 提取文件路径
            import re
            found = re.findall(r'([\w/.-]+\.\w{1,5})', str(result))
            files.extend(found[:3])

        parts = []
        parts.append(f"执行了 {len(old_history)} 个步骤:")
        parts.extend(actions[-10:])  # 最近 10 个动作

        if errors:
            parts.append(f"\n遇到的错误 ({len(errors)}):")
            parts.extend(errors[-5:])

        if files:
            parts.append(f"\n涉及的文件: {', '.join(set(files[-10:]))}")

        return "\n".join(parts)

    def _llm_compress(self, old_history: List[dict]) -> str:
        """使用 LLM 进行摘要压缩"""
        prompt = f"""请将以下 Agent 执行历史压缩为简洁摘要。保留：
1. 关键操作和结果
2. 遇到的错误及解决方式
3. 涉及的重要文件

历史记录:
{json.dumps(old_history, ensure_ascii=False, indent=2)[:3000]}

请输出压缩摘要（不超过 500 字）:"""

        try:
            return self.llm_call(prompt)
        except Exception:
            return self._simple_compress(old_history)

    def get_watermark(self, current_tokens: int) -> dict:
        """获取当前水位线状态"""
        percentage = current_tokens / self.max_token_limit * 100
        return {
            "current_tokens": current_tokens,
            "max_tokens": self.max_token_limit,
            "percentage": f"{percentage:.1f}%",
            "threshold": f"{self.threshold * 100:.0f}%",
            "need_compress": self.should_compress(current_tokens),
            "compression_count": self.compression_count
        }