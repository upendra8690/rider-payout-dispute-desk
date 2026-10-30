"""Payment transactions sent to PaySwift and every attempt made for them."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.conversation import JSONType

# Transaction statuses
TX_PENDING = "pending"  # created, not yet attempted
TX_RETRYING = "retrying"  # a retryable failure happened, will retry
TX_UNKNOWN = "unknown"  # outcome unknown (timeout / 504); must reconcile before retrying
TX_SUCCEEDED = "succeeded"
TX_FAILED = "failed"  # permanent failure, needs ops

OPEN_TX_STATUSES = (TX_PENDING, TX_RETRYING, TX_UNKNOWN)


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # auto | approved
    business_date: Mapped[date] = mapped_column(Date)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    reference: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(16), index=True)
    payswift_payout_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payswift_status: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("payment_transactions.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # create | reconcile
    request: Mapped[dict | None] = mapped_column(JSONType)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSONType)
    outcome: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
