"""A dispute = one verified claim by a rider about one IST day (optionally specific trips)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.conversation import JSONType

# Dispute statuses
NO_DISCREPANCY = "no_discrepancy"
AUTO_PAID = "auto_paid"
PAYMENT_PROCESSING = "payment_processing"
PENDING_APPROVAL = "pending_approval"
APPROVED_PAID = "approved_paid"
REJECTED = "rejected"
ESCALATED = "escalated"
OUT_OF_WINDOW = "out_of_window"
ALREADY_PAID = "already_paid"
PAYMENT_FAILED = "payment_failed"
NOT_FOUND = "not_found"


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128))
    business_date: Mapped[date] = mapped_column(Date)  # IST day the rider complained
    dispute_date: Mapped[date | None] = mapped_column(Date)  # IST day being disputed
    categories: Mapped[list] = mapped_column(JSONType, default=list)
    trip_ids: Mapped[list] = mapped_column(JSONType, default=list)
    claimed_amount: Mapped[int | None] = mapped_column(Integer)
    claim_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    expected_amount: Mapped[int | None] = mapped_column(Integer)  # expected for the scope
    actual_amount: Mapped[int | None] = mapped_column(Integer)  # paid lines + desk payouts
    owed_amount: Mapped[int] = mapped_column(Integer, default=0)  # verified, payable
    paid_amount: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str | None] = mapped_column(String(32))  # auto_pay | approval | escalate | none
    decision_reason: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
