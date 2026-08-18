# -*- coding: utf-8 -*-
"""SQLite 持久化：用户、API Key、任务、会话、审计日志。

并发策略：
- WAL 模式（PRAGMA journal_mode=WAL）减少读写互斥；
- 单写锁串行化写事务，避免 SQLite 锁库与脏读；
- 每个方法独立 session，用完即关。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        create_engine, event)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            sessionmaker)

ADMIN_ROLE = "admin"
DEVELOPER_ROLE = "developer"
OBSERVER_ROLE = "observer"
ROLES = (ADMIN_ROLE, DEVELOPER_ROLE, OBSERVER_ROLE)
ROLE_LEVEL = {ADMIN_ROLE: 3, DEVELOPER_ROLE: 2, OBSERVER_ROLE: 1}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).isoformat() if dt else ""


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "as_" + secrets.token_urlsafe(32)


def new_id(prefix: str = "") -> str:
    return (prefix + "_" if prefix else "") + uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default=DEVELOPER_ROLE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)


class TaskRecord(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    workspace: Mapped[str] = mapped_column(Text, default="")
    config_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="queued",
                                        index=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timeout: Mapped[float] = mapped_column(default=1800.0)
    max_cost: Mapped[Optional[float]] = mapped_column(nullable=True)
    max_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime,
                                                           nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime,
                                                            nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.result_json:
            try:
                payload = json.loads(self.result_json)
            except json.JSONDecodeError:
                payload = {}
        return {
            "id": self.id,
            "user_id": self.user_id,
            "instruction": self.instruction,
            "workspace": self.workspace,
            "config_path": self.config_path,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "timeout": self.timeout,
            "max_cost": self.max_cost,
            "max_tokens": self.max_tokens,
            "cancelled": self.cancelled,
            "created_at": utc_iso(self.created_at),
            "started_at": utc_iso(self.started_at),
            "finished_at": utc_iso(self.finished_at),
            "result": payload,
        }


class SessionRecord(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"),
                                                   nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Store:
    """SQLite 存储门面。"""

    def __init__(self, db_path: str, echo: bool = False):
        self.db_path = db_path
        self._write_lock = threading.Lock()
        url = f"sqlite:///{db_path}"
        self.engine = create_engine(
            url, echo=echo, future=True,
            connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", self._set_pragma)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False,
                                    class_=Session)
        Base.metadata.create_all(self.engine)

    @staticmethod
    def _set_pragma(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    # ---------- 内部 ----------
    def _session(self) -> Session:
        return self.Session()

    def _write(self, fn):
        with self._write_lock:
            with self._session() as s:
                result = fn(s)
                s.commit()
                return result

    # ---------- 用户与 API Key ----------
    def create_user(self, name: str, role: str = DEVELOPER_ROLE
                    ) -> tuple[User, str]:
        """创建用户并签发 API Key，返回 (user, 明文 key)。"""
        if role not in ROLES:
            raise ValueError(f"非法角色: {role}")

        def _do(s: Session) -> tuple[User, str]:
            existing = s.query(User).filter(User.name == name).first()
            if existing:
                raise ValueError(f"用户已存在: {name}")
            user = User(name=name, role=role)
            s.add(user)
            s.flush()
            key = new_api_key()
            s.add(ApiKey(user_id=user.id, prefix=key[:8],
                         key_hash=hash_api_key(key)))
            return user, key

        return self._write(_do)

    def issue_api_key(self, user_id: int) -> str:
        def _do(s: Session) -> str:
            key = new_api_key()
            s.add(ApiKey(user_id=user_id, prefix=key[:8],
                         key_hash=hash_api_key(key)))
            return key

        return self._write(_do)

    def authenticate(self, api_key: str) -> Optional[User]:
        """按 API Key 哈希查用户；命中则更新 last_used_at。"""
        digest = hash_api_key(api_key.strip())

        def _do(s: Session) -> Optional[User]:
            row = s.query(ApiKey).filter(ApiKey.key_hash == digest).first()
            if row is None:
                return None
            row.last_used_at = _now()
            user = s.query(User).filter(User.id == row.user_id).first()
            return user

        with self._write_lock:
            with self._session() as s:
                user = _do(s)
                s.commit()
                return user

    def get_user(self, user_id: int) -> Optional[User]:
        with self._session() as s:
            return s.query(User).filter(User.id == user_id).first()

    def get_user_by_name(self, name: str) -> Optional[User]:
        with self._session() as s:
            return s.query(User).filter(User.name == name).first()

    def list_users(self) -> List[User]:
        with self._session() as s:
            return s.query(User).order_by(User.id).all()

    def user_count(self) -> int:
        with self._session() as s:
            return s.query(User).count()

    # ---------- 任务 ----------
    def create_task(self, user_id: int, instruction: str, workspace: str,
                    config_path: str, timeout: float,
                    max_cost: Optional[float],
                    max_tokens: Optional[int]) -> str:
        def _do(s: Session) -> str:
            task = TaskRecord(id=new_id("task"), user_id=user_id,
                              instruction=instruction, workspace=workspace,
                              config_path=config_path, timeout=timeout,
                              max_cost=max_cost, max_tokens=max_tokens,
                              status="queued")
            s.add(task)
            s.flush()
            return task.id

        return self._write(_do)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._session() as s:
            return s.query(TaskRecord).filter(
                TaskRecord.id == task_id).first()

    def list_tasks(self, user_id: Optional[int] = None,
                   limit: int = 100) -> List[TaskRecord]:
        with self._session() as s:
            q = s.query(TaskRecord)
            if user_id is not None:
                q = q.filter(TaskRecord.user_id == user_id)
            return q.order_by(TaskRecord.created_at.desc()).limit(limit).all()

    def update_task(self, task_id: str, **fields) -> Optional[TaskRecord]:
        def _do(s: Session) -> Optional[TaskRecord]:
            task = s.query(TaskRecord).filter(
                TaskRecord.id == task_id).first()
            if task is None:
                return None
            for k, v in fields.items():
                setattr(task, k, v)
            return task

        return self._write(_do)

    def mark_cancelled(self, task_id: str) -> Optional[TaskRecord]:
        return self.update_task(task_id, status="cancelled",
                                cancelled=True, finished_at=_now())

    # ---------- 会话 ----------
    def create_session(self, user_id: int, label: str = "") -> SessionRecord:
        def _do(s: Session) -> SessionRecord:
            rec = SessionRecord(id=new_id("ses"), user_id=user_id,
                                label=label)
            s.add(rec)
            s.flush()
            return rec

        return self._write(_do)

    def list_sessions(self, user_id: Optional[int] = None,
                      limit: int = 100) -> List[SessionRecord]:
        with self._session() as s:
            q = s.query(SessionRecord)
            if user_id is not None:
                q = q.filter(SessionRecord.user_id == user_id)
            return q.order_by(SessionRecord.created_at.desc()).limit(limit).all()

    # ---------- 审计 ----------
    def audit(self, user_id: Optional[int], action: str, detail: str = "") -> None:
        def _do(s: Session) -> None:
            s.add(AuditLog(user_id=user_id, action=action,
                           detail=detail[:4000]))
        self._write(_do)

    def list_audit(self, limit: int = 200) -> List[AuditLog]:
        with self._session() as s:
            return s.query(AuditLog).order_by(
                AuditLog.id.desc()).limit(limit).all()

    def close(self) -> None:
        self.engine.dispose()
