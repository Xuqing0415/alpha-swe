"""AST 感知的代码文件摘要（阶段一 1.1）。

当 Agent 读取代码文件时，除原始文本外还提供结构化摘要：类/函数/方法签名、
导入依赖、导出符号——让 Agent 在决策时看到"这个文件提供了什么能力"。
Python 使用标准库 ast 精确解析；JS/TS 优先 tree-sitter（已安装时）精确提取，否则回退正则近似（零硬依赖）。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from agent.code.language_parser import ALL_CODE_EXTS

CODE_EXTS = set(ALL_CODE_EXTS)
# 由 language_parser（正则回退 + tree-sitter 可选）处理的语言
_GENERIC_LANGS = {"java", "go", "rust", "c", "cpp", "csharp",
                 "ruby", "php"}

_JS_FUNC = re.compile(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
_JS_ARROW = re.compile(
    r"\b(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
_JS_CLASS = re.compile(r"\b(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")
_JS_IMPORT = re.compile(r"import\s+[^'\";\n]*?\s+from\s+['\"]([^'\"]+)['\"]")
_JS_REQUIRE = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")
_JS_EXPORT = re.compile(
    r"\bexport\s+(?:default\s+)?(?:function|class|const)\s+([A-Za-z_$][\w$]*)"
)


@dataclass
class Symbol:
    """单个符号（类/函数/方法/常量）的摘要信息。"""
    name: str
    kind: str  # class | function | method | const
    line: int
    args: str = ""
    decorators: List[str] = field(default_factory=list)


@dataclass
class FileAstSummary:
    """一个代码文件的结构摘要。"""
    path: str
    language: str
    symbols: List[Symbol] = field(default_factory=list)
    exports: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.symbols or self.exports or self.imports)

    def to_text(self, max_symbols: int = 30) -> str:
        lines = [f"[代码结构摘要] 语言: {self.language}"]
        if self.imports:
            lines.append(f"导入依赖: {', '.join(sorted(set(self.imports))[:20])}")
        syms = self.symbols[:max_symbols]
        if syms:
            lines.append("符号:")
            for s in syms:
                head = f"- {s.name} ({s.kind}"
                if s.args:
                    head += f", 参数: {s.args}"
                if s.decorators:
                    head += f", 装饰器: {','.join(s.decorators)}"
                lines.append(head + ")")
        if len(self.symbols) > max_symbols:
            lines.append(f"...共 {len(self.symbols)} 个符号，仅显示前 {max_symbols} 个")
        if self.exports:
            lines.append(f"导出: {', '.join(sorted(set(self.exports))[:20])}")
        return "\n".join(lines)


def is_code_file(path: str) -> bool:
    """按扩展名判断是否为可解析的代码文件。"""
    return Path(path).suffix.lower() in CODE_EXTS


def summarize_file(path: str, text: str) -> FileAstSummary:
    """对代码文件生成结构摘要；非代码文件/解析失败返回空摘要。"""
    ext = Path(path).suffix.lower()
    lang = ext.lstrip(".") or "text"
    if not text or ext not in CODE_EXTS:
        return FileAstSummary(path, lang)
    if ext == ".py":
        try:
            return _summarize_python(path, text)
        except SyntaxError:
            return FileAstSummary(path, "python")
    if lang in _GENERIC_LANGS:
        return _summarize_generic(path, text, lang)
    return _summarize_js_ts(path, text, lang)


def _summarize_generic(path: str, text: str, lang: str) -> FileAstSummary:
    """多语言摘要：复用 language_parser 的统一符号/导入提取。"""
    from agent.code.language_parser import parse_file
    pf = parse_file(path, text, lang)
    symbols = [Symbol(s.name, s.kind, s.line, s.args) for s in pf.symbols]
    exports = [s.name for s in pf.symbols if s.kind in (
        "class", "function", "type", "module", "trait", "interface",
        "enum", "struct") and not s.name.startswith("_")]
    return FileAstSummary(path, lang, symbols, exports, list(pf.imports))


def _summarize_python(path: str, text: str) -> FileAstSummary:
    tree = ast.parse(text)
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    symbols: List[Symbol] = []
    exports: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(Symbol(node.name, "function", node.lineno,
                                  _python_args(node.args),
                                  _python_decorators(node)))
            if not node.name.startswith("_"):
                exports.append(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol(node.name, "class", node.lineno,
                                  "", _python_decorators(node)))
            if not node.name.startswith("_"):
                exports.append(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(Symbol(
                        f"{node.name}.{sub.name}", "method", sub.lineno,
                        _python_args(sub.args), _python_decorators(sub)))
    return FileAstSummary(path, "python", symbols, exports, imports)


def _python_args(args) -> str:
    if args is None:
        return ""
    names = [a.arg for a in args.args if a.arg not in ("self", "cls")]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return ", ".join(names)


def _python_decorators(node) -> List[str]:
    out: List[str] = []
    for d in node.decorator_list:
        if isinstance(d, ast.Name):
            out.append(d.id)
        elif isinstance(d, ast.Attribute) and isinstance(d.value, ast.Name):
            out.append(f"{d.value.id}.{d.attr}")
    return out


def _summarize_js_ts(path: str, text: str, lang: str) -> FileAstSummary:
    ts_summary = _summarize_js_ts_tree_sitter(path, text, lang)
    if ts_summary is not None:
        return ts_summary
    imports = [m.group(1) for m in _JS_IMPORT.finditer(text)]
    imports += [m.group(1) for m in _JS_REQUIRE.finditer(text)]
    symbols: List[Symbol] = []
    exports: List[str] = []
    seen: set = set()
    for m in _JS_FUNC.finditer(text):
        _add_js_symbol(text, m, "function", symbols, exports, seen)
    for m in _JS_ARROW.finditer(text):
        _add_js_symbol(text, m, "const", symbols, exports, seen)
    for m in _JS_CLASS.finditer(text):
        _add_js_symbol(text, m, "class", symbols, exports, seen)
    for m in _JS_EXPORT.finditer(text):
        if m.group(1) not in exports:
            exports.append(m.group(1))
    return FileAstSummary(path, lang, symbols, exports, imports)


def _summarize_js_ts_tree_sitter(path: str, text: str,
                                 lang: str) -> "Optional[FileAstSummary]":
    """tree-sitter 精确提取；不可用或解析失败返回 None，由调用方回退正则。"""
    try:
        from agent.code import ts_parser
        if not ts_parser.available():
            return None
        info = ts_parser.parse_js_ts(path, text)
        symbols = [
            Symbol(s.name, s.kind, s.line, s.args, s.decorators)
            for s in info.symbols
        ]
        return FileAstSummary(path, lang, symbols,
                              list(info.exports), list(info.imports))
    except Exception:
        return None


def _add_js_symbol(text: str, m: "re.Match", kind: str,
                   symbols: List[Symbol], exports: List[str], seen: set) -> None:
    name = m.group(1)
    pos = m.start()
    if (name, kind) in seen:
        return
    seen.add((name, kind))
    line = text.count("\n", 0, pos) + 1
    symbols.append(Symbol(name, kind, line))
    if _is_exported(text, pos) and name not in exports:
        exports.append(name)


def _is_exported(text: str, pos: int) -> bool:
    return "export" in text[max(0, pos - 16):pos]
