"""上下文管理 —— 插件/技能动态激活 + 自动压缩（对应设计第 10、11 节）。"""
from agent.context.manager import ContextManager
from agent.context.plugin import Plugin, PluginManager, ProjectContext
from agent.context.skill import Skill, SkillLibrary, SkillStep

__all__ = ["ContextManager", "Plugin", "PluginManager", "ProjectContext",
           "Skill", "SkillLibrary", "SkillStep"]
