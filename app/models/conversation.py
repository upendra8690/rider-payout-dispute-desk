"""Conversations and messages (the agent's memory)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed
    language: Mapped[str] = mapped_column(String(16), default="hinglish")
    state: Mapped[dict] = mapped_column(JSONType, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """Inbound rider messages (unique vendor id => webhook de-duplication) and our replies."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)  # wamid, inbound only
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Inbound processing lifecycle: processing -> done | failed
    processing_status: Mapped[str | None] = mapped_column(String(16))
    reply_text: Mapped[str | None] = mapped_column(Text)
    delivery_count: Mapped[int] = mapped_column(Integer, default=1)
    in_reply_to: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_messages_rider_created", "rider_id", "id"),)
