"""ORM 模型。业务数据规模很小，SQLite 足够。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .core.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    external_user_id: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, nullable=True
    )
    password_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    daily_quota: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiClient(Base):
    """一次登录会话（一个设备）。refresh token 只存哈希，支持按设备吊销。"""

    __tablename__ = "api_clients"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    refresh_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    scopes: Mapped[str] = mapped_column(String(128), default="tasks:read tasks:write")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_task_user_idem"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    client_task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    kind: Mapped[str] = mapped_column(String(8))
    input_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    instruction: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_output_chars: Mapped[int] = mapped_column(Integer, default=1200)

    status: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    queue_pos: Mapped[int] = mapped_column(Integer, default=0)
    instance_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    retried: Mapped[bool] = mapped_column(Boolean, default=False)

    result_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    fetched_chars: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)

    callback_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    callback_state: Mapped[str] = mapped_column(String(16), default="none")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TaskEvent(Base):
    """任务事件流，落库用于断线重连补发（Last-Event-ID / last_seq）。"""

    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_event_task_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserBinding(Base):
    """用户与实例/会话的粘性绑定；summary 用于溢出到新实例时回灌上下文。"""

    __tablename__ = "user_bindings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), primary_key=True
    )
    instance_id: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str] = mapped_column(String(64))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
