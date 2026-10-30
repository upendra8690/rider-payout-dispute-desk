"""Reference data exported by QuickDrop: riders, trips, payout lines."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Rider(Base):
    __tablename__ = "riders"

    rider_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    joined_on: Mapped[date | None] = mapped_column(Date)


class Trip(Base):
    __tablename__ = "trips"

    trip_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(16), ForeignKey("riders.rider_id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ist_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32))
    distance_km: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    surge_multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    # Raw values kept for auditability when normalisation changed something.
    raw_rider_id: Mapped[str] = mapped_column(String(16))
    raw_distance: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    data_flags: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (Index("ix_trips_rider_day", "rider_id", "ist_date"),)


class PayoutLine(Base):
    __tablename__ = "payout_lines"

    line_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    payout_date: Mapped[date] = mapped_column(Date, index=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    line_type: Mapped[str] = mapped_column(String(32))
    trip_id: Mapped[str | None] = mapped_column(String(16), index=True)
    amount: Mapped[int] = mapped_column(Integer)

    __table_args__ = (Index("ix_lines_rider_day", "rider_id", "payout_date"),)
