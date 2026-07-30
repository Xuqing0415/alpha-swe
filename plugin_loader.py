"""第四关：Skills/Plugins 插入式 Context（热加载）
扫描 ./skills/ 目录，根据工作目录或用户指令动态注入技能模块。
修改 skills 文件夹后无需重启进程，下次循环自动加载新内容。
"""
import os
import re
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger("alpha-swe.plugin")


class PluginLoader:
    """技能插件热加载器"""

    SKILL_PATTERN = re.compile(r'@skill\((\w+)\)', re.IGNORECASE)

    def __init__(self, skills_dir: str = "./skills"):
        self.skills_dir = skills_dir
        self.loaded_skills: Dict[str, dict] = {}
        self._last_load_time: Dict[str, float] = {}
        self._refresh()

    def _refresh(self):
        """扫描并加载所有技能文件"""
        if not os.path.isdir(self.skills_dir):
            logger.warning(f"技能目录不存在: {self.skills_dir}")
            return

        for filename in os.listdir(self.skills_dir):
            filepath = os.path.join(self.skills_dir, filename)
            if not os.path.isfile(filepath):
                continue

            # 检查文件是否更新
            mtime = os.path.getmtime(filepath)
            if filename in self._last_load_time and self._last_load_time[filename] >= mtime:
                continue  # 文件未变化，跳过

            self._last_load_time[filename] = mtime

            if filename.endswith(".md"):
                self._load_md_skill(filename, filepath)
            elif filename.endswith(".py"):
                self._load_py_skill(filename, filepath)

        logger.info(f"技能加载完成: {list(self.loaded_skills.keys())}")

    def _load_md_skill(self, filename: str, filepath: str):
        """加载 Markdown 技能文件"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            # 提取 @skill(name) 标记
            skill_names = self.SKILL_PATTERN.findall(content)
            if not skill_names:
                # 默认使用文件名
                skill_names = [filename.replace(".md", "")]

            # 提取第一行作为标题
            title = content.split("\n")[0].strip("# ").strip()

            for name in skill_names:
                self.loaded_skills[name] = {
                    "type": "markdown",
                    "file": filename,
                    "title": title,
                    "content": content,
                    "loaded_at": datetime.now().isoformat()
                }
            logger.info(f"热加载技能: {skill_names} from {filename}")
        except Exception as e:
            logger.error(f"加载技能文件失败 {filename}: {e}")

    def _load_py_skill(self, filename: str, filepath: str):
        """加载 Python 技能文件"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            # 提取 @skill(name) 标记
            skill_names = self.SKILL_PATTERN.findall(content)
            if not skill_names:
                skill_names = [filename.replace(".py", "")]

            # 提取模块级 docstring
            doc_match = re.search(r'"""(.*?)"""', content, re.DOTALL)
            description = doc_match.group(1).strip() if doc_match else content[:200]

            for name in skill_names:
                self.loaded_skills[name] = {
                    "type": "python",
                    "file": filename,
                    "title": name,
                    "content": description,
                    "loaded_at": datetime.now().isoformat()
                }
            logger.info(f"热加载技能: {skill_names} from {filename}")
        except Exception as e:
            logger.error(f"加载技能文件失败 {filename}: {e}")

    def load_for_context(self, user_prompt: str) -> str:
        """根据用户指令，匹配并加载相关技能上下文"""
        self._refresh()  # 热加载：每次调用都检查文件更新

        matched = []
        prompt_lower = user_prompt.lower()

        # 关键词匹配
        skill_keywords = {
            "python": ["python", "py", "django", "flask", "fastapi"],
            "react": ["react", "jsx", "javascript", "component", "前端"],
            "django": ["django", "orm", "model", "migration"],
            "git": ["git", "commit", "branch", "merge", "pr"],
            "docker": ["docker", "container", "image", "compose"],
            "testing": ["test", "pytest", "jest", "unittest", "测试"],
            "database": ["sql", "mysql", "postgres", "database", "migration"],
        }

        for skill_name, keywords in skill_keywords.items():
            if any(kw in prompt_lower for kw in keywords):
                if skill_name in self.loaded_skills:
                    matched.append(skill_name)

        if not matched:
            return ""

        # 构建注入上下文
        context_parts = ["[已激活技能模块]"]
        for name in matched:
            skill = self.loaded_skills[name]
            context_parts.append(f"\n### {skill['title']} ({name})")
            context_parts.append(skill["content"][:500])  # 截断过长内容

        return "\n".join(context_parts)

    def get_skill(self, name: str) -> Optional[dict]:
        """获取指定技能"""
        self._refresh()
        return self.loaded_skills.get(name)

    def list_skills(self) -> List[str]:
        """列出所有已加载技能"""
        self._refresh()
        return list(self.loaded_skills.keys())