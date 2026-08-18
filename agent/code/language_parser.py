# -*- coding: utf-8 -*-
"""Unified multi-language parsing layer (Direction 2, Phase 2.1/2.2/2.3).

Route by file extension and provide:
- detect_language(): extension -> language name;
- parse_file(): symbols / call edges / imports;
- tree-sitter first (optional deps), regex fallback (zero hard deps).

Call attribution is position-based: each call records its byte offset, then is
assigned to the innermost enclosing symbol by line span.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

LANGUAGE_EXTS: Dict[str, frozenset] = {
    "python": frozenset({".py", ".pyw"}),
    "javascript": frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "java": frozenset({".java"}),
    "go": frozenset({".go"}),
    "rust": frozenset({".rs"}),
    "c": frozenset({".c", ".h"}),
    "cpp": frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}),
    "csharp": frozenset({".cs"}),
    "ruby": frozenset({".rb"}),
    "php": frozenset({".php", ".php5", ".phtml"}),
}

EXT_TO_LANGUAGE: Dict[str, str] = {
    ext: lang for lang, exts in LANGUAGE_EXTS.items() for ext in exts
}

ALL_CODE_EXTS: frozenset = frozenset(EXT_TO_LANGUAGE)

_TS_PACKAGES: Dict[str, str] = {
    "java": "tree_sitter_java",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "csharp": "tree_sitter_c_sharp",
    "ruby": "tree_sitter_ruby",
    "php": "tree_sitter_php",
    "python": "tree_sitter_python",
}

_CALL_STOPWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "sizeof", "return", "assert",
    "yield", "with", "lock", "defer", "go", "select", "case", "match",
    "when", "unless", "until", "each", "map", "filter", "include",
    "new",
})

_PHP_MODIFIERS = frozenset({
    "public", "private", "protected", "static", "final", "abstract",
})

_METHOD_MODIFIERS = (
    r"(?:public|private|protected|internal|static|final|abstract|virtual|"
    r"override|sealed|partial|readonly|synchronized|native|default|async)"
)

_Q = r"[\x22\x27]"           # double or single quote (regex char class)
_QUOTED = r"([^\x22\x27]+)"  # non-quote run

# 方法/函数声明前置的"类型前缀"若是关键字，说明是调用语句而非声明
_KEYWORD_PREFIX = frozenset({
    "return", "throw", "assert", "this", "super", "new", "instanceof",
    "case", "break", "continue", "goto", "synchronized", "else", "do",
})


@dataclass
class LangSymbol:
    """Language-neutral symbol definition."""
    name: str
    kind: str          # function | method | class | interface | enum |
                       # struct | trait | const | module | type | record
    line: int          # 1-based start line
    args: str = ""     # parameter list text (truncated)
    end_line: int = 0  # approx end line; 0 = unknown


@dataclass
class ParsedFile:
    """Result of one multi-language parse."""
    language: str
    symbols: List[LangSymbol] = field(default_factory=list)
    calls: List[Tuple[str, str]] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.symbols or self.calls or self.imports)


def detect_language(path) -> str:
    """Identify language by extension; empty string if unknown."""
    return EXT_TO_LANGUAGE.get(Path(str(path)).suffix.lower(), "")


def parse_file(path, text: str, language: str = "") -> ParsedFile:
    """Parse file content; tree-sitter first, regex fallback."""
    lang = language or detect_language(path)
    if not lang or not text:
        return ParsedFile(lang or "text")
    ts = _parse_with_tree_sitter(lang, text)
    if ts is not None:
        symbols, calls, imports = ts
        if symbols or calls or imports:
            return ParsedFile(lang, symbols, calls, imports)
    return _parse_regex(lang, text)


def tree_sitter_available(language: str) -> bool:
    """Whether the tree-sitter grammar for this language is installed."""
    mod = _TS_PACKAGES.get(language)
    if not mod:
        return False
    try:
        importlib.import_module(mod)
        import tree_sitter  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- tree-sitter
_TS_DECL_KINDS: Dict[str, str] = {
    "function_declaration": "function",
    "function_item": "function",
    "function_definition": "function",
    "method_declaration": "method",
    "method_definition": "method",
    "class_declaration": "class",
    "class_specifier": "class",
    "struct_specifier": "struct",
    "struct_item": "struct",
    "interface_declaration": "interface",
    "interface_item": "interface",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "record_declaration": "record",
    "type_declaration": "struct",     # Go: type X struct
    "trait_item": "trait",
    "impl_item": "impl",
    "module_declaration": "module",
    "class_definition": "class",
    "module_definition": "module",
}
_TS_CALL_TYPES = frozenset({
    "call_expression", "method_invocation", "call",
    "function_call_expression", "method_call", "invocation_expression",
})


def _ts_name_of(node) -> str:
    try:
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "name",
                              "property_identifier", "field_identifier"):
                return child.text.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        name_field = node.child_by_field_name("name")
        if name_field is not None:
            return name_field.text.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def _walk_ts(node, depth: int = 0):
    symbols: List[LangSymbol] = []
    calls: List[Tuple[str, str, int]] = []
    if depth > 256 or node is None:
        return symbols, calls
    ntype = getattr(node, "type", "")
    kind = _TS_DECL_KINDS.get(ntype, "")
    try:
        start_line = node.start_point[0] + 1
    except Exception:
        start_line = 0
    if kind:
        name = _ts_name_of(node)
        if name and not name.startswith("("):
            symbols.append(LangSymbol(name=name, kind=kind, line=start_line))
    if ntype in _TS_CALL_TYPES:
        callee = _ts_callee(node)
        if callee and callee.lower() not in _CALL_STOPWORDS:
            try:
                offset = node.start_byte
            except Exception:
                offset = 0
            calls.append(("", callee, offset))
    for child in getattr(node, "children", []):
        cs, cc = _walk_ts(child, depth + 1)
        symbols.extend(cs)
        calls.extend(cc)
    return symbols, calls


def _ts_callee(call_node) -> str:
    try:
        fn = call_node.child_by_field_name("function")
        if fn is None:
            for c in call_node.children:
                if c.type in ("identifier", "member_expression",
                              "field_expression", "attribute",
                              "scoped_identifier", "qualified_identifier",
                              "name"):
                    fn = c
                    break
        if fn is None:
            return ""
        if fn.type in ("identifier", "name", "field_identifier"):
            return fn.text.decode("utf-8", errors="replace")
        segs = []
        for c in fn.children:
            if c.type in ("identifier", "field_identifier",
                          "property_identifier", "type_identifier"):
                segs.append(c.text.decode("utf-8", errors="replace"))
        return segs[-1] if segs else ""
    except Exception:
        return ""


def _ts_imports(root, language: str) -> List[str]:
    out: List[str] = []
    import_types = frozenset({
        "import_declaration", "import_statement", "use_declaration",
        "preproc_include", "using_directive", "require_statement",
    })
    stack = [root]
    while stack:
        node = stack.pop()
        try:
            ntype = node.type
        except Exception:
            continue
        if ntype in import_types:
            txt = node.text.decode("utf-8", errors="replace")
            m = re.search(r"[\x22\x27<]([^\x22\x27>]+)[\x22\x27>]", txt)
            if m:
                out.append(m.group(1))
        stack.extend(getattr(node, "children", []))
        if len(out) > 64:
            break
    return out


def _parse_with_tree_sitter(language: str, text: str):
    """Best-effort tree-sitter parse; returns (symbols, calls, imports) or None."""
    mod_name = _TS_PACKAGES.get(language)
    if not mod_name:
        return None
    try:
        mod = importlib.import_module(mod_name)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, "language", None) or getattr(
            mod, "language_%s" % language, None)
        if lang_fn is None:
            return None
        parser = Parser(Language(lang_fn()))
        tree = parser.parse(text.encode("utf-8"))
        root = tree.root_node
        symbols, calls = _walk_ts(root)
        imports: List[str] = []
        for imp in _ts_imports(root, language):
            if imp not in imports:
                imports.append(imp)
        _attach_calls(symbols, calls, text)
        return symbols, [(o, c) for o, c, _ in calls], imports
    except Exception:
        return None


# ---------------------------------------------------------------- regex
_IMPORT_PATTERNS: Dict[str, object] = {}


def _imp(lang: str, pat: str) -> None:
    _IMPORT_PATTERNS[lang] = re.compile(pat, re.M)


_imp("java", r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;")
_imp("go", r"import\s+(?:\w+\s+)?" + _Q + _QUOTED + _Q)
_imp("rust", r"^\s*use\s+([\w:]+)(?:::\*)?\s*;")
_imp("c", r"^\s*#\s*include\s*[<\x22]((?:[^>\x22\s]+))[>\x22]")
_imp("cpp", r"^\s*#\s*include\s*[<\x22]((?:[^>\x22\s]+))[>\x22]")
_imp("csharp", r"^\s*using\s+([\w.]+)\s*;")
_imp("ruby", r"^\s*(?:require|require_relative)\s+" + _Q + _QUOTED + _Q)
_imp("php", r"^\s*use\s+([\\\w]+)\s*;")

# (pattern, kind, name_group, args_group, prefix_group_or_0)
_SYMBOL_PATTERNS: Dict[str, List[Tuple[object, str, int, int, int]]] = {}


def _syms(lang: str, pats) -> None:
    _SYMBOL_PATTERNS[lang] = [
        (re.compile(p, re.M), kind, ng, ag, pg) for p, kind, ng, ag, pg in pats
    ]


_syms("java", [
    (r"\b(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+"
     r"|static\s+)*\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)",
     "class", 2, 0, 0),
    (r"\b(?:public|protected|private|static|final|abstract|synchronized|"
     r"native)\s+(?:[\w<>\[\],?.\s]+?)\s+"
     r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?:throws[^{;]*)?\s*\{",
     "method", 1, 2, 0),
    (r"\b(?:[\w<>\[\],?.\s]+?)\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"
     r"\s*(?:throws[^{;]*)?\s*(?:\{|;)", "method", 1, 2, 0),
])
_syms("go", [
    (r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)",
     "function", 1, 2, 0),
    (r"\btype\s+([A-Za-z_]\w*)\s+(struct|interface)\b", "type", 1, 0, 0),
])
_syms("rust", [
    (r"\bfn\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", "function", 1, 2, 0),
    (r"\b(struct|enum|trait)\s+([A-Za-z_]\w*)", "type", 2, 0, 0),
    (r"\btype\s+([A-Za-z_]\w*)\s*=", "type", 1, 0, 0),
])
_syms("c", [
    (r"\b(?:[\w:<>*&\s]+?)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)"
     r"\s*(?:const\s*)?\{", "function", 1, 2, 0),
    (r"\b(class|struct|union)\s+([A-Za-z_]\w*)\b", "type", 2, 0, 0),
])
_syms("cpp", [
    (r"\b(?:[\w:<>*&\s]+?)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)"
     r"\s*(?:const\s*)?(?:override\s*)?(?:noexcept\s*)?\{",
     "function", 1, 2, 0),
    (r"\b(class|struct|union|enum)\s+([A-Za-z_]\w*)"
     r"\s*(?::\s*public\s+[\w]+\s*)?\{?", "type", 2, 0, 0),
])
_syms("csharp", [
    (r"\b(class|interface|struct|enum|record)\s+([A-Za-z_]\w*)",
     "type", 2, 0, 0),
    (r"\b(?:" + _METHOD_MODIFIERS + r"\s+)*(?:[\w<>\[\],?.\s]+?)\s+"
     r"([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:\{|=>|;)", "method", 1, 2, 0),
])
_syms("ruby", [
    (r"^\s*class\s+([A-Za-z_:]\w*)", "class", 1, 0, 0),
    (r"^\s*module\s+([A-Za-z_:]\w*)", "module", 1, 0, 0),
    (r"^\s*def\s+([A-Za-z_]\w*(?:[.!]\w+)?)\s*(?:\(([^)]*)\))?",
     "function", 1, 2, 0),
])
_syms("php", [
    (r"\b(?:public|private|protected|static|final|abstract)\s+"
     r"function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", "method", 1, 2, 0),
    (r"\b((?:public|private|protected|static|final|abstract)\s+)?"
     r"function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", "function", 2, 3, 1),
    (r"\b(class|interface|trait|enum)\s+([A-Za-z_]\w*)", "type", 2, 0, 0),
])

_CALL_PATTERNS: Dict[str, object] = {
    lang: re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(" if lang == "java"
                     else r"\b([A-Za-z_]\w*)\s*\(")
    for lang in ("java", "go", "rust", "c", "cpp", "csharp", "ruby", "php")
}


def _regex_imports(language: str, text: str) -> List[str]:
    pat = _IMPORT_PATTERNS.get(language)
    if pat is None:
        return []
    out: List[str] = []
    for m in pat.finditer(text):
        imp = m.group(1)
        if imp and imp not in out:
            out.append(imp)
    return out[:64]


def _regex_symbols(language: str, text: str) -> List[LangSymbol]:
    syms: List[LangSymbol] = []
    seen: set = set()
    for pat, kind, ng, ag, pg in _SYMBOL_PATTERNS.get(language, []):
        for m in pat.finditer(text):
            name = m.group(ng)
            if not name or name in _CALL_STOPWORDS:
                continue
            if pg:
                prefix = (m.group(pg) or "").strip()
                if prefix and prefix.split()[-1].lower() in _PHP_MODIFIERS:
                    continue
            if kind in ("method", "function") and not pg:
                # 名称前的最后一个词若是关键字，则是调用语句误匹配
                tail = re.search(r"(\w+)\s*$", text[:m.start(ng)])
                if tail and tail.group(1).lower() in _KEYWORD_PREFIX:
                    continue
            line = text.count("\n", 0, m.start()) + 1
            args = (m.group(ag) or "").strip() if ag else ""
            if len(args) > 160:
                args = args[:160] + "..."
            key = (name, kind, line)
            if key in seen:
                continue
            seen.add(key)
            syms.append(LangSymbol(name=name, kind=kind, line=line,
                                   args=args))
    # 计算 end_line：下一个符号行 - 1（同行的至少等于自身行）
    ordered = sorted(syms, key=lambda s: s.line)
    for i, s in enumerate(ordered):
        nxt = ordered[i + 1].line if i + 1 < len(ordered) else None
        if nxt is not None and nxt > s.line:
            s.end_line = nxt - 1
        else:
            s.end_line = max(s.line, s.end_line)
    return ordered


def _regex_calls(language: str, text: str) -> List[Tuple[str, str, int]]:
    pat = _CALL_PATTERNS.get(language)
    if pat is None:
        return []
    out: List[Tuple[str, str, int]] = []
    seen: set = set()
    for m in pat.finditer(text):
        callee = m.group(1)
        if (not callee or callee.lower() in _CALL_STOPWORDS
                or len(callee) < 2):
            continue
        # new Foo(...) 是构造，不是调用
        before = text[max(0, m.start() - 8):m.start()]
        if re.search(r"\bnew\s+$", before):
            continue
        key = (m.start(), callee)
        if key in seen:
            continue
        seen.add(key)
        out.append(("", callee, m.start()))
    return out


def _attach_calls(symbols: List[LangSymbol],
                  calls: List[Tuple[str, str, int]], text: str) -> None:
    """Assign each call to the innermost enclosing symbol by byte offset."""
    if not calls:
        return
    ordered = sorted(symbols, key=lambda s: s.line)
    total_lines = text.count("\n") + 1
    for i, (owner, callee, offset) in enumerate(calls):
        if owner:
            continue
        line = (text.count("\n", 0, offset) + 1
                if 0 <= offset < len(text) else 1)
        best = ""
        for j, s in enumerate(ordered):
            end = s.end_line or s.line
            if j == len(ordered) - 1 and end == s.line:
                end = total_lines   # last symbol: span to EOF
            if s.line <= line <= end:
                best = s.name
        if best == callee:
            best = ""   # 声明本身的调用噪声：归为顶层
        calls[i] = (best, callee, offset)


def _parse_regex(language: str, text: str) -> ParsedFile:
    symbols = _regex_symbols(language, text)
    calls = _regex_calls(language, text)
    _attach_calls(symbols, calls, text)
    return ParsedFile(
        language=language,
        symbols=symbols,
        calls=[(o, c) for o, c, _ in calls],
        imports=_regex_imports(language, text),
    )


def extract_symbols(path, text: str, language: str = ""):
    """Convenience entry: return LangSymbol list."""
    return parse_file(path, text, language).symbols


def extract_calls(path, text: str, language: str = ""):
    """Convenience entry: return (owner, callee) call-edge list."""
    return parse_file(path, text, language).calls
