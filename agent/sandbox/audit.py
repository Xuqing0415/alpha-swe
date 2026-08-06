"""文件操作审计 —— 记录 before/after diff，支持回滚（对应设计第 5.2 节）。

每次文件写/追加操作记录 {ts, action, path, before, after, diff, task_id} 到 JSONL；
rollback(path) 用最近一次审计的 before 内容恢复原文件。
"""
from __future__ import annotations

import difflib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alpha-swe.sandbox.audit")


class FileAuditStore:
    """线程安全的文件操作审计日志（JSONL 追加写）。"""

    def __init__(self, log_dir: str = "./logs/audit"):
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    def _log_path(self) -> Path:
        return self.log_dir / "file_audit.jsonl"

    def record(self, action: str, path: str, before: Optional[str],
               after: Optional[str], task_id: str = "") -> Dict[str, Any]:
        """记录一次文件操作；before/after 为完整内容，生成 unified diff。"""
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "ts": time.time(),
            "action": action,
            "path": str(path),
            "task_id": task_id or "",
            "before": before,
            "after": after,
            "diff": self._make_diff(before, after),
        }
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self._log_path(), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("审计写入失败: %s", e)
        return record

    def find(self, path: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按路径查询最近审计记录（倒序）。"""
        rows = []
        try:
            with open(self._log_path(), encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("path") == str(path):
                        rows.append(row)
        except OSError:
            return []
        return rows[-limit:][::-1]

    def rollback(self, path: str) -> Optional[str]:
        """用最近一次审计的 before 内容恢复文件，返回恢复后的内容。"""
        rows = self.find(path)
        if not rows:
            return None
        row = rows[0]
        if row.get("before") is None:
            return None
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(row["before"], encoding="utf-8")
        except OSError as e:
            logger.warning("回滚失败: %s", e)
            return None
        return row["before"]

    @staticmethod
    def _make_diff(before: Optional[str], after: Optional[str]) -> str:
        if before is None and after is None:
            return ""
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = (after or "").splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            before_lines, after_lines,
            fromfile="before", tofile="after",
        ))


__all__ = ["FileAuditStore"]