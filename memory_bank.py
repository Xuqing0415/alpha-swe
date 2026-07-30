"""第一关：Long-Term Memory 实现（记忆持久化）
使用 SQLite 存储关键实体（文件路径、类名等），在后续 Prompt 中注入压缩摘要。
"""
import json
import sqlite3
import time
import logging
from typing import Optional, List, Dict
from datetime import datetime

logger = logging.getLogger("alpha-swe.memory")


class MemoryBank:
    """持久化记忆库——存储和检索关键实体"""

    def __init__(self, db_path: str = "memory.db", max_entities: int = 1000):
        self.db_path = db_path
        self.max_entities = max_entities
        self._pending_entities: List[dict] = []  # 批量写入缓冲
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        conn.commit()
        conn.close()

    def _ensure_tables(self, conn):
        """确保表存在"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                name TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                access_count INTEGER DEFAULT 1,
                last_access TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context TEXT NOT NULL,
                token_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(entity_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name)")

    def add_entity(self, entity_type: str, name: str, metadata: dict = None):
        """添加实体到记忆"""
        entry = {
            "type": entity_type,
            "name": name,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "timestamp": datetime.now().isoformat()
        }
        self._pending_entities.append(entry)

        # 批量写入（每 50 条或即时）
        if len(self._pending_entities) >= 50:
            self._flush()

    def _flush(self):
        """批量写入待处理实体"""
        if not self._pending_entities:
            return
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)  # 确保表存在（:memory: 模式需要）
        for e in self._pending_entities:
            conn.execute(
                """INSERT OR REPLACE INTO entities (entity_type, name, metadata, last_access)
                   VALUES (?, ?, ?, ?)""",
                (e["type"], e["name"], e["metadata"], e["timestamp"])
            )
        conn.commit()
        conn.close()
        logger.debug(f"刷新 {len(self._pending_entities)} 条记忆")
        self._pending_entities.clear()

    def persist(self):
        """强制持久化所有缓冲"""
        self._flush()

    def get_context(self, query: str, limit: int = 10) -> str:
        """检索相关记忆，返回压缩摘要"""
        self._flush()
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)

        # 基于关键词匹配
        keywords = query.split()
        conditions = " OR ".join(["name LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]

        if conditions:
            rows = conn.execute(
                f"""SELECT entity_type, name, metadata, access_count
                    FROM entities WHERE {conditions}
                    ORDER BY access_count DESC, last_access DESC
                    LIMIT ?""",
                params + [limit]
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT entity_type, name, metadata, access_count
                   FROM entities ORDER BY access_count DESC, last_access DESC LIMIT ?""",
                (limit,)
            ).fetchall()

        conn.close()

        if not rows:
            return ""

        # 更新访问计数
        self._increment_access([r[1] for r in rows])

        # 构建压缩摘要
        lines = ["[记忆摘要] 以下为历史关键实体:"]
        entity_types = {}
        for etype, name, meta, count in rows:
            entity_types.setdefault(etype, []).append(name)

        for etype, names in entity_types.items():
            lines.append(f"- {etype}: {', '.join(names[:5])}")

        return "\n".join(lines)

    def _increment_access(self, names: List[str]):
        """增加访问计数"""
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        for name in names:
            conn.execute(
                "UPDATE entities SET access_count = access_count + 1, last_access = ? WHERE name = ?",
                (datetime.now().isoformat(), name)
            )
        conn.commit()
        conn.close()

    def compact(self) -> str:
        """触发生成压缩快照（当对话轮次 > 5 时触发）"""
        self._flush()
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)

        # 获取所有实体
        rows = conn.execute(
            "SELECT entity_type, name, metadata FROM entities ORDER BY access_count DESC LIMIT 50"
        ).fetchall()

        if not rows:
            conn.close()
            return ""

        # 按类型分组
        grouped: Dict[str, List[str]] = {}
        for etype, name, _ in rows:
            grouped.setdefault(etype, []).append(name)

        # 生成摘要
        summary_parts = ["[长期记忆压缩]"]
        for etype, names in grouped.items():
            summary_parts.append(f"{etype}: {', '.join(names[:8])}")

        summary = "\n".join(summary_parts)
        token_est = len(summary) // 2

        # 保存快照
        conn.execute(
            "INSERT INTO memory_snapshots (context, token_count) VALUES (?, ?)",
            (summary, token_est)
        )
        conn.commit()
        conn.close()

        logger.info(f"记忆压缩完成: {len(rows)} 实体 -> {token_est} tokens")
        return summary

    def get_stats(self) -> dict:
        """获取记忆统计"""
        self._flush()
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        type_counts = conn.execute(
            "SELECT entity_type, COUNT(*) FROM entities GROUP BY entity_type"
        ).fetchall()
        snapshots = conn.execute("SELECT COUNT(*) FROM memory_snapshots").fetchone()[0]
        conn.close()
        return {
            "total_entities": total,
            "type_distribution": dict(type_counts),
            "snapshots": snapshots
        }