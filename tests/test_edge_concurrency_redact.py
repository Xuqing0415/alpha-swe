# -*- coding: utf-8 -*-
"""任务 C 边界加固用例：文件锁并发边界 + sqlite 记忆并发 + 脱敏工具。

覆盖：
- ProjectLock：同进程重复 acquire 幂等 / 第二实例明确失败（不死锁）；
  持有者刚崩溃（<5s）的残留锁可被安全接管；损坏 pid 不崩溃；
  独立子进程第二实例获取冲突被拒绝，释放后可再获取。
- SqliteMemoryStore：同一 db 文件，两个并发 asyncio 任务 remember + search，
  不抛异常、不丢条目。
- redact：sk- / Bearer / authorization 头 / URL 内嵌凭据 / 敏感键名整段值，
  普通中英文内容不被误伤。

运行：.venv/Scripts/python.exe -X utf8 -m pytest -p no:cacheprovider
      tests/test_edge_concurrency_redact.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agent.memory.store import SqliteMemoryStore
from agent.project_lock import ProjectLock
from agent.redact import redact_dict, redact_secrets

REDACTED = "***REDACTED***"


# ===================== 1. ProjectLock 边界 =====================

def test_lock_same_object_reacquire_is_idempotent(ws_tmp):
    """同一对象重复 acquire 同一目录：幂等返回 True，不挂死。"""
    lock = ProjectLock(str(ws_tmp / "proj"), holder="same-process")
    assert lock.acquire(timeout=0.0) is True
    assert lock.acquire(timeout=0.0) is True  # 设计：持有中再次获取为幂等成功
    assert lock.lock_path.exists()
    lock.release()
    lock.release()  # release 幂等
    assert not lock.lock_path.exists()


def test_lock_second_instance_same_process_rejected(ws_tmp):
    """同进程第二个锁对象获取同一目录：立即明确失败而非死锁。"""
    proj = str(ws_tmp / "proj2")
    holder = ProjectLock(proj, holder="holder-1")
    contender = ProjectLock(proj, holder="holder-2")
    assert holder.acquire(timeout=0.0) is True
    started = time.monotonic()
    assert contender.acquire(timeout=0.0) is False
    assert time.monotonic() - started < 2.0  # 明确快速失败
    assert holder.lock_path.exists()
    holder.release()
    assert contender.acquire(timeout=0.0) is True  # 释放后可正常获取
    contender.release()


def test_lock_stale_recent_crash_takeover(ws_tmp):
    """锁文件指向不存在的 pid 且创建时间很新（<5s）：仍应立即接管。"""
    proj = ws_tmp / "proj3"
    proj.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(str(proj), holder="reclaimer")
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path.write_text(json.dumps({
        "pid": 2 ** 31 - 1,                 # 不存在于任何平台的 pid
        "holder": "crashed-instance",
        "acquired_at": time.time() - 1.0,   # 刚崩溃，远未到 5s
    }), encoding="utf-8")
    assert lock.is_held_by_alive_process() is False
    assert lock.acquire(timeout=2.0) is True, "残留锁应被安全接管"
    info = json.loads(lock.lock_path.read_text(encoding="utf-8"))
    assert info["pid"] == os.getpid()
    lock.release()
    assert not lock.lock_path.exists()


def test_lock_corrupt_pid_no_crash(ws_tmp):
    """锁文件 pid 损坏（非数字）：查询/获取不抛异常，安全拒绝。"""
    proj = ws_tmp / "proj4"
    proj.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(str(proj), holder="reclaimer")
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path.write_text(json.dumps({
        "pid": "not-a-number", "holder": "ghost",
        "acquired_at": time.time() - 100.0,
    }), encoding="utf-8")
    assert lock.is_held_by_alive_process() is False  # 不抛 ValueError
    assert lock.acquire(timeout=0.0) is False        # 未达超时兜底，安全拒绝
    assert lock.lock_path.exists()                   # 未被误删


_LOCK_CHILD = textwrap.dedent("""\
    import json, os, sys
    sys.path.insert(0, {repo!r})
    from agent.project_lock import ProjectLock
    proj, timeout = sys.argv[1], float(sys.argv[2])
    lock = ProjectLock(proj, holder=f"pid-{{os.getpid()}}")
    ok = lock.acquire(timeout=timeout)
    print(json.dumps({{"acquired": ok}}))
    if ok:
        lock.release()
""")


def _run_lock_child(proj: str, timeout: float) -> dict:
    code = _LOCK_CHILD.format(repo=REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, proj, str(timeout)],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT)
    assert proc.returncode == 0, f"子进程失败: {proc.stderr[-300:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_lock_cross_process_conflict(ws_tmp):
    """第二实例（独立子进程）争同一项目锁：被拒绝；释放后可获取。"""
    proj = str(ws_tmp / "proj5")
    holder = ProjectLock(proj, holder="parent-holder")
    assert holder.acquire(timeout=0.0) is True
    assert _run_lock_child(proj, 0.0)["acquired"] is False
    holder.release()
    assert _run_lock_child(proj, 0.0)["acquired"] is True


# ================ 2. SqliteMemoryStore 同进程并发 ================

def test_sqlite_memory_two_async_tasks_no_loss(ws_tmp):
    """同一 sqlite 记忆库，两个并发 asyncio 任务 remember + search：不崩不丢。"""
    db = str(ws_tmp / "mem.db")

    async def worker(wid: int, count: int, errors: list) -> None:
        store = SqliteMemoryStore(db)
        try:
            for i in range(count):
                await asyncio.sleep(0)  # 让两个任务真正交错
                store.remember("exp", f"item-{wid}-{i:03d}", {"worker": wid})
                if i % 7 == 0:
                    store.search(f"item-{wid}", top_k=5)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            store.close()

    errors: list = []

    async def main() -> None:
        await asyncio.gather(worker(0, 30, errors), worker(1, 30, errors))

    asyncio.run(main())
    assert errors == [], f"并发任务出现异常: {errors}"

    store = SqliteMemoryStore(db)
    try:
        hits = store.search("item", top_k=1000)
        texts = {h["text"] for h in hits}
        expected = {f"item-{w}-{i:03d}" for w in (0, 1) for i in range(30)}
        assert texts == expected, f"并发写丢条目: 期望 {len(expected)}，实际 {len(texts)}"
    finally:
        store.close()

# ===================== 3. redact 脱敏工具 =====================

_SAMPLE_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
               "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")


def test_redact_sk_keys():
    """OpenAI / DeepSeek 形态的 sk- 长密钥被脱敏。"""
    openai_key = "sk-" + "aB3cD5eF7gH9jK1lM2nP4qR6sT8uV0wX2yZ4aB3cD5eF7gH"
    out = redact_secrets(f"配置 key={openai_key}，请勿外泄。")
    assert REDACTED in out and openai_key not in out
    assert out == f"配置 key={REDACTED}，请勿外泄。"

    deepseek_key = "sk-" + "Ab3dEf9gHi1Jk2Lm4Np6"
    out2 = redact_secrets(f"Authorization: Bearer {deepseek_key}")
    assert deepseek_key not in out2 and REDACTED in out2


def test_redact_bearer_jwt():
    """Bearer <JWT>（大小写、句中位置）token 被脱敏。"""
    out = redact_secrets(f"token was sent as Bearer {_SAMPLE_JWT} in the header")
    assert _SAMPLE_JWT not in out and REDACTED in out
    lower = redact_secrets(f"use bearer {_SAMPLE_JWT} here")
    assert _SAMPLE_JWT not in lower and REDACTED in lower


def test_redact_authorization_header_values():
    """行首 authorization 头的整段值（Bearer / Basic）被脱敏。"""
    jwt_line = f"Authorization: Bearer {_SAMPLE_JWT}"
    out = redact_secrets(jwt_line)
    assert _SAMPLE_JWT not in out and REDACTED in out

    basic_line = "Authorization: Basic dXNlcjpwYXNz"
    assert "dXNlcjpwYXNz" not in redact_secrets(basic_line)

    indented = f"   authorization: Bearer {_SAMPLE_JWT}"
    assert _SAMPLE_JWT not in redact_secrets(indented)


def test_redact_url_embedded_credentials():
    """https://user:pass@host 内嵌凭据被脱敏，普通 URL 不受影响。"""
    assert (redact_secrets("https://alice:s3cr3t@api.example.com/v1/list")
            == f"https://{REDACTED}@api.example.com/v1/list")
    assert (redact_secrets("参考 https://example.com/docs 文档")
            == "参考 https://example.com/docs 文档")


def test_redact_no_false_positive_plain_text():
    """普通中文/英文句子不被误伤。"""
    samples = [
        "task-2024-regression and risk-assessment are both fine",
        "Please use a Bearer token in the Authorization header when calling the API.",
        "Bearer token 是通用术语，不应被当成密钥。",
        "你好，这是普通中文内容：提到 token、password 与 secret 等概念，不包含真实密钥。",
        "配置说明：sk-123 这样的短标识不会命中长密钥规则。",
        "wsk-anchor-abcdefghijklmnop 只是普通单词。",
    ]
    for sample in samples:
        assert redact_secrets(sample) == sample, f"误伤普通文本: {sample}"


def test_redact_idempotent():
    """重复脱敏结果稳定。"""
    t = (f"key=sk-{'A' * 48} 见 https://u:p@h/x "
         f"Bearer {_SAMPLE_JWT}")
    once = redact_secrets(t)
    assert redact_secrets(once) == once


# ===================== 4. redact_dict =====================

def test_redact_dict_sensitive_keys_whole_value():
    """敏感键名对应整段值替换为脱敏串，其余键值不受影响。"""
    obj = {
        "api_key": "sk-" + "x" * 48,
        "access_token": "tok_123456",
        "password": "p@ssw0rd",
        "client_secret": "sec-ret",
        "Authorization": "Bearer " + _SAMPLE_JWT,
        "name": "张三",
        "message": "普通内容不受影响",
        "count": 42,
    }
    out = redact_dict(obj)
    assert out is not obj
    for key in ("api_key", "access_token", "password", "client_secret",
                "Authorization"):
        assert out[key] == REDACTED, key
    assert out["name"] == "张三"
    assert out["message"] == "普通内容不受影响"
    assert out["count"] == 42
    assert obj["api_key"].startswith("sk-")  # 原始对象未被就地改动


def test_redact_dict_nested_camel_and_key_tokens():
    """嵌套 dict/list、camelCase 键、private_key 词元均被处理。"""
    obj = {
        "apiKey": "c1",
        "private_key": "c2",
        "nested": {"authToken": "c3", "safe": {"note": "hi"}},
        "items": [{"token": "c4"}, {"name": "alice"}],
    }
    out = redact_dict(obj)
    assert out["apiKey"] == REDACTED
    assert out["private_key"] == REDACTED       # 独立词元 key 命中
    assert out["nested"]["authToken"] == REDACTED
    assert out["nested"]["safe"] == {"note": "hi"}
    assert out["items"] == [{"token": REDACTED}, {"name": "alice"}]


def test_redact_dict_no_false_positive_keys():
    """monkey/keyboard/keyword 等普通键名不被误伤。"""
    obj = {
        "monkey": "banana",
        "keyboard": "qwerty",
        "keyword": "text",
        "name": "李雷",
        "notes": "记录：token 是通用词，无需脱敏。",
    }
    assert redact_dict(obj) == obj


def test_redact_dict_custom_redacted_and_free_text_value():
    """自定义脱敏串生效；非敏感键的字符串值仍做文本层扫描。"""
    out = redact_dict({"token": "abc", "payload": "key=abc"},
                      redacted="[MASKED]")
    assert out == {"token": "[MASKED]", "payload": "key=abc"}
    scrubbed = redact_dict({"error": "failed: Bearer " + _SAMPLE_JWT})
    assert _SAMPLE_JWT not in scrubbed["error"]
