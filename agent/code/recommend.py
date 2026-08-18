# -*- coding: utf-8 -*-
"""issue → 文件推荐（方向一·优化 3.1 增强代码检索与定位）。

从 issue 文本提取标识符/关键词，与仓库文件内容做词元重叠打分；
路径片段命中加分；结合调用图把与高分文件有调用关系的
文件一并纳入候选，输出“候选文件”注入规划提示，
减少 Agent 盲目搜索。

设计要点：
- 纯规则、离线可测，不引入额外依赖；
- 打分可解释：content_hits / path_hits / impact_boost；
- 供 Planner / TestRunnerTool / Prompt 复用。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".idea", ".vscode", ".swe-agent", ".codex", ".agents", "logs",
    "test_workspace", "coverage", ".tox", ".nox", ".eggs",
})
_CODE_SUFFIXES = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb",
    ".php", ".swift", ".kt", ".sh", ".toml", ".yaml", ".yml", ".json",
    ".md", ".txt", ".html", ".css",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_STOPWORDS = frozenset(
    "我们 你们 他们 她们 它们 这个 那个 这些 那些 一个 一种 一些 这样 那样 "
    "问题 需要 应该 可以 必须 进行 通过 如果 但是 因为 所以 而且 或者 以及 "
    "没有 不是 就是 是否 怎么 什么 如何 修复 解决 支持 使用 当前 目前 已经 "
    "之前 之后 其中 同时 由于 导致 出现 发生 存在 相关 情况 时候 方式 方法".split()
)
# 无话语分别力的全局词汇（几乎出现在每个 SWE-bench issue 里）
_STOPWORDS = frozenset(
    "the a an is are was were be been being to of in on for with as at by "
    "from or and not no if then else when this that these those it its we "
    "our you your they their he she him her his i my me do does did done "
    "have has had can could will would should shall may might must about "
    "into over under again further once here there all any both each few "
    "more most other some such only own same so than too very just but "
    "what which who whom whose where why how while during before after "
    "above below up down out off through also because until against "
    "between new old now get got make made use used using fix fixed bug "
    "issue error fail failed test tests testing should currently expected "
    "actual result results value values code function method class module "
    "file when calling called call work works working please add added "
    "change changed changes problem problems happen happens instead need "
    "needed without with current behavior behave".split()
)


def _cjk_bigrams(text: str) -> List[str]:
    """从连续 CJK 字符提取二元组词元，支持中文 issue 与中文注释匹配。"""
    out: List[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _CJK_STOPWORDS:
                out.append(bg)
    return out


def _tokenize(text: str) -> List[str]:
    """提取标识符并拆分 snake_case/camelCase 词元；附加 CJK 二元组。"""
    words: List[str] = []
    for m in _IDENT_RE.finditer(text or ""):
        tok = m.group(0)
        low = tok.lower()
        if len(tok) < 2:
            continue
        pieces = {low}
        pieces.update(low.split("_"))
        pieces.update(re.findall(r"[a-z]+", tok))
        words.extend(p for p in pieces if len(p) >= 2 and p not in _STOPWORDS)
    words.extend(_cjk_bigrams(text or ""))
    return words


def term_weights(issue_text: str) -> Counter:
    return Counter(_tokenize(issue_text))


def _content_hits(content: str, terms: Counter) -> int:
    cnt = Counter(_tokenize(content))
    return sum(w * cnt.get(t, 0) for t, w in terms.items())


def _path_hits(rel: str, terms: Counter) -> float:
    parts = [p.lower() for p in Path(rel).parts]
    return sum(2.0 for t in terms if any(t in p for p in parts))


def score_files(issue_text: str, files: Iterable[str], root: str = ".",
                call_graph=None, top_k: int = 8,
                read_head: int = 4096) -> List[Dict[str, object]]:
    """对相对路径文件列表打分，返回排序后的推荐结果。"""
    terms = term_weights(issue_text)
    if not terms:
        return []
    root_p = Path(root)
    rows: List[Dict[str, object]] = []
    for rel in files:
        rel = str(rel).replace("\\", "/")
        content = ""
        try:
            with (root_p / rel).open("r", encoding="utf-8",
                                     errors="ignore") as fh:
                content = fh.read(read_head)
        except OSError:
            pass
        rows.append({
            "path": rel,
            "content_hits": _content_hits(content, terms),
            "path_hits": _path_hits(rel, terms),
            "impact_boost": 0.0,
            "score": 0.0,
        })
    rows.sort(key=lambda r: -(int(r["content_hits"]) + r["path_hits"]))
    if call_graph is not None:
        impacted: set = set()
        for r in rows[: max(4, top_k * 2)]:
            if int(r["content_hits"]) + r["path_hits"] > 0:
                impacted.update(call_graph.impact_files(str(r["path"])))
        for r in rows:
            if r["path"] in impacted:
                r["impact_boost"] = 1.0
    for r in rows:
        r["score"] = int(r["content_hits"]) + r["path_hits"] + r["impact_boost"]
    rows = [r for r in rows if r["score"] > 0]
    rows.sort(key=lambda r: (-r["score"], str(r["path"])))
    return rows[:top_k]


def recommend_files(issue_text: str, workspace: str, call_graph=None,
                    top_k: int = 8, read_head: int = 4096) -> List[Dict[str, object]]:
    """扫描 workspace 内代码文件，返回推荐文件（相对路径）+ 分数。"""
    root = Path(workspace)
    rel_files: List[str] = []
    if root.is_dir():
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if _skip(rel):
                continue
            rel_files.append(rel)
    return score_files(issue_text, rel_files, root=str(root),
                       call_graph=call_graph, top_k=top_k,
                       read_head=read_head)


def format_recommendations(scored: List[Dict[str, object]],
                           top: int = 8) -> str:
    """把推荐结果渲染为规划提示的文本块。"""
    if not scored:
        return ""
    lines = ["### 候选文件（依据 issue 关键词与调用图影响面，建议优先查看）"]
    for r in scored[:top]:
        detail = "score=%.1f (content=%d path=%.1f%s)" % (
            r["score"], r["content_hits"], r["path_hits"],
            " impact=1" if r["impact_boost"] else "")
        lines.append(f"- {r['path']}  {detail}")
    return "\n".join(lines)


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return (any(seg in _SKIP_DIRS or seg.startswith(".") for seg in parts)
            or Path(rel).suffix.lower() not in _CODE_SUFFIXES)
