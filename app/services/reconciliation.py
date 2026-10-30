"""Expected-vs-actual reconciliation for one rider and one IST day.

Each day is broken into ledger items:
  trip:<trip_id>        expected fare (or -10 penalty / 0) vs every payout line booked for that trip
  incentive:<date>      expected Rs 150 / 0 vs daily_incentive lines
  line:<line_id>        a payout line that belongs to no trip of this rider (unexplained)

"actual" = lines in payout_lines.csv + money the desk already sent through PaySwift for the
same item (parsed from PaySwift references, so the source of truth is PaySwift itself).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from app.models import PayoutLine, Trip
from app.services import payout_engine as pe

# Claim categories understood by the scoper (produced by the interpreter)
CAT_SURGE = "surge"
CAT_INCENTIVE = "incentive"
CAT_PENALTY = "penalty"
CAT_DISTANCE = "distance"
CAT_UNPAID = "unpaid"
CAT_GENERAL = "general"
CATEGORIES = (CAT_SURGE, CAT_INCENTIVE, CAT_PENALTY, CAT_DISTANCE, CAT_UNPAID, CAT_GENERAL)


@dataclass
class LedgerItem:
    key: str
    kind: str  # trip | incentive | line
    day: date
    expected: int
    paid_lines: int
    paid_desk: int = 0
    reserved: int = 0  # amount sitting in a pending approval / in-flight payment
    trip_id: str | None = None
    status: str | None = None
    facts: dict = field(default_factory=dict)

    @property
    def actual(self) -> int:
        return self.paid_lines + self.paid_desk

    @property
    def shortfall(self) -> int:
        return self.expected - self.actual

    @property
    def finding(self) -> str:
        s = self.shortfall
        if self.kind == "incentive":
            if s > 0:
                return "incentive_missing"
            return "incentive_overpaid" if s < 0 else ("settled_by_desk" if self.paid_desk else "ok")
        if self.kind == "line":
            return "unexplained_deduction" if s > 0 else "unexplained_credit"
        if self.status == pe.COMPLETED:
            if s > 0:
                if self.paid_lines == 0 and not self.facts.get("line_count"):
                    return "trip_unpaid"
                if self.facts.get("surge_applies") and self.paid_lines == self.facts.get("expected_without_surge"):
                    return "surge_missing"
                return "trip_underpaid"
            if s < 0:
                return "trip_overpaid"
            return "settled_by_desk" if self.paid_desk else "ok"
        if self.status == pe.CANCELLED_BY_RIDER:
            if s > 0:
                return "penalty_duplicate"
            if s < 0:
                return "penalty_not_charged"
            return "settled_by_desk" if self.paid_desk else "penalty_correct"
        if self.status == pe.CANCELLED_BY_CUSTOMER:
            if s > 0:
                return "penalty_wrongly_charged"
            if s < 0:
                return "cancelled_trip_paid"
            return "settled_by_desk" if self.paid_desk else "ok"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "day": self.day.isoformat(),
            "trip_id": self.trip_id,
            "status": self.status,
            "expected": self.expected,
            "paid_lines": self.paid_lines,
            "paid_desk": self.paid_desk,
            "reserved": self.reserved,
            "shortfall": self.shortfall,
            "finding": self.finding,
            "facts": self.facts,
        }


@dataclass
class DayLedger:
    rider_id: str
    day: date
    items: list[LedgerItem]
    completed_trips: int
    rider_cancelled: int
    customer_cancelled: int

    @property
    def expected_total(self) -> int:
        return sum(i.expected for i in self.items)

    @property
    def actual_total(self) -> int:
        return sum(i.actual for i in self.items)

    @property
    def net_shortfall(self) -> int:
        return self.expected_total - self.actual_total

    def item(self, key: str) -> LedgerItem | None:
        return next((i for i in self.items if i.key == key), None)

    def summary(self) -> dict:
        return {
            "rider_id": self.rider_id,
            "day": self.day.isoformat(),
            "completed_trips": self.completed_trips,
            "rider_cancelled": self.rider_cancelled,
            "customer_cancelled": self.customer_cancelled,
            "expected_total": self.expected_total,
            "actual_total": self.actual_total,
            "net_shortfall": self.net_shortfall,
        }


def build_day_ledger(
    session: Session,
    rider_id: str,
    day: date,
    desk_paid: dict[str, int] | None = None,
    reserved: dict[str, int] | None = None,
) -> DayLedger:
    desk_paid = desk_paid or {}
    reserved = reserved or {}
    trips = list(session.scalars(
        select(Trip).where(Trip.rider_id == rider_id, Trip.ist_date == day).order_by(Trip.started_at)
    ))
    trip_ids = [t.trip_id for t in trips]
    lines = list(session.scalars(
        select(PayoutLine).where(
            PayoutLine.rider_id == rider_id,
            or_(PayoutLine.trip_id.in_(trip_ids) if trip_ids else false(), PayoutLine.payout_date == day),
        )
    ))
    rider_trip_ids = set(session.scalars(select(Trip.trip_id).where(Trip.rider_id == rider_id)))
    by_trip: dict[str, list[PayoutLine]] = defaultdict(list)
    incentive_lines: list[PayoutLine] = []
    orphan_lines: list[PayoutLine] = []
    for ln in lines:
        if ln.trip_id and ln.trip_id in trip_ids:
            by_trip[ln.trip_id].append(ln)
        elif ln.line_type == "daily_incentive" and ln.payout_date == day:
            incentive_lines.append(ln)
        elif ln.payout_date == day and (not ln.trip_id or ln.trip_id not in rider_trip_ids):
            orphan_lines.append(ln)
        # else: a line for one of the rider's trips on another day -> counted on that day

    items: list[LedgerItem] = []
    completed = rider_cancel = customer_cancel = 0
    for t in trips:
        tl = by_trip.get(t.trip_id, [])
        facts: dict = {
            "started_at": t.started_at.isoformat(),
            "line_ids": [ln.line_id for ln in tl],
            "line_count": len(tl),
            "line_types": [ln.line_type for ln in tl],
        }
        if t.data_flags:
            facts["data_flags"] = t.data_flags
        if t.status == pe.COMPLETED:
            completed += 1
            fb = pe.trip_fare(t.distance_km, t.surge_multiplier)
            facts.update(fb.as_dict())
            facts["surge_applies"] = fb.surge > 1
            facts["expected_without_surge"] = fb.total_without_surge
        elif t.status == pe.CANCELLED_BY_RIDER:
            rider_cancel += 1
            facts["penalty_lines"] = sum(1 for ln in tl if ln.line_type == "cancellation_penalty")
        else:
            customer_cancel += 1
            facts["penalty_lines"] = sum(1 for ln in tl if ln.line_type == "cancellation_penalty")
        key = f"trip:{t.trip_id}"
        items.append(LedgerItem(
            key=key, kind="trip", day=day, trip_id=t.trip_id, status=t.status,
            expected=pe.expected_trip_amount(t.status, t.distance_km, t.surge_multiplier),
            paid_lines=sum(ln.amount for ln in tl), paid_desk=desk_paid.get(key, 0),
            reserved=reserved.get(key, 0), facts=facts,
        ))

    inc_key = f"incentive:{day.isoformat()}"
    items.append(LedgerItem(
        key=inc_key, kind="incentive", day=day,
        expected=pe.daily_incentive(completed),
        paid_lines=sum(ln.amount for ln in incentive_lines),
        paid_desk=desk_paid.get(inc_key, 0), reserved=reserved.get(inc_key, 0),
        facts={"completed_trips": completed, "threshold": pe.INCENTIVE_MIN_TRIPS,
               "line_ids": [ln.line_id for ln in incentive_lines]},
    ))
    for ln in orphan_lines:
        key = f"line:{ln.line_id}"
        items.append(LedgerItem(
            key=key, kind="line", day=day, expected=0, paid_lines=ln.amount,
            paid_desk=desk_paid.get(key, 0), reserved=reserved.get(key, 0), trip_id=ln.trip_id,
            facts={"line_type": ln.line_type, "line_id": ln.line_id},
        ))
    return DayLedger(rider_id, day, items, completed, rider_cancel, customer_cancel)


@dataclass
class ScopeResult:
    day: date
    ledger: DayLedger
    scoped: list[LedgerItem]
    positive: list[LedgerItem]  # items in scope (after widening) with a shortfall
    widened: bool
    owed: int  # payable now: min(scoped positive shortfall, day net shortfall) minus reservations
    gross_shortfall: int
    capped_by_day_net: bool
    reserved_items: list[LedgerItem]

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "day_summary": self.ledger.summary(),
            "scoped_items": [i.as_dict() for i in self.scoped],
            "items_with_shortfall": [i.as_dict() for i in self.positive],
            "widened_to_whole_day": self.widened,
            "gross_shortfall": self.gross_shortfall,
            "capped_by_day_net": self.capped_by_day_net,
            "owed": self.owed,
            "reserved_items": [i.key for i in self.reserved_items],
        }


def _in_category(item: LedgerItem, categories: set[str]) -> bool:
    if not categories or CAT_GENERAL in categories:
        return True
    if item.kind == "incentive":
        return CAT_INCENTIVE in categories
    if item.kind == "line":
        return CAT_PENALTY in categories
    if item.status == pe.COMPLETED:
        if CAT_SURGE in categories and item.facts.get("surge_applies"):
            return True
        return bool(categories & {CAT_DISTANCE, CAT_UNPAID})
    return CAT_PENALTY in categories  # cancelled trips


def scope_claim(ledger: DayLedger, categories: set[str], trip_ids: set[str]) -> ScopeResult:
    """Decide which items of the day the claim is about, then what is payable.

    - If trip ids are given, the claim is about those trips (+ the day's incentive if claimed).
    - Otherwise, the claim category selects items (surge -> surge trips, incentive -> the
      incentive line, penalty -> cancelled trips, general -> the whole day).
    - If nothing in scope is short but something else that day is, widen to the whole day:
      the rider was right that the payout was wrong, just not about why.
    - Never pay more than owed: the payable amount is capped by the day's net shortfall, so
      an overpayment elsewhere that day offsets it.
    """
    if trip_ids:
        scoped = [i for i in ledger.items if i.trip_id in trip_ids and i.kind == "trip"]
        if CAT_INCENTIVE in categories:
            scoped += [i for i in ledger.items if i.kind == "incentive"]
    else:
        scoped = [i for i in ledger.items if _in_category(i, categories)]

    positive = [i for i in scoped if i.shortfall > 0]
    widened = False
    if not positive:
        others = [i for i in ledger.items if i.shortfall > 0 and i not in scoped]
        if others:
            positive, widened = others, True

    reserved_items = [i for i in positive if i.reserved > 0]
    gross = sum(i.shortfall for i in positive)
    day_net = max(0, ledger.net_shortfall)
    payable = min(gross, day_net)
    capped = payable < gross
    reserved_total = sum(min(i.reserved, i.shortfall) for i in reserved_items)
    owed = max(0, payable - reserved_total)
    return ScopeResult(
        day=ledger.day, ledger=ledger, scoped=scoped, positive=positive, widened=widened,
        owed=owed, gross_shortfall=gross, capped_by_day_net=capped, reserved_items=reserved_items,
    )


def allocate(owed: int, items: list[LedgerItem]) -> dict[str, int]:
    """Split a payable amount across items (oldest first) for the PaySwift reference."""
    out: dict[str, int] = {}
    remaining = owed
    for it in items:
        if remaining <= 0:
            break
        free = it.shortfall - min(it.reserved, it.shortfall)
        take = min(free, remaining)
        if take > 0:
            out[it.key] = take
            remaining -= take
    return out
