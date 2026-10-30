"""Items waiting for (or decided by) the ops team: approvals and escalations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.conversation import JSONType

APPROVAL = "approval"
ESCALATION = "escalation"

OPS_PENDING = "pending"
OPS_APPROVED = "approved"
OPS_REJECTED = "rejected"
OPS_RESOLVED = "resolved"


class OpsItem(Base):
    __tablename__ = "ops_items"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    type: Mapped[str] = mapped_column(String(16))  # approval | escalation
    amount: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), index=True, default=OPS_PENDING)
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.id"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(24))
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict] = mapped_column(JSONType, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(80))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
