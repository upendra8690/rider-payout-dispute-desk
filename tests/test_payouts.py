"""Payout policy maths and expected-vs-actual reconciliation on the real exports."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.config import get_settings
from app.services import payout_engine as pe
from app.services.reconciliation import allocate, build_day_ledger, scope_claim
from app.services.seed import load_frames


# ----------------------------------------------------------------------------- policy maths
@pytest.mark.parametrize("km,surge,expected", [
    ("0.8", "1.0", 25),     # short trip: base only
    ("2.0", "1.0", 25),     # first 2 km are included in the base
    ("2.1", "1.0", 26),     # 25 + 0.6 = 25.6 -> 26
    ("5.5", "1.0", 46),     # 25 + 3.5*6
    ("7.4", "1.0", 57),     # 57.4 -> 57 (R026)
    ("6.0", "1.5", 74),     # (25 + 24) * 1.5 = 73.5 -> 74, 0.5 rounds up (R003)
    ("9.0", "1.5", 101),    # 100.5 -> 101 (R007)
    ("10.0", "1.2", 88),    # 87.6 -> 88 (R027)
    ("5.0", "1.5", 65),     # 64.5 -> 65 (R031 after metres->km)
    ("2.25", "1.0", 27),    # 25 + 0.25*6 = 26.5 -> 27
])
def test_trip_fare(km, surge, expected):
    assert pe.trip_fare(km, surge).total == expected


def test_surge_applies_to_whole_fare_not_only_distance():
    fb = pe.trip_fare("6", "1.5")
    assert fb.pre_surge == Decimal("49")
    assert fb.exact == Decimal("73.5")
    assert fb.total_without_surge == 49


def test_half_rupee_rounds_up_and_decimal_has_no_float_error():
    # 0.1 + 0.2 style float error would turn 26.5 into 26.499999...
    assert pe.round_rupees(Decimal("26.5")) == 27
    assert pe.round_rupees(Decimal("26.49")) == 26
    assert pe.trip_fare(2.25, 1.0).total == 27  # float inputs go through str(), not binary floats


def test_statuses_and_incentive():
    assert pe.expected_trip_amount("cancelled_by_rider", "3", "1") == -10
    assert pe.expected_trip_amount("cancelled_by_customer", "9", "1.5") == 0
    assert pe.daily_incentive(11) == 0
    assert pe.daily_incentive(12) == 150
    with pytest.raises(ValueError):
        pe.expected_trip_amount("lost", "1", "1")
    with pytest.raises(ValueError):
        pe.trip_fare("-1", "1")


# ----------------------------------------------------------------------------- data normalisation
def test_import_fixes_known_data_problems():
    riders, trips, lines, report = load_frames(get_settings().data_dir)
    assert report["rider_id_variants"] == ["R7", "r19"]
    assert report["duplicate_trip_rows_removed"] == 10
    assert trips["trip_id"].is_unique
    assert report["distance_in_metres_riders"] == ["R031", "R032", "R038", "R039", "R040"]
    assert trips["distance_km"].max() < 100
    t = trips.set_index("trip_id")
    assert t.loc["T840677", "rider_id"] == "R007"           # was 'R7'
    assert float(t.loc["T312538", "distance_km"]) == 5.0    # was 5000.0
    assert str(t.loc["T312538", "ist_date"]) == "2026-09-18"


# ----------------------------------------------------------------------------- reconciliation (real data)
CASES = [  # rider, day, categories, trip ids, expected owed, expected finding
    ("R003", "2026-09-20", {"general"}, set(), 25, "surge_missing"),
    ("R027", "2026-09-20", {"surge"}, set(), 15, "surge_missing"),
    ("R027", "2026-09-21", {"penalty"}, set(), 10, "penalty_duplicate"),
    ("R005", "2026-09-21", {"general", "incentive"}, set(), 150, "incentive_missing"),
    ("R014", "2026-09-18", {"penalty"}, set(), 10, "penalty_duplicate"),
    ("R016", "2026-09-19", {"unpaid"}, set(), 425, "trip_unpaid"),
    ("R024", "2026-09-18", {"incentive"}, set(), 150, "incentive_missing"),
    ("R007", "2026-09-19", {"surge"}, {"T840677"}, 34, "surge_missing"),
    ("R031", "2026-09-18", {"surge"}, {"T312538"}, 22, "surge_missing"),
    ("R026", "2026-09-19", {"distance"}, {"T672899"}, 32, "trip_underpaid"),
    ("R033", "2026-09-20", {"unpaid"}, {"T795007", "T206956"}, 176, "trip_unpaid"),
    ("R034", "2026-09-19", {"unpaid"}, set(), 249, "trip_unpaid"),
    ("R035", "2026-09-21", {"general", "surge"}, set(), 25, "surge_missing"),
    # correctly paid: the rider is wrong
    ("R008", "2026-09-20", {"surge"}, {"T252921"}, 0, None),
    ("R011", "2026-09-19", {"incentive"}, set(), 0, None),
    ("R025", "2026-09-19", {"incentive"}, set(), 0, None),   # 13 trips in UTC, 11 in IST
    ("R009", "2026-09-17", {"incentive"}, set(), 0, None),   # 12 rows but one is a duplicate
    ("R036", "2026-09-18", {"distance"}, {"T990831"}, 0, None),
    ("R028", "2026-09-21", {"penalty"}, {"T849302"}, 0, None),
]


@pytest.mark.parametrize("rider,day,cats,trips,owed,finding", CASES)
def test_reconciliation_matches_expected_outcomes(database, rider, day, cats, trips, owed, finding):
    from app.database import SessionLocal
    with SessionLocal() as s:
        scope = scope_claim(build_day_ledger(s, rider, date.fromisoformat(day)), cats, trips)
    assert scope.owed == owed
    if finding:
        assert {i.finding for i in scope.positive} == {finding}
    else:
        assert scope.positive == []


def test_ist_day_boundary_for_incentive(database):
    from app.database import SessionLocal
    with SessionLocal() as s:
        r024 = build_day_ledger(s, "R024", date(2026, 9, 18))
        r025 = build_day_ledger(s, "R025", date(2026, 9, 19))
    assert r024.completed_trips == 12   # only 9 if you (wrongly) group by UTC date
    assert r025.completed_trips == 11   # 13 if grouped by UTC date


def test_day_net_cap_never_pays_more_than_owed(database):
    """R036 was paid a Rs150 incentive on 18 Sep with only 11 trips: any shortfall that day is offset."""
    from app.database import SessionLocal
    with SessionLocal() as s:
        ledger = build_day_ledger(s, "R036", date(2026, 9, 18))
        assert ledger.net_shortfall == -150
        item = ledger.item("trip:T990831")
        item.paid_lines -= 20   # pretend this trip was short by 20
        scope = scope_claim(ledger, {"distance"}, {"T990831"})
    assert scope.gross_shortfall == 20
    assert scope.owed == 0 and scope.capped_by_day_net


def test_widen_to_real_problem_when_reason_is_wrong(database):
    """R001 says 'surge' for 19 Sep; the real problem is a double cancellation penalty."""
    from app.database import SessionLocal
    with SessionLocal() as s:
        scope = scope_claim(build_day_ledger(s, "R001", date(2026, 9, 19)), {"surge"}, set())
    assert scope.widened and scope.owed == 10
    assert scope.positive[0].trip_id == "T429673"


def test_desk_payments_and_reservations_reduce_what_is_owed(database):
    from app.database import SessionLocal
    with SessionLocal() as s:
        paid = scope_claim(build_day_ledger(s, "R003", date(2026, 9, 20), desk_paid={"trip:T926334": 25}),
                           {"surge"}, {"T926334"})
        partly = scope_claim(build_day_ledger(s, "R003", date(2026, 9, 20), desk_paid={"trip:T926334": 10}),
                             {"surge"}, {"T926334"})
        reserved = scope_claim(build_day_ledger(s, "R003", date(2026, 9, 20), reserved={"trip:T926334": 25}),
                               {"surge"}, {"T926334"})
    assert paid.owed == 0 and paid.positive == []
    assert partly.owed == 15
    assert reserved.owed == 0 and reserved.reserved_items


def test_allocate_splits_amount_across_items(database):
    from app.database import SessionLocal
    with SessionLocal() as s:
        scope = scope_claim(build_day_ledger(s, "R016", date(2026, 9, 19)), {"unpaid"}, set())
    alloc = allocate(scope.owed, scope.positive)
    assert sum(alloc.values()) == 425 and len(alloc) == 5
    assert sum(allocate(100, scope.positive).values()) == 100
