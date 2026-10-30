"""Agent trace (per rider, ordered) and an audit log of human / system actions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.conversation import JSONType


class TraceStep(Base):
    __tablename__ = "trace_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(String(16))
    conversation_id: Mapped[str | None] = mapped_column(String(24))
    message_id: Mapped[str | None] = mapped_column(String(128))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    type: Mapped[str] = mapped_column(String(16))  # message_in | tool_call | decision | reply | error
    name: Mapped[str] = mapped_column(String(80))
    input: Mapped[dict | list | str | None] = mapped_column(JSONType, nullable=True)
    output: Mapped[dict | list | str | None] = mapped_column(JSONType, nullable=True)

    __table_args__ = (Index("ix_trace_rider_id", "rider_id", "id"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(80))  # agent | ops:<name> | system | worker
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    rider_id: Mapped[str | None] = mapped_column(String(16), index=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    note: Mapped[str | None] = mapped_column(Text)


class SystemMeta(Base):
    __tablename__ = "system_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, default=dict)
