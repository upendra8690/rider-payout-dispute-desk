"""Payment safety: idempotency, retries, lost responses, rate limits, permanent failures, limits."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.integrations import payswift as ps
from app.models import Dispute, OpsItem, PaymentAttempt, PaymentTransaction
from app.models import dispute as ds
from app.services import authorization as authz
from app.services.payments import PaymentService, build_desk_view, build_reference, parse_reference
from app.utils.timeutil import utcnow


class FakePaySwift:
    """In-memory PaySwift with the sandbox's semantics and a script of failures for POSTs."""

    def __init__(self, script: list[str] | None = None, list_fails: bool = False):
        self.script = list(script or [])
        self.ledger: list[dict] = []
        self.idem: dict[str, tuple[str, dict]] = {}
        self.posts: list[dict] = []
        self.list_fails = list_fails
        self.lock = threading.Lock()

    def _create(self, body: dict, key: str | None) -> dict:
        p = {"payout_id": f"pout_{len(self.ledger) + 1:04d}", "rider_id": body["rider_id"], "amount": body["amount"],
             "reference": body["reference"], "status": "processed", "created_at": "2026-10-30T00:00:00Z"}
        self.ledger.append(p)
        if key:
            self.idem[key] = (repr(sorted(body.items())), p)
        return p

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if self.list_fails:
                return httpx.Response(500, json={"error": "boom"})
            rider = request.url.params.get("rider_id")
            data = [p for p in self.ledger if rider is None or p["rider_id"] == rider]
            return httpx.Response(200, json={"data": data, "count": len(data)})
        import json
        body = json.loads(request.content)
        key = request.headers.get("Idempotency-Key")
        with self.lock:
            self.posts.append({"body": body, "key": key})
            if key in self.idem:
                fp, stored = self.idem[key]
                if fp != repr(sorted(body.items())):
                    return httpx.Response(409, json={"error": "idempotency_key_reused"})
                return httpx.Response(200, json=stored)
            action = self.script.pop(0) if self.script else "ok"
            if action == "ok":
                return httpx.Response(201, json=self._create(body, key))
            if action == "503":
                return httpx.Response(503, json={"error": "service_unavailable"})
            if action == "lost":
                self._create(body, key)
                return httpx.Response(504, json={"error": "gateway_timeout"})
            if action == "timeout_created":
                self._create(body, key)
                raise httpx.ReadTimeout("slow", request=request)
            if action == "timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if action == "409":
                return httpx.Response(409, json={"error": "request_in_progress"})
            if action == "429":
                return httpx.Response(429, json={"error": "account_blocked"})
            if action == "400":
                return httpx.Response(400, json={"error": "invalid_amount"})
        raise AssertionError(action)


def make_service(fake: FakePaySwift, settings, **overrides) -> PaymentService:
    client = ps.PaySwiftClient("http://payswift.test", transport=httpx.MockTransport(fake.handler))
    return PaymentService(client, settings.model_copy(update=overrides) if overrides else settings)


def make_dispute(rider: str = "R003", owed: int = 25) -> str:
    with SessionLocal() as s:
        d = Dispute(id=f"DSP-T{time.monotonic_ns() % 10**9}", rider_id=rider, business_date=date(2026, 9, 22),
                    dispute_date=date(2026, 9, 20), categories=["surge"], trip_ids=["T926334"],
                    status=ds.PAYMENT_PROCESSING, owed_amount=owed, paid_amount=0, findings={})
        s.add(d)
        s.commit()
        return d.id


def new_tx(svc: PaymentService, dispute_id: str, amount: int = 25, mode: str = "auto") -> str:
    with SessionLocal() as s:
        d = s.get(Dispute, dispute_id)
        tx = svc.create_transaction(s, dispute=d, amount=amount, items={"trip:T926334": amount}, mode=mode,
                                    business_date=date(2026, 9, 22))
        s.commit()
        return tx.id


def run(svc: PaymentService, tx_id: str, seconds: float = 10.0) -> PaymentTransaction:
    return svc.execute(tx_id, deadline=time.monotonic() + seconds, attempt_timeout=2.0)


# ----------------------------------------------------------------------------- references
def test_reference_roundtrip():
    ref = build_reference("TX-1", "DSP-1", "auto", date(2026, 9, 22), {"trip:T1": 10, "incentive:2026-09-21": 150})
    parsed = parse_reference(ref)
    assert parsed["tx"] == "TX-1" and parsed["m"] == "auto" and parsed["bd"] == "2026-09-22"
    assert parsed["items"] == {"trip:T1": 10, "incentive:2026-09-21": 150}
    assert parse_reference("manual payout by finance") is None


# ----------------------------------------------------------------------------- failure handling
@pytest.mark.parametrize("script,posts", [
    (["503"], 2),              # not created -> retried with the same key
    (["lost"], 1),             # created but 504 -> found by reconciliation, no second POST
    (["timeout_created"], 1),  # created, response lost to our timeout -> reconciled
    (["timeout"], 2),          # not created, timed out -> reconcile finds nothing -> retry
    (["409"], 2),              # in progress -> reconcile, then retry
    (["503", "503", "lost"], 3),
])
def test_exactly_one_payout_whatever_fails(clean_db, settings, script, posts):
    fake = FakePaySwift(script)
    svc = make_service(fake, settings)
    tx = run(svc, new_tx(svc, make_dispute()))
    assert tx.status == "succeeded"
    assert len(fake.ledger) == 1 and fake.ledger[0]["amount"] == 25
    assert len(fake.posts) == posts
    assert len({p["key"] for p in fake.posts}) == 1, "every retry must reuse the idempotency key"
    with SessionLocal() as s:
        attempts = list(s.scalars(select(PaymentAttempt).where(PaymentAttempt.transaction_id == tx.id)))
        assert sum(1 for a in attempts if a.kind == "create") == posts
        d = s.get(Dispute, tx.dispute_id)
        assert d.status == ds.AUTO_PAID and d.paid_amount == 25


def test_rate_limit_backs_off_instead_of_hammering(clean_db, settings):
    fake = FakePaySwift(["429"])
    svc = make_service(fake, settings)
    tx = run(svc, new_tx(svc, make_dispute()))
    assert tx.status == "retrying" and tx.attempts == 1 and not fake.ledger
    assert tx.next_attempt_at >= utcnow() + timedelta(seconds=55)


def test_permanent_rejection_fails_and_escalates(clean_db, settings):
    fake = FakePaySwift(["400"])
    svc = make_service(fake, settings)
    tx = run(svc, new_tx(svc, make_dispute()))
    assert tx.status == "failed" and not fake.ledger
    with SessionLocal() as s:
        esc = s.scalars(select(OpsItem).where(OpsItem.reason_code == "payment_failed")).all()
        assert len(esc) == 1 and esc[0].type == "escalation" and esc[0].amount == 25
        assert s.get(Dispute, tx.dispute_id).status == ds.PAYMENT_FAILED


def test_gives_up_after_max_attempts(clean_db, settings):
    fake = FakePaySwift(["503"] * 10)
    svc = make_service(fake, settings, payment_max_attempts=3)
    tx = run(svc, new_tx(svc, make_dispute()), seconds=20)
    assert tx.status == "failed" and tx.attempts == 3 and not fake.ledger


def test_unfinished_payment_is_finished_by_worker(clean_db, settings):
    fake = FakePaySwift()
    svc = make_service(fake, settings)
    tx_id = new_tx(svc, make_dispute())
    tx = svc.execute(tx_id, deadline=time.monotonic() + 0.5)  # webhook budget already used up
    assert tx.status == "pending" and not fake.ledger
    with SessionLocal() as s:  # make it due now
        s.get(PaymentTransaction, tx_id).next_attempt_at = utcnow()
        s.commit()
    assert svc.process_due() == 1
    with SessionLocal() as s:
        assert s.get(PaymentTransaction, tx_id).status == "succeeded"
    assert len(fake.ledger) == 1


def test_one_transaction_per_dispute_is_enforced_by_the_database(clean_db, settings):
    svc = make_service(FakePaySwift(), settings)
    dispute_id = make_dispute()
    new_tx(svc, dispute_id)
    with pytest.raises(IntegrityError):
        new_tx(svc, dispute_id)


def test_concurrent_execution_pays_once(clean_db, settings):
    fake = FakePaySwift()
    svc = make_service(fake, settings)
    tx_id = new_tx(svc, make_dispute())
    threads = [threading.Thread(target=run, args=(svc, tx_id)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fake.ledger) == 1


# ----------------------------------------------------------------------------- reconciliation view
def test_desk_view_reads_payswift_as_source_of_truth(clean_db, settings):
    fake = FakePaySwift()
    fake.ledger.append({"payout_id": "pout_x", "rider_id": "R003", "amount": 25, "status": "processed",
                        "reference": build_reference("TX-OLD", "DSP-OLD", "auto", date(2026, 9, 22), {"trip:T926334": 25})})
    fake.ledger.append({"payout_id": "pout_y", "rider_id": "R003", "amount": 99, "status": "processed",
                        "reference": "manual payout by finance"})
    svc = make_service(fake, settings)
    with SessionLocal() as s:
        view = build_desk_view(s, svc.client, "R003")
    assert view.verified
    assert view.paid_by_item == {"trip:T926334": 25}
    assert view.auto_payments_on(date(2026, 9, 22)) == 1
    assert view.auto_payments_on(date(2026, 9, 23)) == 0


def test_desk_view_counts_db_success_missing_from_payswift_as_paid(clean_db, settings):
    fake = FakePaySwift()
    svc = make_service(fake, settings)
    tx_id = new_tx(svc, make_dispute())
    with SessionLocal() as s:
        tx = s.get(PaymentTransaction, tx_id)
        tx.status, tx.payswift_payout_id = "succeeded", "pout_lost"
        s.commit()
        view = build_desk_view(s, svc.client, "R003")
    assert view.paid_by_item == {"trip:T926334": 25}  # never pay it again
    assert view.anomalies and view.anomalies[0]["type"] == "succeeded_tx_missing_in_payswift"


def test_desk_view_reserves_open_payments_and_pending_approvals(clean_db, settings):
    svc = make_service(FakePaySwift(), settings)
    new_tx(svc, make_dispute())  # pending tx
    with SessionLocal() as s:
        s.add(OpsItem(id="OPS-T1", rider_id="R003", type="approval", amount=10, reason="r", reason_code="x",
                      status="pending", context={"items": {"trip:T999": 10}}))
        s.commit()
        view = build_desk_view(s, svc.client, "R003")
    assert view.reserved_by_item == {"trip:T926334": 25, "trip:T999": 10}


def test_desk_view_unverified_when_payswift_unreadable(clean_db, settings):
    svc = make_service(FakePaySwift(list_fails=True), settings)
    with SessionLocal() as s:
        view = build_desk_view(s, svc.client, "R003")
    assert not view.verified


# ----------------------------------------------------------------------------- authorization rules
@pytest.mark.parametrize("owed,today,repeat,verified,susp,action,code", [
    (0, 0, False, True, None, "none", "nothing_owed"),
    (200, 0, False, True, None, "auto_pay", "within_auto_limits"),
    (201, 0, False, True, None, "approval", "over_auto_limit"),
    (15, 1, False, True, None, "approval", "daily_auto_limit"),
    (15, 0, True, True, None, "approval", "repeat_dispute"),
    (15, 0, False, False, None, "approval", "payswift_unverified"),
    (15, 0, False, True, "impersonation", "escalate", "suspicious"),
])
def test_authorization_rules(settings, owed, today, repeat, verified, susp, action, code):
    d = authz.authorize(owed=owed, settings=settings, auto_payments_today=today, repeat_of_paid_items=repeat,
                        payswift_verified=verified, suspicious_reason=susp)
    assert (d.action, d.reason_code) == (action, code)


# ----------------------------------------------------------------------------- real sandbox
def test_real_sandbox_idempotency(payswift):
    client = ps.PaySwiftClient(payswift)
    first = client.create_payout("R003", 25, "ref-a", "key-1", timeout=5)
    again = client.create_payout("R003", 25, "ref-a", "key-1", timeout=5)
    conflict = client.create_payout("R003", 30, "ref-a", "key-1", timeout=5)
    invalid = client.create_payout("R003", 0, "ref-b", "key-2", timeout=5)
    assert first.kind == ps.CREATED and again.kind == ps.REPLAYED
    assert again.body["payout_id"] == first.body["payout_id"]
    assert conflict.kind == ps.KEY_CONFLICT
    assert invalid.kind == ps.REJECTED
    assert len(client.list_payouts("R003")) == 1
