# -*- coding: utf-8 -*-
"""敏感信息脱敏工具 —— 保守、低误伤的文本层/字典层脱敏。

只提供脱敏函数，不接入任何主流程；需要时直接调用即可，例如：

    >>> redact_secrets("key=" + "sk-" + "A" * 48)
    'key=***REDACTED***'

文本层规则：
- OpenAI / DeepSeek 形态的 ``sk-...`` 长串密钥（左侧不粘连字母数字，且长度足够，
  避免误伤 "task-2024-..."、"risk-assessment" 之类的普通文本）；
- URL 内嵌凭据 ``https://user:pass@host``（保留 scheme 与 host）；
- 行首 ``authorization`` 头的整段值（含 Basic / 其它原始凭据）；
- ``Bearer <token>``（token 至少 8 字符，避免误伤 "Bearer token" 等普通措辞）。

字典层（redact_dict）：键名规范化后若含 api_key/token/secret/password/
authorization（以及独立词元 "key"）等标记，则整段值替换为脱敏串；嵌套的
dict / list 递归处理，非敏感字符串值仍会被文本层规则扫描。普通中英文内容
一律保持原样。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Sequence, Tuple

__all__ = ["redact_secrets", "redact_dict", "redact_value"]

_REDACTED = "***REDACTED***"

# sk-... 形态（OpenAI/DeepSeek 等）。左侧不能粘连字母数字、且串长 >= 16，
# 以避开 "task-2024-..."、"sk-123" 之类的普通文本。
_SK_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")

# https://user:pass@host 内嵌凭据
_URL_CRED_RE = re.compile(r"(?i)(\bhttps?://)([^/@\s:]+):([^/@\s]+)@")

# 行首 authorization 头：整段值脱敏，保留行首缩进与 "authorization: " 前缀
_AUTH_HEADER_RE = re.compile(
    r"(?im)^([ \t]*)(authorization[ \t]*[:=][ \t]*)[^\r\n]+")

# Bearer <token>：token 至少 8 字符，避免把 "Bearer token" 当密钥误伤
_BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{8,}")


def _tokens(name: str) -> Tuple[str, ...]:
    """键名规范化为小写词元序列（camelCase / 蛇形 / 连字符均归一到下划线分词）。

    "apiKey" -> ("api", "key")；"API_KEY" -> ("api", "key")。
    """
    camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    return tuple(re.findall(r"[a-z0-9]+", camel.lower()))


def _contains_run(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """needle 是否为 haystack 中连续的一段（词元边界匹配）。"""
    if not needle:
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i:i + len(needle)] == needle:
            return True
    return False


def _is_sensitive_key(key: Any, sensitive_keys: Sequence[str]) -> bool:
    if not isinstance(key, str):
        return False
    tokens = _tokens(key)
    if not tokens:
        return False
    # 除调用方传入的标记外，"key" 作为独立词元也视为敏感：api_key/private_key
    # 命中；monkey/keyboard/keyword 不构成独立词元，不命中，避免误伤。
    markers = [*sensitive_keys, "key"]
    for marker in markers:
        marker_tokens = _tokens(marker)
        if marker_tokens and _contains_run(tokens, marker_tokens):
            return True
    return False


def redact_secrets(text: str) -> str:
    """脱敏自由文本中的密钥形态；普通中英文内容保持原样。"""
    if not isinstance(text, str):
        return text
    out = _URL_CRED_RE.sub(lambda m: m.group(1) + _REDACTED + "@", text)
    out = _SK_RE.sub(_REDACTED, out)
    out = _AUTH_HEADER_RE.sub(
        lambda m: m.group(1) + m.group(2) + _REDACTED, out)
    out = _BEARER_RE.sub(_REDACTED, out)
    return out


def _redact_node(value: Any, sensitive_keys: Sequence[str],
                 redacted: str) -> Any:
    if isinstance(value, dict):
        return redact_dict(value, sensitive_keys=sensitive_keys,
                           redacted=redacted)
    if isinstance(value, list):
        return [_redact_node(item, sensitive_keys, redacted)
                for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def redact_dict(obj: Dict[str, Any],
                sensitive_keys: Sequence[str] = (
                    "api_key", "token", "secret", "password",
                    "authorization"),
                redacted: str = _REDACTED) -> Dict[str, Any]:
    """脱敏 dict：敏感键名对应整段值替换为 redacted，返回全新 dict。

    递归处理嵌套 dict / list；非 dict 输入原样返回。
    """
    if not isinstance(obj, dict):
        return obj
    return {
        key: (redacted if _is_sensitive_key(key, sensitive_keys)
              else _redact_node(value, sensitive_keys, redacted))
        for key, value in obj.items()
    }


def redact_value(
    obj: Any,
    sensitive_keys: Sequence[str] = (
        "api_key", "token", "secret", "password", "authorization"),
    redacted: str = _REDACTED,
) -> Any:
    """递归脱敏任意对象（顶层可为 dict / list / str / 标量）。

    规则与 redact_dict 一致：字典按敏感键名整段替换，其余字符串值再做
    文本层扫描。供调用方对结构不确定的载荷（CLI JSON 输出、错误日志
    上下文等）整体脱敏，避免泄漏真实密钥。
    """
    return _redact_node(obj, sensitive_keys, redacted)
