"""主执行循环——集成所有七层模块"""
import json
import logging
import time
from typing import List, Optional, Dict
from dataclasses import dataclass, field

from scheduler import TaskScheduler, TaskStep
from prompter import Prompter
from parser import Parser, ParsedAction
from executor import Executor
from memory_bank import MemoryBank
from background_task import BackgroundTaskManager
from plugin_loader import PluginLoader
from compressor import ContextCompressor
from sandbox import Sandbox
from mcp_config import MCPConfigLoader

logger = logging.getLogger("alpha-swe.loop")


@dataclass
class LoopState:
    """循环状态"""
    step_index: int = 0
    total_steps: int = 0
    round_count: int = 0
    user_prompt: str = ""
    history: List[dict] = field(default_factory=list)
    pending_tasks: Dict[str, dict] = field(default_factory=dict)  # 第三关：后台任务
    status: str = "idle"  # idle/thinking/executing/parsing/done


class Loop:
    """Alpha-SWE 主循环——七层进化集成"""

    MAX_ROUNDS = 30
    TOKEN_THRESHOLD = 0.8  # 第五关：80% 水位线

    def __init__(self, config_path: str = "config.yaml", skills_dir: str = "./skills"):
        # 第七关：MCP 配置
        self.mcp_loader = MCPConfigLoader(config_path)
        self.mcp_config = self.mcp_loader.load()

        # 第六关：沙箱
        self.sandbox = Sandbox(
            workspace="/tmp/workspace",
            allowed_paths=self.mcp_config.get("sandbox", {}).get("allowed_paths", []),
            blocked_paths=self.mcp_config.get("sandbox", {}).get("blocked_paths", [
                "/etc", "/sys", "/proc", "/boot", "/root", "C:\\Windows", "C:\\System32"
            ]),
            block_commands=self.mcp_config.get("sandbox", {}).get("block_commands", [
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

        # 状态
        self.state = LoopState()

        # 第二关：Multi-Agent（延迟初始化）
        self.planner_agent = None
        self.executor_agent = None
        self.task_queue = []  # Planner 与 Executor 之间的通信队列

    def run(self, user_prompt: str) -> str:
        """主入口：执行完整流程"""
        self.state = LoopState(user_prompt=user_prompt)
        self.state.status = "thinking"

        logger.info(f"=== Alpha-SWE 开始执行 ===")
        logger.info(f"用户指令: {user_prompt}")

        # 第四关：加载技能
        skill_context = self.plugin_loader.load_for_context(user_prompt)
        if skill_context:
            self.prompter.set_skill(skill_context)
            logger.info(f"已加载技能模块: {self.plugin_loader.loaded_skills}")

        # 第一步：任务拆解
        steps = self.scheduler.plan(user_prompt)
        self.state.total_steps = len(steps)
        logger.info(f"任务拆解为 {len(steps)} 步: {[s.description for s in steps]}")

        # 第二步：循环执行
        final_answer = ""
        for i, step in enumerate(steps):
            self.state.step_index = i
            self.state.round_count += 1

            if self.state.round_count > self.MAX_ROUNDS:
                logger.warning(f"达到最大轮次 {self.MAX_ROUNDS}，强制终止")
                break

            self.state.status = "executing"
            logger.info(f"[Step {step.step_id}] {step.description}")

            # 第一关：注入记忆
            memory_context = self.memory.get_context(step.description)
            if memory_context:
                self.prompter.set_memory(memory_context)

            # 第五关：检查 token 水位
            self._check_and_compress()

            # 构建 Prompt 并执行
            result = self._execute_step(step, user_prompt)

            # 第一关：存储关键实体到记忆
            self._store_to_memory(step, result)

            # 记录历史
            self.state.history.append({
                "step": step.description,
                "action": step.action,
                "result": str(result)[:500]
            })

            if result and result.success:
                step.status = "done"
                step.result = str(result.output)[:1000]
            else:
                step.status = "failed"
                step.error = result.error if result else "无结果"

            # 检查是否是最终答案
            if isinstance(result, dict) and result.get("final_answer"):
                final_answer = result["final_answer"]
                break

        self.state.status = "done"
        logger.info(f"=== Alpha-SWE 执行完成，共 {self.state.round_count} 轮 ===")

        if not final_answer:
            final_answer = self._build_summary(steps)

        # 第一关：持久化记忆
        self.memory.persist()

        return final_answer

    def _execute_step(self, step: TaskStep, user_prompt: str) -> any:
        """执行单个步骤"""
        # 构建 Prompt
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

            # 第三关：检查是否是后台任务
            if self._is_long_running(parsed.tool_name, parsed.params):
                task_id = self.bg_manager.submit(
                    lambda: self.executor.execute(parsed.tool_name, parsed.params),
                    task_name=f"{parsed.tool_name}_{step.step_id}"
                )
                self.state.pending_tasks[task_id] = {
                    "step": step.step_id,
                    "tool": parsed.tool_name
                }
                logger.info(f"[Background] Task {task_id} is running...")
                # 轮询等待
                result = self.bg_manager.wait(task_id, poll_interval=3)
                if result:
                    logger.info(f"[Background] Task {task_id} completed")
                    return result
                return None

            return self.executor.execute(parsed.tool_name, parsed.params)

        elif parsed.action_type == "final_answer":
            return {"final_answer": parsed.content}

        elif parsed.action_type == "think":
            logger.info(f"Agent 思考: {parsed.content[:200]}")
            return None

        return None

    def _is_long_running(self, tool_name: str, params: dict) -> bool:
        """判断是否是需要后台执行的耗时操作"""
        if tool_name == "terminal_execute":
            cmd = params.get("command", "")
            long_keywords = ["pip install", "npm install", "apt-get", "brew install",
                             "git clone", "docker build", "make", "cmake", "yarn", "cargo build"]
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
                compressed = self.compressor.compress(self.state.history)
                self.prompter.set_compressed(compressed)
                logger.info(f"压缩完成: 压缩后 Token ≈ {self.prompter.estimate_tokens(compressed)}")

    def _store_to_memory(self, step: TaskStep, result: any):
        """第一关：将关键信息存入记忆"""
        if result and result.success:
            # 提取文件路径
            import re
            paths = re.findall(r'(?:\./|/)([\w/.-]+\.\w+)', str(result.output))
            for p in paths[:5]:  # 最多存储 5 个
                self.memory.add_entity("file", p, {"step": step.description})
            # 提取类名
            classes = re.findall(r'class\s+(\w+)', str(result.output))
            for c in classes[:3]:
                self.memory.add_entity("class", c, {"step": step.description})

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
        # 根据步骤类型生成模拟响应
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
        """第二关：Multi-Agent 模式"""
        from planner_agent import PlannerAgent
        from executor_agent import ExecutorAgent

        self.planner_agent = PlannerAgent()
        self.executor_agent = ExecutorAgent(executor=self.executor)

        # Planner 拆解
        plan = self.planner_agent.plan(user_prompt)
        self.task_queue = plan.copy()

        results = []
        while self.task_queue:
            task = self.task_queue.pop(0)
            logger.info(f"[Multi-Agent] Planner 任务: {task}")

            result = self.executor_agent.execute(task)
            results.append(result)

            # Planner 根据结果决定是否纠正
            if not result.get("success"):
                correction = self.planner_agent.correct(task, result)
                if correction:
                    logger.info(f"[Multi-Agent] Planner 纠正: {correction}")
                    self.task_queue.insert(0, correction)

        return "\n".join(str(r) for r in results)