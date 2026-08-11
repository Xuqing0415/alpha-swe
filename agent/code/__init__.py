"""代码语义理解层 —— 阶段一（1.1 AST 感知 / 1.2 调用图 / 1.3 项目约定）。

让 Agent 拥有程序员级别的代码感知能力：
- ast_summary：读取代码文件时附带结构化摘要（类/函数/依赖/导出）；
- call_graph：项目级轻量函数调用图，支撑影响范围分析；
- project_profile：项目约定与技术栈自动提取，持久化注入 Prompt。
"""
from agent.code.ast_summary import (FileAstSummary, Symbol, is_code_file,
                                    summarize_file)
from agent.code.call_graph import CallGraph, build_call_graph
from agent.code.project_profile import ProjectProfile, build_profile

__all__ = ["FileAstSummary", "Symbol", "is_code_file", "summarize_file",
           "CallGraph", "build_call_graph", "ProjectProfile", "build_profile"]
