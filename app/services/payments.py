"""Moving money through PaySwift safely.

Guarantees:
  * One dispute -> at most one transaction per mode, enforced by a deterministic, UNIQUE
    idempotency key (qd-<dispute>-<mode>). Every retry of that transaction re-sends the same key,
    so PaySwift replays the original payout instead of creating a second one.
  * Before re-trying an attempt whose outcome is unknown (timeout / 504 / lost response) we read
    PaySwift and look for our transaction id in the payout reference.
  * Every attempt and its result is stored in payment_attempts and in the rider's trace.
  * Work that does not finish inside the webhook latency budget is finished by a background worker.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.integrations import payswift as ps
from app.models import Dispute, OpsItem, PaymentAttempt, PaymentTransaction
from app.models import dispute as ds
from app.models import ops as ops_m
from app.models.payment import (
    OPEN_TX_STATUSES,
    TX_FAILED,
    TX_PENDING,
    TX_RETRYING,
    TX_SUCCEEDED,
    TX_UNKNOWN,
)
from app.services.trace import Tracer, audit
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger(__name__)

REF_PREFIX = "QD-DESK"


def build_reference(tx_id: str, dispute_id: str, mode: str, business_date: date, items: dict[str, int]) -> str:
    items_s = ";".join(f"{k}={v}" for k, v in items.items())
    return f"{REF_PREFIX}|tx={tx_id}|d={dispute_id}|m={mode}|bd={business_date.isoformat()}|i={items_s}"


def parse_reference(reference: str | None) -> dict | None:
    if not reference or not reference.startswith(REF_PREFIX + "|"):
        return None
    out: dict = {"items": {}}
    for part in reference.split("|")[1:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "i":
            for it in filter(None, v.split(";")):
                key, _, amt = it.rpartition("=")
                try:
                    out["items"][key] = out["items"].get(key, 0) + int(amt)
                except ValueError:
                    continue
        else:
            out[k] = v
    return out if "tx" in out else None


@dataclass
class DeskView:
    """What the desk has already paid / reserved for a rider, reconciled against PaySwift."""

    verified: bool
    payswift_payouts: list[dict] = field(default_factory=list)
    paid_by_item: dict[str, int] = field(default_factory=dict)
    paid_refs_by_item: dict[str, list[str]] = field(default_factory=dict)
    reserved_by_item: dict[str, int] = field(default_factory=dict)
    reserved_detail: dict[str, list[dict]] = field(default_factory=dict)
    auto_tx_by_day: dict[str, set] = field(default_factory=dict)
    anomalies: list[dict] = field(default_factory=list)
    error: str | None = None

    def auto_payments_on(self, day: date) -> int:
        return len(self.auto_tx_by_day.get(day.isoformat(), set()))

    def summary(self) -> dict:
        return {
            "payswift_verified": self.verified,
            "payswift_payout_count": len(self.payswift_payouts),
            "payswift_total": sum(int(p.get("amount", 0)) for p in self.payswift_payouts),
            "desk_paid_by_item": self.paid_by_item,
            "reserved_by_item": self.reserved_by_item,
            "auto_payments_by_day": {k: sorted(v) for k, v in self.auto_tx_by_day.items()},
            "anomalies": self.anomalies,
            "error": self.error,
        }


def _add(d: dict[str, int], key: str, amount: int) -> None:
    d[key] = d.get(key, 0) + amount


def build_desk_view(session: Session, client: ps.PaySwiftClient, rider_id: str) -> DeskView:
    try:
        payouts = client.list_payouts(rider_id)
        view = DeskView(verified=True, payswift_payouts=payouts)
    except ps.PaySwiftError as exc:
        payouts = []
        view = DeskView(verified=False, error=str(exc))

    seen_tx: set[str] = set()
    for p in payouts:
        ref = parse_reference(p.get("reference"))
        if not ref:
            continue
        seen_tx.add(ref["tx"])
        for k, v in ref["items"].items():
            _add(view.paid_by_item, k, v)
            view.paid_refs_by_item.setdefault(k, []).append(p.get("payout_id", "?"))
        if ref.get("m") == "auto" and ref.get("bd"):
            view.auto_tx_by_day.setdefault(ref["bd"], set()).add(ref["tx"])

    txs = list(session.scalars(select(PaymentTransaction).where(PaymentTransaction.rider_id == rider_id)))
    for tx in txs:
        ref = parse_reference(tx.reference) or {"items": {}}
        if tx.status == TX_SUCCEEDED and tx.id not in seen_tx:
            # Our DB says paid but PaySwift does not show it. Never pay twice: count it as paid
            # and surface the mismatch for ops.
            for k, v in ref["items"].items():
                _add(view.paid_by_item, k, v)
            if view.verified:
                view.anomalies.append({"type": "succeeded_tx_missing_in_payswift", "tx": tx.id,
                                       "payout_id": tx.payswift_payout_id, "amount": tx.amount})
        elif tx.status in OPEN_TX_STATUSES and tx.id not in seen_tx:
            for k, v in ref["items"].items():
                _add(view.reserved_by_item, k, v)
                view.reserved_detail.setdefault(k, []).append(
                    {"source": "payment", "id": tx.id, "amount": v, "status": tx.status})
        if tx.mode == "auto" and tx.status != TX_FAILED:
            view.auto_tx_by_day.setdefault(tx.business_date.isoformat(), set()).add(tx.id)

    pending = session.scalars(select(OpsItem).where(
        OpsItem.rider_id == rider_id, OpsItem.type == ops_m.APPROVAL, OpsItem.status == ops_m.OPS_PENDING))
    for item in pending:
        for k, v in (item.context or {}).get("items", {}).items():
            _add(view.reserved_by_item, k, int(v))
            view.reserved_detail.setdefault(k, []).append(
                {"source": "approval", "id": item.id, "amount": int(v), "status": item.status})
    return view


class PaymentService:
    def __init__(self, client: ps.PaySwiftClient, settings: Settings):
        self.client = client
        self.settings = settings

    # ------------------------------------------------------------------ creation
    def create_transaction(
        self, session: Session, *, dispute: Dispute, amount: int, items: dict[str, int], mode: str,
        business_date: date, approved_by: str | None = None,
    ) -> PaymentTransaction:
        if amount <= 0 or amount > 10000:
            raise ValueError(f"invalid payout amount {amount}")
        if sum(items.values()) != amount:
            raise ValueError("item allocation does not add up to the amount")
        tx_id = new_id("TX")
        tx = PaymentTransaction(
            id=tx_id, dispute_id=dispute.id, rider_id=dispute.rider_id, amount=amount, mode=mode,
            business_date=business_date, idempotency_key=f"qd-{dispute.id}-{mode}",
            reference=build_reference(tx_id, dispute.id, mode, business_date, items),
            status=TX_PENDING, attempts=0, approved_by=approved_by, next_attempt_at=utcnow(),
        )
        session.add(tx)
        session.flush()
        audit(session, approved_by or "agent", "payment_created", "payment", tx.id, dispute.rider_id,
              {"amount": amount, "mode": mode, "items": items, "idempotency_key": tx.idempotency_key})
        return tx

    # ------------------------------------------------------------------ execution
    def _acquire(self, session: Session, tx_id: str, seconds: int = 45) -> bool:
        row = session.execute(text(
            "UPDATE payment_transactions SET lease_until = now() + make_interval(secs => :s) "
            "WHERE id = :id AND status IN ('pending','retrying','unknown') "
            "AND (lease_until IS NULL OR lease_until < now()) RETURNING id"
        ), {"id": tx_id, "s": seconds}).first()
        session.commit()
        return row is not None

    def _release(self, session: Session, tx_id: str) -> None:
        session.execute(text("UPDATE payment_transactions SET lease_until = NULL WHERE id = :id"), {"id": tx_id})
        session.commit()

    def _record(self, session: Session, tx: PaymentTransaction, kind: str, outcome: str, *,
                request: dict | None = None, http_status: int | None = None, response: dict | None = None,
                error: str | None = None, duration_ms: int | None = None) -> None:
        session.add(PaymentAttempt(
            transaction_id=tx.id, attempt_no=tx.attempts, kind=kind, request=request,
            http_status=http_status, response=response, outcome=outcome, error=error, duration_ms=duration_ms,
        ))

    def _tracer(self, session: Session, tx: PaymentTransaction) -> Tracer:
        dispute = session.get(Dispute, tx.dispute_id)
        return Tracer(session, tx.rider_id, dispute.conversation_id if dispute else None,
                      dispute.source_message_id if dispute else None, autocommit=False)

    def _succeed(self, session: Session, tx: PaymentTransaction, payout: dict, via: str) -> None:
        tx.status = TX_SUCCEEDED
        tx.payswift_payout_id = payout.get("payout_id")
        tx.payswift_status = payout.get("status")
        tx.last_error = None
        tx.next_attempt_at = None
        dispute = session.get(Dispute, tx.dispute_id)
        if dispute:
            dispute.paid_amount = (dispute.paid_amount or 0) + tx.amount
            dispute.status = ds.AUTO_PAID if tx.mode == "auto" else ds.APPROVED_PAID
        self._tracer(session, tx).decision(
            "payment_confirmed", {"transaction_id": tx.id, "via": via},
            {"payout_id": tx.payswift_payout_id, "status": tx.payswift_status, "amount": tx.amount},
        )
        audit(session, "payment-service", "payment_succeeded", "payment", tx.id, tx.rider_id,
              {"payout_id": tx.payswift_payout_id, "amount": tx.amount, "via": via})

    def _fail(self, session: Session, tx: PaymentTransaction, error: str) -> None:
        tx.status = TX_FAILED
        tx.last_error = error
        tx.next_attempt_at = None
        dispute = session.get(Dispute, tx.dispute_id)
        if dispute:
            dispute.status = ds.PAYMENT_FAILED
        ref = parse_reference(tx.reference) or {"items": {}}
        session.add(OpsItem(
            id=new_id("OPS"), rider_id=tx.rider_id, type=ops_m.ESCALATION, amount=tx.amount,
            reason=f"PaySwift payout of Rs {tx.amount} failed permanently ({error}). Check and pay manually.",
            reason_code="payment_failed", status=ops_m.OPS_PENDING, dispute_id=tx.dispute_id,
            conversation_id=dispute.conversation_id if dispute else None,
            context={"transaction_id": tx.id, "items": ref["items"]},
        ))
        self._tracer(session, tx).error("payment_failed", {"transaction_id": tx.id}, {"error": error})
        audit(session, "payment-service", "payment_failed", "payment", tx.id, tx.rider_id, {"error": error})
        log.error("payment failed permanently", extra={"tx": tx.id, "error": error})

    def _schedule(self, tx: PaymentTransaction, delay_s: float) -> None:
        tx.next_attempt_at = utcnow() + timedelta(seconds=delay_s)

    def _reconcile(self, session: Session, tx: PaymentTransaction) -> dict | None:
        try:
            payouts = self.client.list_payouts(tx.rider_id)
        except ps.PaySwiftError as exc:
            self._record(session, tx, "reconcile", "payswift_unreadable", error=str(exc))
            return None
        match = next((p for p in payouts if (parse_reference(p.get("reference")) or {}).get("tx") == tx.id), None)
        self._record(session, tx, "reconcile", "found" if match else "not_found",
                     request={"rider_id": tx.rider_id}, response={"match": match, "payouts_seen": len(payouts)})
        return match

    def execute(self, tx_id: str, deadline: float, attempt_timeout: float | None = None) -> PaymentTransaction | None:
        """Drive one transaction until success, permanent failure, or the deadline.

        `deadline` is a time.monotonic() value. Returns the transaction's latest state.
        """
        attempt_timeout = attempt_timeout or self.settings.payswift_attempt_timeout_seconds
        session = SessionLocal()
        try:
            if not self._acquire(session, tx_id):
                return session.get(PaymentTransaction, tx_id)
            try:
                self._drive(session, tx_id, deadline, attempt_timeout)
            finally:
                self._release(session, tx_id)
            session.expire_all()
            return session.get(PaymentTransaction, tx_id)
        except Exception:
            session.rollback()
            log.exception("payment execution crashed", extra={"tx": tx_id})
            raise
        finally:
            session.close()

    def _drive(self, session: Session, tx_id: str, deadline: float, attempt_timeout: float) -> None:
        while True:
            tx = session.get(PaymentTransaction, tx_id)
            session.refresh(tx)
            if tx.status not in OPEN_TX_STATUSES:
                return
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                self._schedule(tx, 1.0)
                session.commit()
                return

            if tx.status == TX_UNKNOWN:
                found = self._reconcile(session, tx)
                if found:
                    self._succeed(session, tx, found, via="reconcile")
                    session.commit()
                    return
                session.commit()
                remaining = deadline - time.monotonic()
                if remaining < 1.0:
                    self._schedule(tx, 1.0)
                    session.commit()
                    return

            if tx.attempts >= self.settings.payment_max_attempts:
                self._fail(session, tx, f"gave up after {tx.attempts} attempts; last error: {tx.last_error}")
                session.commit()
                return

            timeout = max(0.5, min(attempt_timeout, remaining - 0.3))
            res = self.client.create_payout(tx.rider_id, tx.amount, tx.reference, tx.idempotency_key, timeout)
            tx.attempts += 1
            self._record(session, tx, "create", res.kind, request=res.request, http_status=res.http_status,
                         response=res.body, error=res.error, duration_ms=res.duration_ms)
            self._tracer(session, tx).tool(
                "payswift.create_payout",
                {"transaction_id": tx.id, "attempt": tx.attempts, "amount": tx.amount,
                 "idempotency_key": tx.idempotency_key},
                {"outcome": res.kind, "http_status": res.http_status, "body": res.body, "error": res.error,
                 "ms": res.duration_ms},
            )

            if res.succeeded:
                self._succeed(session, tx, res.body or {}, via=res.kind)
                session.commit()
                return
            tx.last_error = res.error
            if res.kind == ps.RETRYABLE:
                tx.status = TX_RETRYING
                session.commit()
                time.sleep(min(0.4 * tx.attempts, max(0.0, deadline - time.monotonic() - 1.0)))
                continue
            if res.kind in (ps.UNKNOWN, ps.IN_PROGRESS):
                tx.status = TX_UNKNOWN
                session.commit()
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic() - 1.0)))
                continue
            if res.kind == ps.RATE_LIMITED:
                tx.status = TX_RETRYING
                self._schedule(tx, 61.0)  # PaySwift blocks the account for ~60s
                session.commit()
                return
            # REJECTED / KEY_CONFLICT: a retry cannot fix it
            self._fail(session, tx, res.error or res.kind)
            session.commit()
            return

    # ------------------------------------------------------------------ worker
    def due_transaction_ids(self, limit: int = 20) -> list[str]:
        with SessionLocal() as session:
            rows = session.execute(text(
                "SELECT id FROM payment_transactions WHERE status IN ('pending','retrying','unknown') "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                "AND (lease_until IS NULL OR lease_until < now()) ORDER BY created_at LIMIT :n"
            ), {"n": limit}).all()
            return [r[0] for r in rows]

    def process_due(self) -> int:
        n = 0
        for tx_id in self.due_transaction_ids():
            try:
                self.execute(tx_id, deadline=time.monotonic() + 25.0, attempt_timeout=12.0)
                n += 1
            except Exception:  # keep the worker alive; the error is logged in execute()
                continue
        return n


class PaymentWorker(threading.Thread):
    """Finishes payments that did not complete within the webhook's latency budget."""

    def __init__(self, service: PaymentService, poll_seconds: float = 1.0):
        super().__init__(name="payment-worker", daemon=True)
        self.service = service
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("payment worker started")
        while not self._stop_event.is_set():
            try:
                self.service.process_due()
            except Exception:
                log.exception("payment worker iteration failed")
            self._stop_event.wait(self.poll_seconds)
        log.info("payment worker stopped")
