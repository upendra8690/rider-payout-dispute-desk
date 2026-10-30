"""Load the QuickDrop CSV exports into Postgres, normalising known data-quality problems.

Problems found in the exports (and how they are handled, all recorded in system_meta):
  * rider ids written as 'R7' / 'r19' in trips.csv          -> canonical 'R007' / 'R019'
  * the same trip exported twice (identical rows)           -> de-duplicated by trip_id
  * distance_km values like 5000.0 that are really metres   -> values >= 100 divided by 1000
  * started_at is UTC; the policy "day" is the IST calendar day -> ist_date column
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import PayoutLine, Rider, SystemMeta, Trip
from app.utils.ids import normalize_rider_id
from app.utils.logging import get_logger

log = get_logger(__name__)

METRES_THRESHOLD_KM = 100  # no delivery trip is 100+ km; the largest real value is 13 km
SEED_META_KEY = "seed"


def _files_fingerprint(data_dir: Path) -> str:
    h = hashlib.sha256()
    for name in ("riders.csv", "trips.csv", "payout_lines.csv"):
        h.update((data_dir / name).read_bytes())
    return h.hexdigest()[:20]


def load_frames(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    riders = pd.read_csv(data_dir / "riders.csv", dtype=str)
    trips = pd.read_csv(data_dir / "trips.csv", dtype={"trip_id": str, "rider_id": str, "status": str})
    lines = pd.read_csv(data_dir / "payout_lines.csv", dtype={"line_id": str, "rider_id": str, "trip_id": str})

    report: dict = {}

    riders["rider_id"] = riders["rider_id"].map(normalize_rider_id)

    trips["raw_rider_id"] = trips["rider_id"].astype(str)
    trips["rider_id"] = trips["raw_rider_id"].map(normalize_rider_id)
    changed_ids = trips[trips["rider_id"] != trips["raw_rider_id"]]
    report["rider_id_normalised_rows"] = int(len(changed_ids))
    report["rider_id_variants"] = sorted(set(changed_ids["raw_rider_id"]))

    before = len(trips)
    dup_ids = sorted(set(trips.loc[trips["trip_id"].duplicated(), "trip_id"]))
    trips = trips.drop_duplicates(subset=["trip_id", "rider_id", "started_at", "status", "distance_km", "surge_multiplier"])
    conflicting = trips[trips["trip_id"].duplicated(keep=False)]
    if not conflicting.empty:  # same id, different content: keep first, flag loudly
        log.warning("conflicting duplicate trips", extra={"trip_ids": sorted(set(conflicting.trip_id))})
        trips = trips.drop_duplicates(subset=["trip_id"], keep="first")
    report["duplicate_trip_rows_removed"] = int(before - len(trips))
    report["duplicate_trip_ids"] = dup_ids

    trips["raw_distance"] = trips["distance_km"].astype(float)
    metres = trips["raw_distance"] >= METRES_THRESHOLD_KM
    trips["distance_km"] = trips["raw_distance"].where(~metres, trips["raw_distance"] / 1000.0)
    report["distance_in_metres_rows"] = int(metres.sum())
    report["distance_in_metres_riders"] = sorted(set(trips.loc[metres, "rider_id"]))

    ts = pd.to_datetime(trips["started_at"], utc=True)
    trips["started_at_ts"] = ts
    trips["ist_date"] = ts.dt.tz_convert("Asia/Kolkata").dt.date
    utc_dates = ts.dt.date
    report["trips_where_ist_day_differs_from_utc_day"] = int((utc_dates != trips["ist_date"]).sum())

    flags = []
    for raw_id, norm_id, is_m in zip(trips["raw_rider_id"], trips["rider_id"], metres, strict=False):
        f = []
        if raw_id != norm_id:
            f.append(f"rider_id_normalised_from:{raw_id}")
        if is_m:
            f.append("distance_metres_to_km")
        flags.append(";".join(f) or None)
    trips["data_flags"] = flags

    lines["raw_rider_id"] = lines["rider_id"]
    lines["rider_id"] = lines["rider_id"].map(normalize_rider_id)
    lines["trip_id"] = lines["trip_id"].where(lines["trip_id"].notna(), None)
    lines["payout_date"] = pd.to_datetime(lines["payout_date"]).dt.date

    unknown_riders = set(trips["rider_id"]) - set(riders["rider_id"])
    report["trips_with_unknown_rider"] = sorted(unknown_riders)
    report["counts"] = {"riders": int(len(riders)), "trips": int(len(trips)), "payout_lines": int(len(lines))}
    return riders, trips, lines, report


def seed_reference_data(session: Session, data_dir: Path, force: bool = False) -> dict:
    """Idempotent: reloads only if the CSVs changed (fingerprint) or force=True."""
    fingerprint = _files_fingerprint(data_dir)
    meta = session.get(SystemMeta, SEED_META_KEY)
    if meta and meta.value.get("fingerprint") == fingerprint and not force:
        return meta.value

    riders, trips, lines, report = load_frames(data_dir)

    session.execute(delete(PayoutLine))
    session.execute(delete(Trip))
    existing_riders = set(session.scalars(select(Rider.rider_id)))
    for row in riders.itertuples(index=False):
        if row.rider_id in existing_riders:
            r = session.get(Rider, row.rider_id)
            r.name, r.city = row.name, row.city
        else:
            session.add(Rider(
                rider_id=row.rider_id, name=row.name, city=row.city,
                joined_on=pd.to_datetime(row.joined_on).date() if isinstance(row.joined_on, str) else None,
            ))
    session.flush()

    session.bulk_insert_mappings(Trip, [
        {
            "trip_id": r.trip_id,
            "rider_id": r.rider_id,
            "started_at": r.started_at_ts.to_pydatetime(),
            "ist_date": r.ist_date,
            "status": r.status,
            "distance_km": Decimal(str(round(r.distance_km, 3))),
            "surge_multiplier": Decimal(str(r.surge_multiplier)),
            "raw_rider_id": r.raw_rider_id,
            "raw_distance": Decimal(str(r.raw_distance)),
            "data_flags": r.data_flags,
        }
        for r in trips.itertuples(index=False)
    ])
    session.bulk_insert_mappings(PayoutLine, [
        {
            "line_id": r.line_id,
            "payout_date": r.payout_date,
            "rider_id": r.rider_id,
            "line_type": r.line_type,
            "trip_id": r.trip_id,
            "amount": int(r.amount),
        }
        for r in lines.itertuples(index=False)
    ])

    value = {"fingerprint": fingerprint, "data_quality": report}
    if meta:
        meta.value = value
    else:
        session.add(SystemMeta(key=SEED_META_KEY, value=value))
    session.flush()
    log.info("reference data seeded", extra={"data_quality": report})
    return value
