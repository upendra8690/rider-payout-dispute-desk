"""Operations actions: approve / reject approvals, resolve escalations, plus read models for the dashboard."""

from __future__ import annotations

import time
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal, rider_lock
from app.integrations.payswift import PaySwiftClient
from app.models import (
    AuditLog,
    Conversation,
    Dispute,
    Message,
    OpsItem,
    PaymentAttempt,
    PaymentTransaction,
    Rider,
    SystemMeta,
    TraceStep,
)
from app.models import dispute as ds
from app.models import ops as ops_m
from app.models.payment import TX_SUCCEEDED
from app.services.payments import PaymentService, build_desk_view
from app.services.reconciliation import allocate, build_day_ledger, scope_claim
from app.services.trace import Tracer, audit
from app.utils.timeutil import iso, ist_date, utcnow


class OpsError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def item_out(item: OpsItem) -> dict:
    return {
        "id": item.id, "rider_id": item.rider_id, "type": item.type,
        "amount": item.amount if item.type == ops_m.APPROVAL or item.amount is not None else None,
        "reason": item.reason, "created_at": iso(item.created_at),
        "reason_code": item.reason_code, "status": item.status, "dispute_id": item.dispute_id,
        "conversation_id": item.conversation_id, "decided_by": item.decided_by,
        "decision_note": item.decision_note, "decided_at": iso(item.decided_at),
    }


def dispute_out(d: Dispute) -> dict:
    return {
        "id": d.id, "rider_id": d.rider_id, "conversation_id": d.conversation_id,
        "source_message_id": d.source_message_id, "business_date": d.business_date.isoformat(),
        "dispute_date": d.dispute_date.isoformat() if d.dispute_date else None, "categories": d.categories,
        "trip_ids": d.trip_ids, "claimed_amount": d.claimed_amount, "claim_text": d.claim_text, "status": d.status,
        "expected_amount": d.expected_amount, "actual_amount": d.actual_amount, "owed_amount": d.owed_amount,
        "paid_amount": d.paid_amount, "decision": d.decision, "decision_reason": d.decision_reason,
        "findings": d.findings, "created_at": iso(d.created_at), "updated_at": iso(d.updated_at),
    }


def tx_out(tx: PaymentTransaction, attempts: list[PaymentAttempt] | None = None) -> dict:
    out = {
        "id": tx.id, "dispute_id": tx.dispute_id, "rider_id": tx.rider_id, "amount": tx.amount, "mode": tx.mode,
        "business_date": tx.business_date.isoformat(), "idempotency_key": tx.idempotency_key,
        "reference": tx.reference, "status": tx.status, "payswift_payout_id": tx.payswift_payout_id,
        "payswift_status": tx.payswift_status, "attempts": tx.attempts, "last_error": tx.last_error,
        "approved_by": tx.approved_by, "created_at": iso(tx.created_at), "updated_at": iso(tx.updated_at),
    }
    if attempts is not None:
        out["attempt_log"] = [{
            "attempt_no": a.attempt_no, "kind": a.kind, "http_status": a.http_status, "outcome": a.outcome,
            "error": a.error, "duration_ms": a.duration_ms, "at": iso(a.at), "response": a.response,
        } for a in attempts]
    return out


class OpsService:
    def __init__(self, settings: Settings, payswift: PaySwiftClient, payments: PaymentService):
        self.settings = settings
        self.payswift = payswift
        self.payments = payments

    # ------------------------------------------------------------------ queue
    def pending(self, s: Session) -> list[dict]:
        items = s.scalars(select(OpsItem).where(OpsItem.status == ops_m.OPS_PENDING).order_by(OpsItem.created_at))
        return [item_out(i) for i in items]

    def items(self, s: Session, status: str | None = None, rider_id: str | None = None) -> list[dict]:
        q = select(OpsItem).order_by(OpsItem.created_at.desc())
        if status and status != "all":
            q = q.where(OpsItem.status == status)
        if rider_id:
            q = q.where(OpsItem.rider_id == rider_id)
        return [item_out(i) for i in s.scalars(q.limit(500))]

    def _load_pending(self, s: Session, item_id: str, expected_type: str) -> OpsItem:
        item = s.get(OpsItem, item_id)
        if item is None:
            raise OpsError(404, "ops item not found")
        if item.type != expected_type:
            raise OpsError(409, f"item is an {item.type}, not an {expected_type}")
        if item.status != ops_m.OPS_PENDING:
            raise OpsError(409, f"item already {item.status}")
        return item

    # ------------------------------------------------------------------ approve
    def approve(self, item_id: str, actor: str, note: str | None) -> dict:
        with SessionLocal() as s:
            rider_id = self._load_pending(s, item_id, ops_m.APPROVAL).rider_id
        with rider_lock(rider_id):
            s = SessionLocal()
            try:
                item = self._load_pending(s, item_id, ops_m.APPROVAL)
                dispute = s.get(Dispute, item.dispute_id) if item.dispute_id else None
                if dispute is None or dispute.dispute_date is None:
                    raise OpsError(409, "approval has no dispute to pay")
                tracer = Tracer(s, rider_id, dispute.conversation_id, None)
                desk = build_desk_view(s, self.payswift, rider_id)
                tracer.tool("payswift.list_payouts", {"rider_id": rider_id, "for": "ops_approval"}, desk.summary())
                if not desk.verified:
                    raise OpsError(503, "PaySwift cannot be read right now, so the payout cannot be reconciled. Try again.")
                for k, v in (item.context or {}).get("items", {}).items():  # don't count this approval against itself
                    desk.reserved_by_item[k] = max(0, desk.reserved_by_item.get(k, 0) - int(v))
                claim = (dispute.findings or {}).get("claim", {})
                whole_day = "general" in (dispute.categories or []) and claim.get("explicit_date")
                ledger = build_day_ledger(s, rider_id, dispute.dispute_date, desk.paid_by_item, desk.reserved_by_item)
                scope = scope_claim(ledger, set(dispute.categories or []),
                                    set() if whole_day else set(dispute.trip_ids or []))
                amount = min(item.amount or 0, scope.owed)
                tracer.decision("ops_approval_reverify", {"ops_item_id": item.id, "approved_amount": item.amount,
                                                          "actor": actor},
                                {"owed_now": scope.owed, "will_pay": amount, "verification": scope.as_dict()})
                item.status = ops_m.OPS_APPROVED
                item.decided_by, item.decision_note, item.decided_at = actor, note, utcnow()
                audit(s, f"ops:{actor}", "approval_approved", "ops_item", item.id, rider_id,
                      {"requested": item.amount, "paying": amount}, note)
                if amount <= 0:
                    dispute.status = ds.ALREADY_PAID
                    tracer.decision("ops_approved", {"ops_item_id": item.id, "actor": actor},
                                    {"paid": 0, "reason": "nothing owed any more (already paid)"})
                    s.commit()
                    return {"item": item_out(item), "paid": 0, "transaction": None,
                            "message": "Approved, but nothing is owed any more; no payment made."}
                tx = self.payments.create_transaction(
                    s, dispute=dispute, amount=amount, items=allocate(amount, scope.positive), mode="approved",
                    business_date=ist_date(utcnow()), approved_by=actor,
                )
                dispute.status = ds.PAYMENT_PROCESSING
                tracer.decision("ops_approved", {"ops_item_id": item.id, "actor": actor, "note": note},
                                {"transaction_id": tx.id, "amount": amount})
                s.commit()
                tx_id = tx.id
            finally:
                s.close()
            final = self.payments.execute(tx_id, deadline=time.monotonic() + 20, attempt_timeout=12)
        with SessionLocal() as s:
            item = s.get(OpsItem, item_id)
            return {"item": item_out(item), "paid": amount if final and final.status == TX_SUCCEEDED else 0,
                    "transaction": tx_out(final) if final else None,
                    "message": "Paid via PaySwift." if final and final.status == TX_SUCCEEDED
                    else "Approved; payment is being retried in the background."}

    # ------------------------------------------------------------------ reject / resolve
    def reject(self, item_id: str, actor: str, note: str | None) -> dict:
        with SessionLocal() as s:
            item = self._load_pending(s, item_id, ops_m.APPROVAL)
            item.status = ops_m.OPS_REJECTED
            item.decided_by, item.decision_note, item.decided_at = actor, note, utcnow()
            dispute = s.get(Dispute, item.dispute_id) if item.dispute_id else None
            if dispute:
                dispute.status = ds.REJECTED
            Tracer(s, item.rider_id, item.conversation_id, None, autocommit=False).decision(
                "ops_rejected", {"ops_item_id": item.id, "actor": actor, "note": note}, {"amount": item.amount})
            audit(s, f"ops:{actor}", "approval_rejected", "ops_item", item.id, item.rider_id, {"amount": item.amount}, note)
            s.commit()
            return {"item": item_out(item)}

    def resolve(self, item_id: str, actor: str, note: str | None) -> dict:
        with SessionLocal() as s:
            item = self._load_pending(s, item_id, ops_m.ESCALATION)
            item.status = ops_m.OPS_RESOLVED
            item.decided_by, item.decision_note, item.decided_at = actor, note, utcnow()
            Tracer(s, item.rider_id, item.conversation_id, None, autocommit=False).decision(
                "ops_resolved_escalation", {"ops_item_id": item.id, "actor": actor, "note": note},
                {"reason_code": item.reason_code})
            audit(s, f"ops:{actor}", "escalation_resolved", "ops_item", item.id, item.rider_id,
                  {"reason_code": item.reason_code}, note)
            s.commit()
            return {"item": item_out(item)}

    # ------------------------------------------------------------------ dashboard read models
    def stats(self, s: Session) -> dict:
        by_status = dict(s.execute(select(Dispute.status, func.count()).group_by(Dispute.status)).all())
        auto_resolved_statuses = (ds.AUTO_PAID, ds.NO_DISCREPANCY, ds.ALREADY_PAID, ds.OUT_OF_WINDOW, ds.NOT_FOUND)
        pend = list(s.scalars(select(OpsItem).where(OpsItem.status == ops_m.OPS_PENDING)))
        succeeded = s.execute(select(func.count(), func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
            PaymentTransaction.status == TX_SUCCEEDED)).one()
        by_mode = dict(s.execute(select(PaymentTransaction.mode, func.coalesce(func.sum(PaymentTransaction.amount), 0))
                                 .where(PaymentTransaction.status == TX_SUCCEEDED).group_by(PaymentTransaction.mode)).all())
        tx_status = dict(s.execute(select(PaymentTransaction.status, func.count()).group_by(PaymentTransaction.status)).all())
        try:
            live = self.payswift.list_payouts(None)
            payswift = {"reachable": True, "count": len(live), "total": sum(int(p.get("amount", 0)) for p in live)}
        except Exception as exc:
            payswift = {"reachable": False, "error": str(exc)[:200]}
        meta = s.get(SystemMeta, "seed")
        return {
            "disputes_total": sum(by_status.values()),
            "disputes_by_status": by_status,
            "auto_resolved": sum(by_status.get(k, 0) for k in auto_resolved_statuses),
            "auto_paid": by_status.get(ds.AUTO_PAID, 0),
            "pending_approvals": sum(1 for i in pend if i.type == ops_m.APPROVAL),
            "pending_approval_amount": sum(i.amount or 0 for i in pend if i.type == ops_m.APPROVAL),
            "escalations_open": sum(1 for i in pend if i.type == ops_m.ESCALATION),
            "payouts_processed_count": succeeded[0],
            "payouts_processed_amount": int(succeeded[1]),
            "payouts_by_mode": {k: int(v) for k, v in by_mode.items()},
            "transactions_by_status": tx_status,
            "conversations": s.scalar(select(func.count()).select_from(Conversation)),
            "messages_in": s.scalar(select(func.count()).select_from(Message).where(Message.direction == "in")),
            "payswift": payswift,
            "data_quality": (meta.value or {}).get("data_quality") if meta else None,
            "policy": {"auto_pay_max_per_dispute": self.settings.auto_pay_max_per_dispute,
                       "auto_pay_max_per_rider_per_day": self.settings.auto_pay_max_per_rider_per_day,
                       "dispute_window_days": self.settings.dispute_window_days,
                       "llm_provider": self.settings.llm_provider if self.settings.llm_enabled else "none (rule parser)"},
        }

    def conversations(self, s: Session) -> list[dict]:
        convs = list(s.scalars(select(Conversation).order_by(Conversation.last_message_at.desc()).limit(300)))
        names = dict(s.execute(select(Rider.rider_id, Rider.name)).all())
        out = []
        for c in convs:
            last = s.scalar(select(Message).where(Message.conversation_id == c.id).order_by(Message.id.desc()).limit(1))
            n_disp = s.scalar(select(func.count()).select_from(Dispute).where(Dispute.conversation_id == c.id))
            open_items = s.scalar(select(func.count()).select_from(OpsItem).where(
                OpsItem.conversation_id == c.id, OpsItem.status == ops_m.OPS_PENDING))
            out.append({
                "id": c.id, "rider_id": c.rider_id, "rider_name": names.get(c.rider_id), "status": c.status,
                "language": c.language, "started_at": iso(c.started_at), "last_message_at": iso(c.last_message_at),
                "last_message": last.text[:140] if last else None, "disputes": n_disp, "open_ops_items": open_items,
            })
        return out

    def rider_detail(self, s: Session, rider_id: str) -> dict:
        rider = s.get(Rider, rider_id)
        msgs = list(s.scalars(select(Message).where(Message.rider_id == rider_id).order_by(Message.id)))
        disputes = list(s.scalars(select(Dispute).where(Dispute.rider_id == rider_id).order_by(Dispute.created_at)))
        txs = list(s.scalars(select(PaymentTransaction).where(PaymentTransaction.rider_id == rider_id)
                             .order_by(PaymentTransaction.created_at)))
        attempts: dict[str, list[PaymentAttempt]] = {}
        for a in s.scalars(select(PaymentAttempt).where(PaymentAttempt.transaction_id.in_([t.id for t in txs]))
                           .order_by(PaymentAttempt.id)):
            attempts.setdefault(a.transaction_id, []).append(a)
        audit_rows = list(s.scalars(select(AuditLog).where(AuditLog.rider_id == rider_id).order_by(AuditLog.id)))
        try:
            live = self.payswift.list_payouts(rider_id)
        except Exception:
            live = None
        return {
            "rider": {"rider_id": rider.rider_id, "name": rider.name, "city": rider.city} if rider else None,
            "messages": [{
                "id": m.id, "direction": m.direction, "text": m.text, "message_id": m.external_id,
                "conversation_id": m.conversation_id, "received_at": iso(m.received_at), "created_at": iso(m.created_at),
                "delivery_count": m.delivery_count, "processing_status": m.processing_status,
            } for m in msgs],
            "disputes": [dispute_out(d) for d in disputes],
            "payments": [tx_out(t, attempts.get(t.id, [])) for t in txs],
            "ops_items": self.items(s, status="all", rider_id=rider_id),
            "payswift_payouts": live,
            "audit": [{"at": iso(a.at), "actor": a.actor, "action": a.action, "entity_type": a.entity_type,
                       "entity_id": a.entity_id, "details": a.details, "note": a.note} for a in audit_rows],
            "trace": trace_for(s, rider_id),
        }

    def disputes(self, s: Session, status: str | None = None) -> list[dict]:
        q = select(Dispute).order_by(Dispute.created_at.desc())
        if status and status != "all":
            q = q.where(Dispute.status == status)
        return [dispute_out(d) for d in s.scalars(q.limit(500))]

    def payments_list(self, s: Session, rider_id: str | None = None) -> list[dict]:
        q = select(PaymentTransaction).order_by(PaymentTransaction.created_at.desc())
        if rider_id:
            q = q.where(PaymentTransaction.rider_id == rider_id)
        txs = list(s.scalars(q.limit(500)))
        attempts: dict[str, list[PaymentAttempt]] = {}
        if txs:
            for a in s.scalars(select(PaymentAttempt).where(PaymentAttempt.transaction_id.in_([t.id for t in txs]))
                               .order_by(PaymentAttempt.id)):
                attempts.setdefault(a.transaction_id, []).append(a)
        return [tx_out(t, attempts.get(t.id, [])) for t in txs]

    def audit_log(self, s: Session, rider_id: str | None = None, limit: int = 300) -> list[dict]:
        q = select(AuditLog).order_by(AuditLog.id.desc())
        if rider_id:
            q = q.where(AuditLog.rider_id == rider_id)
        return [{"at": iso(a.at), "actor": a.actor, "action": a.action, "entity_type": a.entity_type,
                 "entity_id": a.entity_id, "rider_id": a.rider_id, "details": a.details, "note": a.note}
                for a in s.scalars(q.limit(limit))]


def trace_for(s: Session, rider_id: str) -> list[dict]:
    rows = s.scalars(select(TraceStep).where(TraceStep.rider_id == rider_id).order_by(TraceStep.id))
    return [{"at": iso(r.at), "type": r.type, "name": r.name, "input": r.input, "output": r.output,
             "message_id": r.message_id, "conversation_id": r.conversation_id} for r in rows]


def today_ist() -> date:
    return ist_date(utcnow())
