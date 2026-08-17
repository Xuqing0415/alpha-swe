"""第四关：Skills/Plugins 插入式 Context（热加载 + Manifest）
扫描 ./skills/ 目录，根据 skill_manifest.json 的优先级/触发条件动态注入技能模块。
修改 skills 文件夹后无需重启进程，下次循环自动加载新内容。
"""
import os
import re
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger("alpha-swe.plugin")


class PluginLoader:
    """技能插件热加载器（支持 Manifest 优先级管理）"""

    SKILL_PATTERN = re.compile(r'@skill\((\w+)\)', re.IGNORECASE)

    def __init__(self, skills_dir: str = "./skills"):
        self.skills_dir = skills_dir
        self.loaded_skills: Dict[str, dict] = {}
        self.manifest: Dict[str, dict] = {}
        self._last_load_time: Dict[str, float] = {}
        self._load_manifest()
        self._refresh()

    def _load_manifest(self):
        """加载 skill_manifest.json"""
        manifest_path = os.path.join(self.skills_dir, "skill_manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                    self.manifest = data.get("skills", {})
                    logger.info(f"Manifest 加载完成: {len(self.manifest)} 个技能定义")
            except Exception as e:
                logger.error(f"Manifest 加载失败: {e}")

    def _refresh(self):
        """扫描并加载所有技能文件"""
        if not os.path.isdir(self.skills_dir):
            logger.warning(f"技能目录不存在: {self.skills_dir}")
            return

        # 重新加载 manifest（热加载）
        self._load_manifest()

        for filename in os.listdir(self.skills_dir):
            if filename == "skill_manifest.json":
                continue
            filepath = os.path.join(self.skills_dir, filename)
            if not os.path.isfile(filepath):
                continue

            mtime = os.path.getmtime(filepath)
            if filename in self._last_load_time and self._last_load_time[filename] >= mtime:
                continue

            self._last_load_time[filename] = mtime

            if filename.endswith(".md"):
                self._load_md_skill(filename, filepath)
            elif filename.endswith(".py"):
                self._load_py_skill(filename, filepath)

        logger.debug(f"技能加载完成: {list(self.loaded_skills.keys())}")

    def _load_md_skill(self, filename: str, filepath: str):
        """加载 Markdown 技能文件"""
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                content = f.read()

            skill_names = self.SKILL_PATTERN.findall(content)
            if not skill_names:
                skill_names = [filename.replace(".md", "")]
            title = content.split("\n")[0].strip("# ").strip()

            for name in skill_names:
                # 合并 manifest 中的元数据
                manifest_info = self.manifest.get(name, {})
                self.loaded_skills[name] = {
                    "type": "markdown",
                    "file": filename,
                    "title": title,
                    "content": content,
                    "loaded_at": datetime.now().isoformat(),
                    "priority": manifest_info.get("priority", 1),
                    "version": manifest_info.get("version", "0.0.0"),
                    "triggers": manifest_info.get("triggers", []),
                    "requires": manifest_info.get("requires", []),
                }
            logger.info(f"热加载技能: {skill_names} from {filename}")
        except Exception as e:
            logger.error(f"加载技能文件失败 {filename}: {e}")

    def _load_py_skill(self, filename: str, filepath: str):
        """加载 Python 技能文件"""
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                content = f.read()

            skill_names = self.SKILL_PATTERN.findall(content)
            if not skill_names:
                skill_names = [filename.replace(".py", "")]
            doc_match = re.search(r'"""(.*?)"""', content, re.DOTALL)
            description = doc_match.group(1).strip() if doc_match else content[:200]

            for name in skill_names:
                manifest_info = self.manifest.get(name, {})
                self.loaded_skills[name] = {
                    "type": "python",
                    "file": filename,
                    "title": name,
                    "content": description,
                    "loaded_at": datetime.now().isoformat(),
                    "priority": manifest_info.get("priority", 1),
                    "version": manifest_info.get("version", "0.0.0"),
                    "triggers": manifest_info.get("triggers", []),
                    "requires": manifest_info.get("requires", []),
                }
            logger.info(f"热加载技能: {skill_names} from {filename}")
        except Exception as e:
            logger.error(f"加载技能文件失败 {filename}: {e}")

    def _resolve_dependencies(self, matched: List[str]) -> List[str]:
        """解析技能依赖，确保 required 技能也被加载"""
        resolved = set(matched)
        changed = True
        while changed:
            changed = False
            for name in list(resolved):
                skill = self.loaded_skills.get(name, {})
                for req in skill.get("requires", []):
                    if req in self.loaded_skills and req not in resolved:
                        resolved.add(req)
                        changed = True
        return list(resolved)

    def load_for_context(self, user_prompt: str) -> str:
        """根据用户指令，匹配并加载相关技能上下文（优先级排序）"""
        self._refresh()

        prompt_lower = user_prompt.lower()
        matched_scores = []

        # 使用 manifest 中的 triggers 做匹配
        for name, skill in self.loaded_skills.items():
            triggers = skill.get("triggers", [])
            if not triggers:
                continue

            # 计算匹配分数
            score = 0
            for trigger in triggers:
                if trigger in prompt_lower:
                    score += 1

            # 检查 exclude_triggers
            excludes = skill.get("exclude_triggers", [])
            if any(ex in prompt_lower for ex in excludes):
                score = 0

            if score > 0:
                matched_scores.append((name, score, skill.get("priority", 1)))

        # 按优先级降序、匹配分数降序排列
        matched_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)

        # 限制数量
        max_skills = self.manifest.get("global", {}).get("max_skills_per_request", 3)
        matched = [name for name, _, _ in matched_scores[:max_skills]]

        # 依赖解析
        matched = self._resolve_dependencies(matched)

        if not matched:
            return ""

        context_parts = ["[已激活技能模块]"]
        for name in matched:
            skill = self.loaded_skills[name]
            ver = skill.get("version", "")
            context_parts.append(f"\n### {skill['title']} ({name} v{ver}) [priority={skill.get('priority', 1)}]")
            context_parts.append(skill["content"][:500])

        return "\n".join(context_parts)

    def unload_skill(self, name: str) -> bool:
        """卸载技能"""
        if name in self.loaded_skills:
            del self.loaded_skills[name]
            logger.info(f"技能已卸载: {name}")
            return True
        return False

    def get_skill(self, name: str) -> Optional[dict]:
        self._refresh()
        return self.loaded_skills.get(name)

    def list_skills(self) -> List[str]:
        self._refresh()
        return list(self.loaded_skills.keys())
