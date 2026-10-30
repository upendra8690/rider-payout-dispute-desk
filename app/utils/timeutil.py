"""Time helpers. Business "day" is always the calendar day in IST (docs/policy.md)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def ist_date(dt: datetime) -> date:
    return to_ist(dt).date()


def parse_received_at(value: str | datetime | None) -> datetime:
    """Vendor timestamps are ISO-8601; naive timestamps are interpreted as IST."""
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def fmt_day(d: date) -> str:
    """'20 Sep' style used in rider replies."""
    return f"{d.day} {d.strftime('%b')}"


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def window_start(today: date, window_days: int) -> date:
    return today - timedelta(days=window_days)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None
