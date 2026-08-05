"""主执行循环——集成所有七层模块 + EventBus 事件发布 + 错误恢复 + Critic 验证"""
import json
import logging
import re
from typing import List, Dict, Any
from dataclasses import dataclass, field

from scheduler import TaskScheduler, TaskStep
from prompter import Prompter
from parser import Parser
from executor import Executor
from memory_bank import MemoryBank
from background_task import BackgroundTaskManager
from plugin_loader import PluginLoader
from compressor import ContextCompressor
from sandbox import Sandbox
from mcp_config import MCPConfigLoader
from event_bus import publish_event
from recovery import ErrorRecovery, RetryConfig
from critic_agent import CriticAgent
from structured_log import new_trace
from tools.base import ToolResult

logger = logging.getLogger("alpha-swe.loop")


@dataclass
class LoopState:
    """循环状态"""
    step_index: int = 0
    total_steps: int = 0
    completed_steps: int = 0
    round_count: int = 0
    user_prompt: str = ""
    history: List[dict] = field(default_factory=list)
    pending_tasks: Dict[str, dict] = field(default_factory=dict)
    status: str = "idle"


class Loop:
    """Alpha-SWE 主循环——七层进化集成"""

    MAX_ROUNDS = 30
    TOKEN_THRESHOLD = 0.8

    def __init__(self, config_path: str = "config.yaml", skills_dir: str = "./skills"):
        # 第七关：MCP 配置
        self.mcp_loader = MCPConfigLoader(config_path)
        self.mcp_config = self.mcp_loader.load()

        # 第六关：沙箱（工作目录从配置读取，默认回退到测试工作区）
        sandbox_config = self.mcp_config.get("sandbox", {})
        self.sandbox = Sandbox(
            workspace=sandbox_config.get("workspace", "./test_workspace"),
            allowed_paths=sandbox_config.get("allowed_paths", []),
            blocked_paths=sandbox_config.get("blocked_paths", [
                "/etc", "/sys", "/proc", "/boot", "/root", "C:\\Windows", "C:\\System32"
            ]),
            block_commands=sandbox_config.get("block_commands", [
                "sudo", "rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:"
            ])
        )

        # 核心组件
        self.executor = Executor(sandbox=self.sandbox, mcp_config=self.mcp_config)
        self.prompter = Prompter(tools=self.executor.get_tools())
        self.parser = Parser()
        self.scheduler = TaskScheduler()

        # 第一关：记忆库
        memory_config = self.mcp_config.get("memory", {})
        self.memory = MemoryBank(
            db_path=memory_config.get("db_path", "memory.db"),
            max_entities=memory_config.get("max_entities", 1000)
        )

        # 第三关：后台任务管理器
        self.bg_manager = BackgroundTaskManager()

        # 第四关：插件加载器
        self.plugin_loader = PluginLoader(skills_dir=skills_dir)

        # 第五关：上下文压缩器
        self.compressor = ContextCompressor(
            threshold=self.TOKEN_THRESHOLD,
            max_token_limit=100000
        )

        # 错误恢复
        recovery_config = self.mcp_config.get("recovery", {})
        self.recovery = ErrorRecovery(
            config=RetryConfig(
                max_retries=recovery_config.get("max_retries", 3),
                delay=recovery_config.get("delay", 1.0),
                backoff=recovery_config.get("backoff", 2.0),
                max_delay=recovery_config.get("max_delay", 30.0)
            )
        )

        # Critic Agent
        self.critic = CriticAgent()

        # 状态
        self.state = LoopState()

        # 第二关：Multi-Agent（延迟初始化）
        self.planner_agent = None
        self.executor_agent = None
        self.task_queue = []

    def run(self, user_prompt: str) -> str:
        """主入口：执行完整流程"""
        # 生成 trace_id 用于链路追踪
        trace_id = new_trace()
        self.state = LoopState(user_prompt=user_prompt)
        self.state.status = "thinking"

        logger.info(f"=== Alpha-SWE 开始执行 [trace={trace_id}] ===")
        logger.info(f"用户指令: {user_prompt}")

        publish_event("run_start", trace_id=trace_id, prompt=user_prompt)

        # 第四关：加载技能
        skill_context = self.plugin_loader.load_for_context(user_prompt)
        if skill_context:
            self.prompter.set_skill(skill_context)
            logger.info(f"已加载技能模块: {self.plugin_loader.loaded_skills}")
            publish_event("skill_loaded", skills=self.plugin_loader.list_skills())

        # 第一步：任务拆解
        steps = self.scheduler.plan(user_prompt)
        self.state.total_steps = len(steps)
        logger.info(f"任务拆解为 {len(steps)} 步: {[s.description for s in steps]}")
        publish_event("plan_created", total_steps=len(steps),
                      steps=[s.description for s in steps])

        # 第二步：循环执行
        final_answer = ""
        for i, step in enumerate(steps):
            self.state.step_index = i
            self.state.round_count += 1

            if self.state.round_count > self.MAX_ROUNDS:
                logger.warning(f"达到最大轮次 {self.MAX_ROUNDS}，强制终止")
                publish_event("max_rounds_reached", rounds=self.state.round_count)
                break

            self.state.status = "executing"
            logger.info(f"[Step {step.step_id}] {step.description}")
            publish_event("step_start", step_id=step.step_id,
                          step_index=i + 1, total_steps=len(steps),
                          description=step.description)

            # 第一关：注入记忆
            memory_context = self.memory.get_context(step.description)
            if memory_context:
                self.prompter.set_memory(memory_context)

            # 第五关：检查 token 水位
            self._check_and_compress()

            # 执行步骤（带错误恢复 + Critic 验证）
            result = self._execute_step_with_recovery(step, user_prompt)

            # 第一关：存储关键实体到记忆（使用 Parser 提取）
            self._store_to_memory(step, result)

            # 记录历史（成功时保存输出文本，便于报告回填与压缩摘要）
            if result and getattr(result, "success", False):
                self.state.completed_steps += 1
                result_text = str(getattr(result, "output", ""))[:500]
            else:
                result_text = str(result)[:500] if result else ""
            self.state.history.append({
                "step": step.description,
                "action": step.action,
                "result": result_text
            })

            if result and getattr(result, "success", False):
                step.status = "done"
                step.result = str(getattr(result, "output", ""))[:1000]
                publish_event("step_done", step_id=step.step_id,
                              description=step.description)
            else:
                step.status = "failed"
                step.error = result.error if result else "无结果"
                publish_event("step_failed", step_id=step.step_id,
                              description=step.description,
                              error=step.error)

            # 检查是否是最终答案
            if isinstance(result, dict) and result.get("final_answer"):
                final_answer = result["final_answer"]
                break

        self.state.status = "done"
        logger.info(f"=== Alpha-SWE 执行完成，共 {self.state.round_count} 轮 ===")
        publish_event("run_done", rounds=self.state.round_count,
                      memory_entities=self.memory.get_stats().get("total_entities", 0),
                      sandbox_violations=self.sandbox.violation_count,
                      compression_count=self.compressor.compression_count,
                      retry_count=self.recovery.retry_count,
                      fallback_count=self.recovery.fallback_count,
                      critic_stats=self.critic.get_stats())

        if not final_answer:
            final_answer = self._build_summary(steps)

        # 第一关：持久化记忆
        self.memory.persist()

        return final_answer

    def _execute_step_with_recovery(self, step: TaskStep, user_prompt: str) -> Any:
        """带错误恢复和 Critic 验证的步骤执行"""
        retry_count = 0
        max_critic_retries = 3
        max_total_attempts = 6  # 限制 Critic 重试 x Recovery 重试的叠加放大

        attempts = 0
        while retry_count <= max_critic_retries and attempts < max_total_attempts:
            attempts += 1
            # 尝试执行
            try:
                result = self._execute_step(step, user_prompt)
            except Exception as e:
                logger.error(f"步骤执行异常: {e}", exc_info=True)
                publish_event("error", step_id=step.step_id, error=str(e),
                              error_type="exception")
                result = ToolResult(success=False, output="", error=str(e))

            # Critic 验证
            task_info = {
                "step_id": step.step_id,
                "description": step.description,
                "action": step.action,
                "params": step.params
            }
            result_info = {
                "success": getattr(result, "success", False),
                "output": str(getattr(result, "output", ""))[:500] if result else "",
                "error": str(getattr(result, "error", "")) if result else "",
                "retry_count": retry_count
            }

            verdict = self.critic.review(task_info, result_info)
            logger.info(f"[Critic] Step {step.step_id}: {verdict.verdict} "
                        f"(confidence={verdict.confidence:.2f}) - {verdict.reason}")

            if verdict.verdict == "pass":
                return result

            if verdict.verdict == "retry":
                retry_count += 1
                if retry_count <= max_critic_retries:
                    logger.info(f"[Critic] 重试 {retry_count}/{max_critic_retries}: "
                                f"{verdict.suggestion}")
                    publish_event("step_retry", step_id=step.step_id,
                                  retry=retry_count, suggestion=verdict.suggestion)

                    # 尝试 fallback 策略
                    if result and not result.success:
                        fallback = self.recovery.apply_fallback(
                            step.action, step.params,
                            result.error if result else ""
                        )
                        if fallback:
                            logger.info(f"应用 fallback: {fallback}")
                            step.action = fallback.get("action", step.action)
                            step.params = fallback.get("params", step.params)
                    continue

            if verdict.verdict == "revert":
                logger.warning(f"[Critic] 回退: {verdict.reason}")
                publish_event("step_revert", step_id=step.step_id,
                              reason=verdict.reason)
                return ToolResult(success=False, output="",
                                   error=f"Critic 回退: {verdict.reason}")

        return result

    def _execute_step(self, step: TaskStep, user_prompt: str) -> Any:
        """执行单个步骤"""
        prompt = self.prompter.build(
            user_prompt=user_prompt,
            history=self.state.history,
            current_step={
                "step_id": step.step_id,
                "description": step.description,
                "params": step.params
            }
        )
        self.state.status = "thinking"

        # 模拟 LLM 调用（实际项目中替换为真实 API）
        response = self._llm_simulate(step, prompt)

        self.state.status = "parsing"
        parsed = self.parser.parse(response)

        if parsed.action_type == "tool_call":
            self.state.status = "executing"
            publish_event("tool_call", tool=parsed.tool_name,
                          params=parsed.params, step_id=step.step_id)

            # 报告占位符回填：把之前成功步骤的输出写入 report
            if parsed.tool_name == "file_ops" and parsed.params.get("action") == "write":
                content = parsed.params.get("content", "")
                if "{{search_results}}" in content:
                    findings = "\n".join(
                        str(h.get("result", "")).strip()
                        for h in self.state.history
                        if h.get("result") and "error" not in str(h.get("result")).lower()
                    )
                    parsed.params["content"] = content.replace(
                        "{{search_results}}", findings or "（未找到结果）"
                    )

            # 第三关：检查是否是后台任务
            if self._is_long_running(parsed.tool_name, parsed.params):
                task_id = self.bg_manager.submit(
                    lambda: self._safe_execute(parsed.tool_name, parsed.params),
                    task_name=f"{parsed.tool_name}_{step.step_id}"
                )
                self.state.pending_tasks[task_id] = {
                    "step": step.step_id,
                    "tool": parsed.tool_name
                }
                logger.info(f"[Background] Task {task_id} is running...")
                publish_event("background_task", task_id=task_id,
                              tool=parsed.tool_name)

                result = self.bg_manager.wait(task_id, poll_interval=3, timeout=300)
                if result:
                    logger.info(f"[Background] Task {task_id} completed")
                    publish_event("background_task_done", task_id=task_id)
                    return result
                return None

            # 带重试的工具执行
            return self._safe_execute(parsed.tool_name, parsed.params)

        elif parsed.action_type == "final_answer":
            return {"final_answer": parsed.content}

        elif parsed.action_type == "think":
            logger.info(f"Agent 思考: {parsed.content[:200]}")
            publish_event("think", content=parsed.content[:200])
            return None

        return None

    def _safe_execute(self, tool_name: str, params: dict) -> Any:
        """带重试和 fallback 的安全执行"""
        try:
            return self.recovery.execute_with_retry(
                self.executor.execute, tool_name, params
            )
        except Exception:
            # 已耗尽重试，尝试 fallback
            fallback = self.recovery.apply_fallback(tool_name, params, "重试耗尽")
            if fallback:
                logger.info(f"重试耗尽后应用 fallback: {fallback}")
                try:
                    return self.executor.execute(
                        fallback["action"], fallback["params"]
                    )
                except Exception as e2:
                    logger.error(f"Fallback 也失败: {e2}")
                    return ToolResult(success=False, output="",
                                       error=f"Fallback 失败: {e2}")
            return ToolResult(success=False, output="",
                               error=f"工具执行失败: {tool_name}")

    def _is_long_running(self, tool_name: str, params: dict) -> bool:
        """判断是否是需要后台执行的耗时操作"""
        if tool_name == "terminal_execute":
            cmd = params.get("command", "")
            long_keywords = ["pip install", "npm install", "apt-get", "brew install",
                             "git clone", "docker build", "make", "cmake", "yarn",
                             "cargo build"]
            return any(kw in cmd.lower() for kw in long_keywords)
        return False

    def _check_and_compress(self):
        """第五关：检查 token 水位并触发压缩"""
        if self.state.round_count > 5:
            context_text = json.dumps(self.state.history, ensure_ascii=False)
            estimated_tokens = self.prompter.estimate_tokens(context_text)
            limit = self.compressor.max_token_limit

            if estimated_tokens > limit * self.TOKEN_THRESHOLD:
                logger.warning(
                    f"Token 水位告警: {estimated_tokens}/{limit} "
                    f"({estimated_tokens / limit * 100:.1f}%)，触发紧急压缩"
                )
                publish_event("compress", before=estimated_tokens,
                              threshold=limit * self.TOKEN_THRESHOLD,
                              count=self.compressor.compression_count)
                compressed = self.compressor.compress(self.state.history)
                self.prompter.set_compressed(compressed)
                after = self.prompter.estimate_tokens(compressed)
                logger.info(f"压缩完成: 压缩后 Token ≈ {after}")
                publish_event("compress_done", after=after,
                              before=estimated_tokens)

    def _store_to_memory(self, step: TaskStep, result: Any):
        """第一关：将关键信息存入记忆（使用 Parser 实体提取）"""
        if result and getattr(result, "success", False):
            # 使用 Parser 的轻量级规则提取实体（零 LLM 调用）
            entities = self.parser.extract_entities(str(getattr(result, "output", "")))
            for e in entities:
                self.memory.add_entity(
                    e["type"], e["name"],
                    {"step": step.description, "source": "parser"}
                )

            # 补充：从执行结果中提取文件路径（如果 Parser 漏了）
            if not any(e["type"] == "file" for e in entities):
                paths = re.findall(r'(?:\./|/|\\\\)([\w/.-]+\.\w+)', str(result.output))
                for p in paths[:5]:
                    self.memory.add_entity("file", p, {"step": step.description})

    def _build_summary(self, steps: List[TaskStep]) -> str:
        """构建执行摘要"""
        lines = ["## Alpha-SWE 执行摘要\n"]
        for s in steps:
            status = "✓" if s.status == "done" else "✗"
            lines.append(f"- {status} Step {s.step_id}: {s.description}")
            if s.result:
                lines.append(f"  结果: {s.result[:200]}")
            if s.error:
                lines.append(f"  错误: {s.error[:200]}")
        return "\n".join(lines)

    def _llm_simulate(self, step: TaskStep, prompt: str) -> str:
        """模拟 LLM 响应（实际项目中替换为 API 调用）"""
        if step.action == "terminal_execute":
            cmd = step.params.get("command", "echo ok")
            return json.dumps({
                "tool": "terminal_execute",
                "params": {"command": cmd}
            }, ensure_ascii=False)
        elif step.action == "file_ops":
            return json.dumps({
                "tool": "file_ops",
                "params": step.params
            }, ensure_ascii=False)
        elif step.action == "think":
            return json.dumps({
                "think": f"正在分析: {step.description}"
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "tool": step.action,
                "params": step.params
            }, ensure_ascii=False)

    def run_with_multi_agent(self, user_prompt: str) -> str:
        """第二关：Multi-Agent 模式（含 Critic 验证 + 错误恢复）"""
        from planner_agent import PlannerAgent
        from executor_agent import ExecutorAgent

        trace_id = new_trace()
        logger.info(f"=== Multi-Agent 模式 [trace={trace_id}] ===")
        publish_event("run_start", trace_id=trace_id, prompt=user_prompt,
                      mode="multi_agent")

        self.planner_agent = PlannerAgent()
        self.executor_agent = ExecutorAgent(executor=self.executor)

        # Planner 拆解
        plan = self.planner_agent.plan(user_prompt)
        self.task_queue = plan.copy()
        publish_event("plan_created", total_steps=len(plan),
                      steps=[t.get("description", "") for t in plan])

        results = []
        while self.task_queue:
            task = self.task_queue.pop(0)
            logger.info(f"[Multi-Agent] Planner 任务: {task}")
            publish_event("step_start", step_id=task.get("step_id", "?"),
                          description=task.get("description", ""))

            # 执行（带重试）
            result = self.recovery.execute_with_retry(
                self.executor_agent.execute, task
            )
            results.append(result)

            # Critic 验证
            critic_result = self.critic.review(task, result)
            logger.info(f"[Critic] {critic_result.verdict}: {critic_result.reason}")

            # Planner 根据结果决定是否纠正
            if not result.get("success") or critic_result.verdict == "retry":
                correction = self.planner_agent.correct(task, result)
                if correction:
                    logger.info(f"[Multi-Agent] Planner 纠正: {correction}")
                    self.task_queue.insert(0, correction)

            if critic_result.verdict == "revert":
                logger.warning(f"[Multi-Agent] 回退: {critic_result.reason}")
                publish_event("step_revert", step_id=task.get("step_id", "?"),
                              reason=critic_result.reason)
                break

        publish_event("run_done", mode="multi_agent",
                      total_tasks=len(results),
                      critic_stats=self.critic.get_stats(),
                      recovery_stats=self.recovery.get_stats())
        return "\n".join(str(r) for r in results)
