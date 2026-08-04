"""Alpha-SWE Agent —— 七层进化系统主入口

流程: User Prompt -> Scheduler 拆解 -> Loop 执行 -> Executor 执行工具 -> Parser 提取 -> 返回答案

七层进化:
  1. MemoryBank  - 记忆持久化
  2. Multi-Agent  - Planner/Executor 角色拆分
  3. Background   - 异步非阻塞任务
  4. PluginLoader - 技能热加载
  5. Compressor   - 上下文智能压缩
  6. Sandbox      - 安全围栏
  7. MCP + TUI    - 配置自由 + 可视化仪表盘
"""
import sys
import os
import json
import logging
import argparse
import time
from datetime import datetime

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loop import Loop
from terminal_ui import TerminalUI, HAS_RICH


def setup_logging(log_dir: str = "./logs"):
    """配置日志系统"""
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"alpha_swe_{timestamp}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger("alpha-swe")


def create_test_workspace(base_dir: str = "./test_workspace"):
    """创建测试工作区（用于验收场景）"""
    os.makedirs(base_dir, exist_ok=True)

    # 创建 src 目录和 TypeScript 文件
    src_dir = os.path.join(base_dir, "src")
    os.makedirs(src_dir, exist_ok=True)

    ts_files = {
        "app.ts": """
// Main application
console.log("App started");

function init(): void {
    console.log("Initializing...");
    console.log("App ready");
}

init();
""",
        "utils.ts": """
// Utility functions
console.log("Utils loaded");

function formatDate(date: Date): string {
    const iso = date.toISOString();
    console.log("Formatting date:", iso);
    return iso;
}
""",
        "components/header.ts": """
// Header component
console.log("Header mounted");

function renderHeader(): string {
    console.log("Rendering header");
    return "<header>App</header>";
}
""",
        "components/footer.ts": """
// Footer component
console.log("Footer mounted");

function renderFooter(): string {
    console.log("Rendering footer");
    return "<footer>2024</footer>";
}
"""
    }

    for filepath, content in ts_files.items():
        full_path = os.path.join(src_dir, filepath)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content.strip())

    # 创建 TypeScript 编译配置
    with open(os.path.join(base_dir, "tsconfig.json"), "w", encoding="utf-8") as f:
        json.dump({
            "compilerOptions": {
                "target": "ES2020",
                "module": "commonjs",
                "strict": True,
                "outDir": "dist",
                "rootDir": "src",
                "esModuleInterop": True,
                "skipLibCheck": True,
                "forceConsistentCasingInFileNames": True,
                "noEmitOnError": True
            },
            "include": ["src/**/*.ts"]
        }, f, ensure_ascii=False, indent=2)

    # 创建 node_modules（模拟）
    nm_dir = os.path.join(base_dir, "node_modules")
    os.makedirs(nm_dir, exist_ok=True)
    nm_subdir = os.path.join(nm_dir, "lodash")
    os.makedirs(nm_subdir, exist_ok=True)
    with open(os.path.join(nm_subdir, "index.js"), "w") as f:
        f.write("console.log('lodash loaded');")

    # 创建 README
    with open(os.path.join(base_dir, "README.md"), "w") as f:
        f.write("# Test Project\nThis is a test project for Alpha-SWE (TypeScript).\n\n- Source: src/**/*.ts\n- Build: npm run build (tsc)")

    logger = logging.getLogger("alpha-swe")
    logger.info(f"测试工作区已创建: {base_dir}")
    return base_dir


def main():
    parser = argparse.ArgumentParser(description="Alpha-SWE Agent - 七层进化系统")
    parser.add_argument("--prompt", "-p", type=str, default="",
                        help="用户指令")
    parser.add_argument("--mode", "-m", type=str, default="standard",
                        choices=["standard", "multi_agent", "demo"],
                        help="运行模式: standard/multi_agent/demo")
    parser.add_argument("--config", "-c", type=str, default="config.yaml",
                        help="MCP 配置文件路径")
    parser.add_argument("--no-ui", action="store_true",
                        help="禁用 Terminal UI")
    parser.add_argument("--create-workspace", action="store_true",
                        help="创建测试工作区")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="交互模式")
    args = parser.parse_args()

    logger = setup_logging()
    logger.info("=== Alpha-SWE Agent 启动 ===")

    # 创建测试工作区（仅创建工作区且无其他指令时直接退出）
    if args.create_workspace:
        create_test_workspace()
        if not args.prompt and not args.interactive:
            logger.info("测试工作区已就绪，未提供指令，退出")
            return

    # 初始化 Loop（集成所有七层）
    loop = Loop(config_path=args.config, skills_dir="./skills")

    # 初始化 Terminal UI
    ui = TerminalUI() if not args.no_ui else None
    if ui and HAS_RICH:
        ui.start()

    # 默认验收场景
    if not args.prompt and not args.interactive:
        if args.mode == "demo":
            args.prompt = "请帮我读取 src/ 下所有 .ts 文件，找出所有的 console.log，并生成一个 report.txt，但注意不要读取 node_modules"
        else:
            args.prompt = "请帮我列出当前目录的所有文件，找出 Python 文件并统计行数"

    try:
        # 交互模式
        if args.interactive:
            print("\n" + "=" * 60)
            print("  Alpha-SWE Agent 交互模式")
            print("  输入 'quit' 或 'exit' 退出")
            print("=" * 60 + "\n")

            while True:
                try:
                    user_input = input("\n>>> ").strip()
                    if user_input.lower() in ("quit", "exit", "q"):
                        print("再见!")
                        break
                    if not user_input:
                        continue

                    if ui:
                        ui.update_status("thinking")
                        ui.update(step="0/0", current_action="解析用户指令...")

                    if args.mode == "multi_agent":
                        result = loop.run_with_multi_agent(user_input)
                    else:
                        result = loop.run(user_input)

                    print(f"\n{'=' * 60}")
                    print(f"  Alpha-SWE 回答:")
                    print(f"{'=' * 60}")
                    print(result)
                    print(f"{'=' * 60}\n")

                    if ui:
                        ui.update_status("done")

                except KeyboardInterrupt:
                    print("\n\n操作已取消")
                    break
                except Exception as e:
                    logger.error(f"执行异常: {e}", exc_info=True)
                    print(f"\n错误: {e}")

        else:
            # 单次执行模式
            logger.info(f"用户指令: {args.prompt}")
            if ui:
                ui.update_status("thinking")
                ui.update(step="0/0", current_action="解析用户指令...")

            if args.mode == "multi_agent":
                result = loop.run_with_multi_agent(args.prompt)
            else:
                result = loop.run(args.prompt)

            print(f"\n{'=' * 60}")
            print(f"  Alpha-SWE 回答:")
            print(f"{'=' * 60}")
            print(result)
            print(f"{'=' * 60}")

            # 打印统计信息
            print(f"\n--- 统计信息 ---")
            print(f"总轮次: {loop.state.round_count}")
            print(f"总步骤: {loop.state.total_steps}")
            print(f"完成步骤: {sum(1 for h in loop.state.history if 'error' not in str(h).lower())}")
            mem_stats = loop.memory.get_stats()
            print(f"记忆实体: {mem_stats['total_entities']}")
            print(f"沙箱拦截: {loop.sandbox.violation_count}")
            print(f"压缩次数: {loop.compressor.compression_count}")
            print(f"加载技能: {loop.plugin_loader.list_skills()}")

            if ui:
                ui.update_status("done")
                ui.update(
                    step=f"{loop.state.total_steps}/{loop.state.total_steps}",
                    memory_entities=mem_stats["total_entities"],
                    sandbox_violations=loop.sandbox.violation_count,
                    compression_count=loop.compressor.compression_count,
                )

    finally:
        if ui and HAS_RICH:
            time.sleep(0.5)  # 让用户看到最终状态
            ui.stop()

        # 持久化记忆（Loop 初始化失败时不执行）
        if 'loop' in locals():
            loop.memory.persist()
            loop.memory.close()
        logger.info("=== Alpha-SWE Agent 关闭 ===")


if __name__ == "__main__":
    main()