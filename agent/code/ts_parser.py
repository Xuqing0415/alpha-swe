"""JS/TS tree-sitter 精确解析模块（阶段一 1.1/1.2 增强）。

基于 tree-sitter 获取符号定义（函数/类/方法/const 箭头函数/接口/枚举）、
导入导出与调用边；tree-sitter 未安装时由调用方回退到正则近似实现。

- tree-sitter / tree-sitter-javascript / tree-sitter-typescript 均为可选依赖。
- 对外暴露 available() / parse_js_ts()；解析失败抛 TsParseError，由调用方回退。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("alpha-swe.code.ts")

_TS_EXTS = {".ts", ".tsx"}
_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs"}


class TsParseError(RuntimeError):
    """tree-sitter 解析失败（语法无法恢复等），调用方应回退正则。"""


def available() -> bool:
    """tree-sitter 及其 JS/TS grammar 是否可用。"""
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_javascript  # noqa: F401
        import tree_sitter_typescript  # noqa: F401
        return True
    except ImportError:
        return False


_parsers: Dict[str, object] = {}


def _get_parser(ext: str):
    """按扩展名惰性创建并缓存 JS 或 TS/TSX 解析器。"""
    if ext in _parsers:
        return _parsers[ext]
    from tree_sitter import Language, Parser
    if ext in _TS_EXTS:
        from tree_sitter_typescript import language_tsx, language_typescript
        key = "tsx" if ext == ".tsx" else "ts"
        lang_fn = language_tsx if ext == ".tsx" else language_typescript
    else:
        from tree_sitter_javascript import language as js_lang
        key, lang_fn = "js", js_lang
    if key not in _parsers:
        _parsers[key] = Parser(Language(lang_fn()))
    parser = _parsers[key]
    _parsers[ext] = parser
    return parser


@dataclass
class TsSymbol:
    """tree-sitter 提取的单个符号定义。"""
    name: str
    kind: str          # function | const | class | method | interface | enum
    line: int          # 1-based 起始行
    args: str = ""     # 参数列表文本（如 "(n, m)"）
    decorators: List[str] = field(default_factory=list)


@dataclass
class TsFileInfo:
    """一次 JS/TS 解析的完整结果。"""
    language: str              # javascript | typescript | tsx
    symbols: List[TsSymbol] = field(default_factory=list)
    exports: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    calls: List[Tuple[str, str]] = field(default_factory=list)
    # calls: (owner_symbol, callee)；owner 为空表示模块顶层调用


_DECL_TYPES = ("function_declaration", "class_declaration",
               "lexical_declaration", "interface_declaration",
               "enum_declaration")


def _child_of_type(node, types) -> Optional[object]:
    for c in node.children:
        if c.type in types:
            return c
    return None


def _name_of(node) -> str:
    """取声明节点的名字：identifier / type_identifier / property_identifier。"""
    for c in node.children:
        if c.type in ("identifier", "type_identifier", "property_identifier"):
            return c.text.decode("utf-8", errors="replace")
    return ""


def _call_callee(fn) -> str:
    """取调用目标：identifier 直接返回；member_expression 返回最后的 property。"""
    if fn.type == "identifier":
        return fn.text.decode("utf-8", errors="replace")
    prop = None
    for c in fn.children:
        if c.type == "property_identifier":
            prop = c
    if prop is None:
        prop = _child_of_type(fn, ("identifier",))
    return prop.text.decode("utf-8", errors="replace") if prop is not None else ""


def _params_text(node) -> str:
    for c in node.children:
        if c.type == "formal_parameters":
            return c.text.decode("utf-8", errors="replace").strip()
    return ""


def _decorators(node) -> List[str]:
    out: List[str] = []
    for c in node.children:
        if c.type == "decorator":
            txt = c.text.decode("utf-8", errors="replace").lstrip("@").strip()
            name = txt.split("(", 1)[0].strip()
            if name:
                out.append(name)
    return out


def _walk_call_edges(node, owner: str, out: List[Tuple[str, str]],
                     depth: int = 0) -> None:
    """在符号体内收集 call_expression：callee 为 identifier 或 member 的 property。"""
    if depth > 32 or node is None:
        return
    if node.type == "call_expression":
        fn = _child_of_type(node, ("identifier", "member_expression"))
        if fn is not None:
            callee = _call_callee(fn)
            if callee and callee != "require":
                out.append((owner, callee))
    for c in node.children:
        _walk_call_edges(c, owner, out, depth + 1)


def parse_js_ts(path: str, text: str) -> TsFileInfo:
    """解析 JS/TS 源码，返回符号/导出/导入/调用边。"""
    ext = Path(path).suffix.lower()
    lang = ("tsx" if ext == ".tsx"
            else "typescript" if ext in _TS_EXTS
            else "javascript")
    parser = _get_parser(ext)
    tree = parser.parse(bytes(text, "utf-8"))
    root = tree.root_node
    if root.has_error:
        raise TsParseError(f"tree-sitter 语法错误: {path}")
    info = TsFileInfo(language=lang)

    for decl in root.children:
        if decl.type == "export_statement":
            _process_export(decl, info)
        elif decl.type == "import_statement":
            src = _import_source(decl)
            if src and src not in info.imports:
                info.imports.append(src)
        else:
            _process_decl(decl, info, exported=False)
    info.calls = list(dict.fromkeys(info.calls))
    return info


def _process_export(node, info: TsFileInfo) -> None:
    """export 语句：包裹的声明按导出处理；export { a, b } 收集名字；re-export 记导入。"""
    for inner in node.children:
        if inner.type in _DECL_TYPES:
            _process_decl(inner, info, exported=True)
        elif inner.type == "export_clause":
            for spec in inner.children:
                if spec.type in ("identifier", "type_identifier"):
                    name = spec.text.decode("utf-8", errors="replace")
                    if name not in info.exports:
                        info.exports.append(name)
        elif inner.type == "call_expression":
            # export * from / export { x } from '...'
            for c in inner.children:
                if c.type == "string":
                    src = c.text.decode("utf-8", errors="replace").strip("'\"")
                    if src and src not in info.imports:
                        info.imports.append(src)


def _process_decl(decl, info: TsFileInfo, exported: bool) -> None:
    """处理一个顶层声明节点（可能被 export 包裹）。"""
    if decl.type == "function_declaration":
        name = _name_of(decl)
        if not name:
            return
        info.symbols.append(TsSymbol(
            name, "function", decl.start_point.row + 1,
            _params_text(decl), _decorators(decl)))
        _walk_call_edges(decl, name, info.calls)
        if exported:
            info.exports.append(name)
    elif decl.type == "class_declaration":
        name = _name_of(decl)
        if not name:
            return
        info.symbols.append(TsSymbol(
            name, "class", decl.start_point.row + 1, "", _decorators(decl)))
        if exported:
            info.exports.append(name)
        body = _child_of_type(decl, ("class_body",))
        if body is not None:
            for m in body.children:
                if m.type == "method_definition":
                    mname = _name_of(m)
                    if mname and mname != "constructor":
                        info.symbols.append(TsSymbol(
                            f"{name}.{mname}", "method",
                            m.start_point.row + 1, _params_text(m),
                            _decorators(m)))
                        _walk_call_edges(m, f"{name}.{mname}", info.calls)
    elif decl.type == "lexical_declaration":
        _lexical_symbols(decl, info, exported)
    elif decl.type == "interface_declaration":
        name = _name_of(decl)
        if name:
            info.symbols.append(TsSymbol(
                name, "interface", decl.start_point.row + 1,
                "", _decorators(decl)))
            if exported:
                info.exports.append(name)
    elif decl.type == "enum_declaration":
        name = _name_of(decl)
        if name:
            info.symbols.append(TsSymbol(
                name, "enum", decl.start_point.row + 1,
                "", _decorators(decl)))
            if exported:
                info.exports.append(name)


def _lexical_symbols(decl, info: TsFileInfo, exported: bool) -> None:
    """const/let/var 且值为箭头函数/函数/类的声明 → 符号。"""
    for d in decl.children:
        if d.type != "variable_declarator":
            continue
        name = _name_of(d)
        if not name:
            continue
        value = _child_of_type(d, ("arrow_function", "function", "class"))
        if value is None:
            continue
        kind = "class" if value.type == "class" else "const"
        info.symbols.append(TsSymbol(
            name, kind, d.start_point.row + 1,
            _params_text(value) if value.type != "class" else "",
            _decorators(d)))
        _walk_call_edges(d, name, info.calls)
        if exported and name not in info.exports:
            info.exports.append(name)


def _is_exported(node) -> bool:
    """判断声明是否被 export 包裹（向上找一层 export_statement）。"""
    p = node.parent
    return p is not None and p.type == "export_statement"


def _import_source(node) -> str:
    for c in node.children:
        if c.type == "string":
            return c.text.decode("utf-8", errors="replace").strip("'\"")
    return ""
