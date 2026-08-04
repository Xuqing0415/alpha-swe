"""第五关：Context Auto-Compression（层次压缩）
当 Token 预估超过阈值 80% 时触发紧急压缩：
1. 第一层：截断过长的 Observation（保留首尾 200 字符）
2. 第二层：保留最新 3 轮完整对话，历史摘要浓缩
3. 第三层：调用 LLM 做摘要（可选）
4. 输出中包含占位符标记，Parser 可识别
"""
import json
import re
import logging
from typing import List

logger = logging.getLogger("alpha-swe.compressor")


class ContextCompressor:
    """上下文层次压缩器"""

    # 截断保留长度
    TRUNCATE_HEAD = 200
    TRUNCATE_TAIL = 200
    TRUNCATE_MARKER = "\n...[内容已截断]...\n"

    def __init__(self, threshold: float = 0.8, max_token_limit: int = 100000,
                 keep_recent: int = 3, llm_call=None):
        self.threshold = threshold
        self.max_token_limit = max_token_limit
        self.keep_recent = keep_recent
        self.llm_call = llm_call
        self.compression_count = 0
        self.truncation_count = 0
        self.compression_history: List[dict] = []

    def should_compress(self, estimated_tokens: int) -> bool:
        return estimated_tokens > self.max_token_limit * self.threshold

    def compress(self, history: List[dict]) -> str:
        """层次压缩：先截断 -> 再摘要"""
        if len(history) <= self.keep_recent:
            return ""

        self.compression_count += 1
        logger.info(f"第 {self.compression_count} 次压缩: 共 {len(history)} 轮")

        recent = history[-self.keep_recent:]
        old = history[:-self.keep_recent]

        # 第一层：截断长 Observation
        old_truncated = [self._truncate_observation(h) for h in old]

        # 第二层：摘要浓缩
        if self.llm_call:
            summary = self._llm_compress(old_truncated)
        else:
            summary = self._simple_compress(old_truncated)

        self.compression_history.append({
            "count": self.compression_count,
            "old_rounds": len(old),
            "recent_rounds": len(recent),
            "truncations": self.truncation_count,
            "summary_length": len(summary)
        })

        return summary

    def _truncate_observation(self, entry: dict) -> dict:
        """截断过长的 Observation，保留首尾"""
        result = entry.get("result", "")
        if not result or len(result) <= self.TRUNCATE_HEAD + self.TRUNCATE_TAIL + 100:
            return entry

        self.truncation_count += 1
        entry = dict(entry)
        entry["result"] = (
            result[:self.TRUNCATE_HEAD]
            + self.TRUNCATE_MARKER
            + result[-self.TRUNCATE_TAIL:]
        )
        entry["_truncated"] = True
        return entry

    def _simple_compress(self, old_history: List[dict]) -> str:
        """简单压缩（不调用 LLM）"""
        actions = []
        errors = []
        critical_errors = []
        files = []

        for h in old_history:
            step = h.get("step", "")
            action = h.get("action", "")
            result = h.get("result", "")

            actions.append(f"- {action}: {step}")

            if "error" in str(result).lower() or "失败" in str(result):
                err_msg = str(result)[:100]
                errors.append(f"- {step}: {err_msg}")
                # 标记关键错误（保留更多上下文）
                if any(keyword in err_msg.lower() for keyword in
                       ["permission", "sandbox", "timeout", "crash", "panic"]):
                    critical_errors.append(f"- [CRITICAL] {step}: {err_msg}")

            # 提取文件路径
            found = re.findall(r'([\w/.-]+\.\w{1,5})', str(result))
            files.extend(found[:3])

        parts = ["[COMPRESSED_SUMMARY] 以下为历史轮次摘要:", f"执行了 {len(old_history)} 个步骤:"]
        parts.extend(actions[-10:])

        if critical_errors:
            parts.append(f"\n关键错误 ({len(critical_errors)}):")
            parts.extend(critical_errors)
        elif errors:
            parts.append(f"\n遇到的错误 ({len(errors)}):")
            parts.extend(errors[-5:])

        if files:
            parts.append(f"\n涉及的文件: {', '.join(set(files[-10:]))}")

        # 标记压缩摘要
        parts.append("\n[注意: 以上为压缩摘要，信息可能不完整，如有疑问请主动询问]")

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
        percentage = current_tokens / self.max_token_limit * 100
        return {
            "current_tokens": current_tokens,
            "max_tokens": self.max_token_limit,
            "percentage": f"{percentage:.1f}%",
            "threshold": f"{self.threshold * 100:.0f}%",
            "need_compress": self.should_compress(current_tokens),
            "compression_count": self.compression_count,
            "truncation_count": self.truncation_count,
        }