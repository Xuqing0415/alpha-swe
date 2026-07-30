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

print('=' * 70)
print('  Alpha-SWE 七层集成测试')
print('=' * 70)

# 第一关：MemoryBank
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

# 第二关：Multi-Agent
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

# 第三关：Background Tasks
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

# 第四关：PluginLoader
print()
print('--- 第四关：PluginLoader ---')
loader = PluginLoader(skills_dir='./skills')
print(f'技能: {loader.list_skills()}')
ctx = loader.load_for_context('React 项目')
print(f'React 匹配: {"Yes" if "React" in ctx else "No"}')

# 第五关：ContextCompressor
print()
print('--- 第五关：ContextCompressor ---')
compressor = ContextCompressor(max_token_limit=1000, threshold=0.5)
history = [{'step': f'step_{i}', 'action': 'test', 'result': 'x' * 100} for i in range(20)]
estimated = len(json.dumps(history))
print(f'模拟Token: {estimated}, 需压缩: {compressor.should_compress(estimated)}')
compressed = compressor.compress(history)
print(f'压缩后: {len(compressed)} chars')
print(f'水位: {compressor.get_watermark(estimated)}')

# 第六关：Sandbox
print()
print('--- 第六关：Sandbox ---')
r = executor.execute('file_ops', {'action': 'write', 'path': '/etc/passwd', 'content': 'hack'})
print(f'写 /etc: blocked={not r.success}')
r = executor.execute('terminal_execute', {'command': 'sudo rm -rf /'})
print(f'sudo: blocked={not r.success}')
r = executor.execute('file_ops', {'action': 'write', 'path': './safe.txt', 'content': 'safe'})
print(f'安全写: success={r.success}')
print(f'违规数: {sandbox.violation_count}')

# 第七关：MCP
print()
print('--- 第七关：MCP 配置 ---')
mcp = MCPConfigLoader('config.yaml')
config = mcp.load()
print(f'terminal: enabled={mcp.is_tool_enabled("terminal_execute")}')
print(f'git: enabled={mcp.is_tool_enabled("git")}')
print(f'max_rounds={config["agent"]["max_rounds"]}')

# 全流程
print()
print('--- 全流程集成 ---')
loop = Loop(config_path='config.yaml', skills_dir='./skills')
result = loop.run('请帮我读取 src/ 下所有 .js 文件，找出所有的 console.log，并生成一个 report.txt，但注意不要读取 node_modules')
print(f'结果: {result[:300]}')

print()
print('=' * 70)
print('  全部七关测试完成!')
print('=' * 70)

# 清理
import os
for f in ['test_memory.db', 'safe.txt', 'report.txt', 'test_output.txt']:
    if os.path.exists(f):
        os.remove(f)