"""Alpha-SWE 七层集成测试 + 新增模块（EventBus/ErrorRecovery/CriticAgent/StructuredLog）"""
import sys, os, json, logging, time
sys.path.insert(0, '.')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from memory_bank import MemoryBank
from background_task import BackgroundTaskManager
from plugin_loader import PluginLoader
from compressor import ContextCompressor
from sandbox import Sandbox
from executor import Executor
from mcp_config import MCPConfigLoader
from scheduler import TaskScheduler
from prompter import Prompter
from parser import Parser
from loop import Loop
from event_bus import event_bus, publish_event
from recovery import ErrorRecovery, RetryConfig
from critic_agent import CriticAgent
from structured_log import new_trace, get_trace_id, setup_structured_logging

print('=' * 70)
print('  Alpha-SWE 七层集成测试 + 增强模块')
print('=' * 70)

# ==========================================
# 第一关：MemoryBank
# ==========================================
print()
print('--- 第一关：MemoryBank ---')
mem = MemoryBank(db_path='test_memory.db')
mem.add_entity('file', 'src/app.js', {'step': '搜索文件'})
mem.add_entity('file', 'src/utils.js', {'step': '搜索文件'})
mem.add_entity('class', 'Header', {'step': '解析组件'})
mem.persist()
ctx = mem.get_context('搜索 js')
print(f'记忆上下文: {ctx[:200]}')
compact = mem.compact()
print(f'压缩摘要: {compact[:200]}')
print(f'统计: {mem.get_stats()}')

# ==========================================
# 第二关：Multi-Agent
# ==========================================
print()
print('--- 第二关：Multi-Agent ---')
from planner_agent import PlannerAgent
from executor_agent import ExecutorAgent
sandbox = Sandbox(workspace='./test_workspace')
executor = Executor(sandbox=sandbox)
planner = PlannerAgent()
exec_agent = ExecutorAgent(executor=executor)
plan = planner.plan('找出所有 console.log')
print(f'Planner: {len(plan)} 个任务')
for t in plan:
    r = exec_agent.execute(t)
    print(f'  {t["description"]}: success={r["success"]}')
print(f'统计: {exec_agent.get_stats()}')

# ==========================================
# 第三关：Background Tasks
# ==========================================
print()
print('--- 第三关：Background Tasks ---')
bg = BackgroundTaskManager(max_workers=2)
task_id = bg.submit(lambda: (time.sleep(0.5), 'done'), task_name='long_install')
print(f'[Background] Task {task_id} submitted')
for i in range(3):
    status = bg.get_status(task_id)
    print(f'  轮询 {i+1}: status={status}')
    if status == 'completed':
        break
    time.sleep(0.3)
result = bg.wait(task_id, poll_interval=0.2, timeout=5)
print(f'[Background] Task {task_id} completed: {result}')
bg.shutdown(wait=False)

# ==========================================
# 第四关：PluginLoader
# ==========================================
print()
print('--- 第四关：PluginLoader ---')
loader = PluginLoader(skills_dir='./skills')
print(f'技能: {loader.list_skills()}')
ctx = loader.load_for_context('React 项目')
print(f'React 匹配: {"Yes" if "React" in ctx else "No"}')

# ==========================================
# 第五关：ContextCompressor
# ==========================================
print()
print('--- 第五关：ContextCompressor ---')
compressor = ContextCompressor(max_token_limit=1000, threshold=0.5)
history = [{'step': f'step_{i}', 'action': 'test', 'result': 'x' * 100} for i in range(20)]
estimated = len(json.dumps(history))
print(f'模拟Token: {estimated}, 需压缩: {compressor.should_compress(estimated)}')
compressed = compressor.compress(history)
print(f'压缩后: {len(compressed)} chars')
print(f'水位: {compressor.get_watermark(estimated)}')

# ==========================================
# 第六关：Sandbox
# ==========================================
print()
print('--- 第六关：Sandbox ---')
r = executor.execute('file_ops', {'action': 'write', 'path': '/etc/passwd', 'content': 'hack'})
print(f'写 /etc: blocked={not r.success}')
r = executor.execute('terminal_execute', {'command': 'sudo rm -rf /'})
print(f'sudo: blocked={not r.success}')
r = executor.execute('file_ops', {'action': 'write', 'path': './safe.txt', 'content': 'safe'})
print(f'安全写: success={r.success}')
print(f'违规数: {sandbox.violation_count}')

# ==========================================
# 第七关：MCP 配置
# ==========================================
print()
print('--- 第七关：MCP 配置 ---')
mcp = MCPConfigLoader('config.yaml')
config = mcp.load()
print(f'terminal: enabled={mcp.is_tool_enabled("terminal_execute")}')
print(f'git: enabled={mcp.is_tool_enabled("git")}')
print(f'max_rounds={config["agent"]["max_rounds"]}')

# ==========================================
# 新增：EventBus 事件总线
# ==========================================
print()
print('--- 新增：EventBus ---')
event_bus.clear()
publish_event("test_event", msg="hello", value=42)
publish_event("step_start", step_id="1", description="测试步骤")
publish_event("tool_call", tool="file_ops", params={"path": "test.txt"})
events = []
e = event_bus.consume(timeout=0.05)
while e:
    events.append(e)
    e = event_bus.consume(timeout=0)
print(f'发布 3 个事件，消费 {len(events)} 个')
for ev in events:
    print(f'  [{ev.event_type}] {ev.data}')

# ==========================================
# 新增：ErrorRecovery 错误恢复
# ==========================================
print()
print('--- 新增：ErrorRecovery ---')
recovery = ErrorRecovery(config=RetryConfig(max_retries=2, delay=0.1))

# 测试重试
call_count = [0]
def flaky_func():
    call_count[0] += 1
    if call_count[0] < 3:
        from tools.base import ToolResult
        return ToolResult(success=False, output="", error="临时失败")
    from tools.base import ToolResult
    return ToolResult(success=True, output="第三次成功", error="")

result = recovery.execute_with_retry(flaky_func)
print(f'重试测试: success={result.success}, calls={call_count[0]}, retries={recovery.retry_count}')

# 测试 fallback
fallback = recovery.apply_fallback("terminal_execute",
    {"command": "grep -r 'test' ."}, "grep not found")
print(f'Fallback 测试: {fallback["action"] if fallback else "None"}')

fallback2 = recovery.apply_fallback("file_ops",
    {"path": "/tmp/nonexistent.txt"}, "No such file or directory")
print(f'文件不存在 fallback: {fallback2["action"] if fallback2 else "None"}')

print(f'统计: {recovery.get_stats()}')

# ==========================================
# 新增：CriticAgent 评审验证
# ==========================================
print()
print('--- 新增：CriticAgent ---')
critic = CriticAgent()

# 测试通过
v1 = critic.review({"step_id": "1", "description": "读取文件"},
                   {"success": True, "output": "文件内容: hello world", "error": ""})
print(f'通过: {v1.verdict} (confidence={v1.confidence:.2f})')

# 测试权限拒绝
v2 = critic.review({"step_id": "2", "description": "写系统文件"},
                   {"success": False, "output": "", "error": "permission denied: /etc/passwd"})
print(f'权限拒绝: {v2.verdict} (confidence={v2.confidence:.2f}) - {v2.reason}')

# 测试文件不存在
v3 = critic.review({"step_id": "3", "description": "读取配置"},
                   {"success": False, "output": "", "error": "No such file: config.yaml"})
print(f'文件不存在: {v3.verdict} (confidence={v3.confidence:.2f}) - {v3.suggestion}')

# 测试空输出
v4 = critic.review({"step_id": "4", "description": "执行命令"},
                   {"success": True, "output": "", "error": ""})
print(f'空输出: {v4.verdict} (confidence={v4.confidence:.2f})')

print(f'统计: {critic.get_stats()}')

# ==========================================
# 新增：StructuredLog 结构化日志
# ==========================================
print()
print('--- 新增：StructuredLog ---')
log_file = setup_structured_logging(log_dir='./logs', console=False)
trace_id = new_trace()
print(f'Trace ID: {trace_id}')
print(f'日志文件: {log_file}')
# 写一条测试日志
test_logger = logging.getLogger("alpha-swe.test")
test_logger.info("结构化日志测试消息")
test_logger.warning("这是一条警告")
print(f'JSONL 日志已写入 {log_file}')

# ==========================================
# 全流程集成（含新模块）
# ==========================================
print()
print('--- 全流程集成（含 EventBus + ErrorRecovery + CriticAgent） ---')
loop = Loop(config_path='config.yaml', skills_dir='./skills')
result = loop.run('请帮我读取 src/ 下所有 .js 文件，找出所有的 console.log，并生成一个 report.txt，但注意不要读取 node_modules')
print(f'结果: {result[:300]}')

# 打印增强统计
print(f'\n--- 增强统计信息 ---')
print(f'总轮次: {loop.state.round_count}')
print(f'总步骤: {loop.state.total_steps}')
print(f'记忆实体: {loop.memory.get_stats()["total_entities"]}')
print(f'沙箱拦截: {loop.sandbox.violation_count}')
print(f'压缩次数: {loop.compressor.compression_count}')
print(f'重试次数: {loop.recovery.retry_count}')
print(f'Fallback 次数: {loop.recovery.fallback_count}')
print(f'Critic 评审: {loop.critic.get_stats()}')
print(f'事件队列: {event_bus.size()} 条待消费')

# Multi-Agent 模式测试
print()
print('--- Multi-Agent 模式（含 Critic + Recovery） ---')
result_ma = loop.run_with_multi_agent('请列出当前目录的所有文件')
print(f'Multi-Agent 结果: {result_ma[:200]}')

print()
print('=' * 70)
print('  全部七关 + 增强模块测试完成!')
print('=' * 70)

# 清理
import os
for f in ['test_memory.db', 'safe.txt', 'report.txt', 'test_output.txt']:
    if os.path.exists(f):
        os.remove(f)