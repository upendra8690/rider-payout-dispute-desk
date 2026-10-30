"""The payout policy from docs/policy.md, implemented with Decimal arithmetic.

- Every completed trip pays Rs 25 base + Rs 6 per km after the first 2 km.
- A surge multiplier applies to the whole trip fare (base + distance).
- Each trip is rounded to the nearest rupee, 0.5 rounds up.
- Rs 150 daily incentive for 12 or more completed trips in an IST calendar day.
- Rs 10 penalty for every trip the rider cancels; customer cancellations earn nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

BASE_FARE = Decimal("25")
PER_KM = Decimal("6")
FREE_KM = Decimal("2")
DAILY_INCENTIVE = 150
INCENTIVE_MIN_TRIPS = 12
RIDER_CANCEL_PENALTY = 10

COMPLETED = "completed"
CANCELLED_BY_RIDER = "cancelled_by_rider"
CANCELLED_BY_CUSTOMER = "cancelled_by_customer"
TRIP_STATUSES = {COMPLETED, CANCELLED_BY_RIDER, CANCELLED_BY_CUSTOMER}


def dec_str(value: Decimal) -> str:
    """Plain decimal text without exponent: Decimal('10.000') -> '10', Decimal('5.40') -> '5.4'."""
    return format(value.normalize(), "f")


def round_rupees(value: Decimal) -> int:
    """Nearest rupee, 0.5 rounds up (ROUND_HALF_UP; amounts here are never negative)."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FareBreakdown:
    distance_km: Decimal
    surge: Decimal
    extra_km: Decimal
    distance_component: Decimal
    pre_surge: Decimal
    exact: Decimal
    total: int  # rounded
    total_without_surge: int  # what the trip pays if surge is (wrongly) ignored

    def as_dict(self) -> dict:
        return {
            "distance_km": dec_str(self.distance_km),
            "surge": dec_str(self.surge),
            "base": int(BASE_FARE),
            "extra_km": dec_str(self.extra_km),
            "distance_component": dec_str(self.distance_component),
            "pre_surge": dec_str(self.pre_surge),
            "exact": dec_str(self.exact),
            "expected": self.total,
        }


def trip_fare(distance_km: Decimal | float | str, surge: Decimal | float | str = Decimal("1")) -> FareBreakdown:
    d = Decimal(str(distance_km))
    s = Decimal(str(surge))
    if d < 0:
        raise ValueError("distance cannot be negative")
    if s <= 0:
        raise ValueError("surge multiplier must be positive")
    extra = max(Decimal("0"), d - FREE_KM)
    distance_component = PER_KM * extra
    pre_surge = BASE_FARE + distance_component
    exact = pre_surge * s
    return FareBreakdown(
        distance_km=d,
        surge=s,
        extra_km=extra,
        distance_component=distance_component,
        pre_surge=pre_surge,
        exact=exact,
        total=round_rupees(exact),
        total_without_surge=round_rupees(pre_surge),
    )


def expected_trip_amount(status: str, distance_km: Decimal | float | str, surge: Decimal | float | str) -> int:
    """Net effect of one trip on the rider's payout (penalties are negative)."""
    if status == COMPLETED:
        return trip_fare(distance_km, surge).total
    if status == CANCELLED_BY_RIDER:
        return -RIDER_CANCEL_PENALTY
    if status == CANCELLED_BY_CUSTOMER:
        return 0
    raise ValueError(f"unknown trip status: {status}")


def daily_incentive(completed_trips: int) -> int:
    return DAILY_INCENTIVE if completed_trips >= INCENTIVE_MIN_TRIPS else 0
