"""The rider conversation agent.

Pipeline for every vendor webhook:
  1. de-duplicate on the vendor message id (unique constraint) -> replay the stored reply
  2. take a per-rider lock, load the conversation (memory)
  3. interpret the text (LLM or rule parser)                         -> tool_call trace
  4. security screen (prompt injection, impersonation)               -> decision trace
  5. resolve claims to IST days, reconcile each day against trips,
     payout lines and PaySwift                                       -> tool_call / decision traces
  6. authorize: auto-pay / ops approval / escalation (Python rules)  -> decision trace
  7. pay through PaySwift with an idempotency key                    -> tool_call traces
  8. reply with the verified facts                                   -> reply trace
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import replies as R
from app.agents.interpreter import Interpreter
from app.config import Settings
from app.database import SessionLocal, rider_lock
from app.integrations.payswift import PaySwiftClient
from app.models import Conversation, Dispute, Message, OpsItem, PaymentTransaction, Rider, Trip
from app.models import dispute as ds
from app.models import ops as ops_m
from app.models.payment import TX_SUCCEEDED
from app.schemas.interpretation import Claim, Interpretation
from app.schemas.messages import InboundMessage
from app.services import authorization as authz
from app.services.payments import DeskView, PaymentService, build_desk_view, parse_reference
from app.services.reconciliation import allocate, build_day_ledger, scope_claim
from app.services.trace import Tracer, audit
from app.utils.ids import new_id
from app.utils.logging import get_logger
from app.utils.timeutil import ist_date, parse_received_at

log = get_logger(__name__)

STATUS_DISPUTES = (
    ds.AUTO_PAID, ds.APPROVED_PAID, ds.PAYMENT_PROCESSING, ds.PENDING_APPROVAL, ds.REJECTED,
    ds.ESCALATED, ds.PAYMENT_FAILED,
)


@dataclass
class DayClaim:
    day: date
    categories: set[str] = field(default_factory=set)
    trip_ids: set[str] = field(default_factory=set)
    explicit_date: bool = False
    claimed_amount: int | None = None
    claimed_trip_count: int | None = None
    claimed_distance_km: float | None = None

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(), "categories": sorted(self.categories), "trip_ids": sorted(self.trip_ids),
            "explicit_date": self.explicit_date, "claimed_amount": self.claimed_amount,
            "claimed_trip_count": self.claimed_trip_count, "claimed_distance_km": self.claimed_distance_km,
        }


@dataclass
class Turn:
    """Everything needed while handling one message."""

    session: Session
    conv: Conversation
    tracer: Tracer
    msg: InboundMessage
    rider_id: str
    today: date
    lang: str
    deadline: float
    interp: Interpretation | None = None
    new_dispute_ids: list[str] = field(default_factory=list)
    escalated: bool = False


class DisputeAgent:
    def __init__(self, settings: Settings, payswift: PaySwiftClient, payments: PaymentService,
                 interpreter: Interpreter | None = None):
        self.settings = settings
        self.payswift = payswift
        self.payments = payments
        self.interpreter = interpreter or Interpreter(settings)

    # ================================================================== entry point
    def handle(self, msg: InboundMessage) -> dict:
        started = time.monotonic()
        deadline = started + self.settings.message_budget_seconds
        rider_id = msg.rider_id
        status, row_id, stored = self._claim_message(msg)
        if status == "duplicate":
            return self._replay_duplicate(msg, row_id, stored, deadline)
        try:
            with rider_lock(rider_id, timeout_seconds=20):
                reply = self._process(msg, row_id, deadline)
        except Exception as exc:  # never leave the vendor without a 2xx + a sane reply
            log.exception("message processing failed", extra={"rider_id": rider_id, "message_id": msg.message_id})
            reply = self._fail_safely(msg, row_id, exc)
        log.info("message handled", extra={"rider_id": rider_id, "message_id": msg.message_id,
                                           "ms": int((time.monotonic() - started) * 1000)})
        return {"reply": reply, "message_id": msg.message_id, "duplicate": False}

    # ================================================================== de-duplication
    def _claim_message(self, msg: InboundMessage) -> tuple[str, int, str | None]:
        with SessionLocal() as s:
            row = Message(
                external_id=msg.message_id, rider_id=msg.rider_id, direction="in", text=msg.text,
                received_at=parse_received_at(msg.received_at), processing_status="processing", delivery_count=1,
            )
            s.add(row)
            try:
                s.commit()
                return "new", row.id, None
            except IntegrityError:
                s.rollback()
            existing = s.scalar(select(Message).where(Message.external_id == msg.message_id))
            s.execute(update(Message).where(Message.id == existing.id).values(delivery_count=Message.delivery_count + 1))
            s.commit()
            if existing.processing_status == "done":
                return "duplicate", existing.id, existing.reply_text
            # A previous delivery failed, or its worker died: take it over and process again.
            reclaimed = s.execute(text(
                "UPDATE messages SET processing_status='processing', updated_at=now() WHERE id=:id AND "
                "(processing_status='failed' OR updated_at < now() - interval '60 seconds') RETURNING id"
            ), {"id": existing.id}).first()
            s.commit()
            if reclaimed:
                return "new", existing.id, None
            return "duplicate", existing.id, None

    def _replay_duplicate(self, msg: InboundMessage, row_id: int, stored: str | None, deadline: float) -> dict:
        reply = stored
        # The first delivery may still be in flight: wait for it (bounded by the vendor timeout).
        while reply is None and time.monotonic() < deadline:
            time.sleep(0.25)
            with SessionLocal() as s:
                m = s.get(Message, row_id)
                if m and m.processing_status in ("done", "failed") and m.reply_text:
                    reply = m.reply_text
        with SessionLocal() as s:
            m = s.get(Message, row_id)
            conv_id = m.conversation_id if m else None
            tr = Tracer(s, msg.rider_id, conv_id, msg.message_id)
            tr.step("message_in", "rider_message_duplicate",
                    {"message_id": msg.message_id, "text": msg.text, "received_at": msg.received_at.isoformat()}, None)
            tr.decision("duplicate_delivery_ignored",
                        {"message_id": msg.message_id, "delivery_count": m.delivery_count if m else None},
                        {"action": "replayed_stored_reply" if reply else "first_delivery_still_processing",
                         "same_text": bool(m and m.text == msg.text)})
            if reply is None:
                lang = (s.get(Conversation, conv_id).language if conv_id else "hinglish")
                reply = R.say("holding", lang)
            tr.step("reply", "reply_to_rider_duplicate", None, reply)
        return {"reply": reply, "message_id": msg.message_id, "duplicate": True}

    def _fail_safely(self, msg: InboundMessage, row_id: int, exc: Exception) -> str:
        with SessionLocal() as s:
            m = s.get(Message, row_id)
            lang = "hinglish"
            if m and m.conversation_id:
                conv = s.get(Conversation, m.conversation_id)
                lang = conv.language if conv else lang
            reply = R.say("error", lang)
            tr = Tracer(s, msg.rider_id, m.conversation_id if m else None, msg.message_id)
            tr.error("agent_exception", {"message_id": msg.message_id}, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            exists = s.scalar(select(OpsItem).where(
                OpsItem.source_message_id == msg.message_id, OpsItem.reason_code == "agent_error"))
            if not exists:
                s.add(OpsItem(
                    id=new_id("OPS"), rider_id=msg.rider_id, type=ops_m.ESCALATION, amount=None,
                    reason=f"The agent failed while handling a message ({type(exc).__name__}). Please handle manually.",
                    reason_code="agent_error", status=ops_m.OPS_PENDING, source_message_id=msg.message_id,
                    conversation_id=m.conversation_id if m else None, context={"text": msg.text},
                ))
            tr.step("reply", "reply_to_rider", None, reply)
            if m:
                m.processing_status = "failed"
                m.reply_text = reply
            s.commit()
        return reply

    # ================================================================== main processing
    def _process(self, msg: InboundMessage, row_id: int, deadline: float) -> str:
        session = SessionLocal()
        try:
            received_at = parse_received_at(msg.received_at)
            conv = self._conversation(session, msg.rider_id, received_at)
            row = session.get(Message, row_id)
            row.conversation_id = conv.id
            tracer = Tracer(session, msg.rider_id, conv.id, msg.message_id)
            tracer.step("message_in", "rider_message",
                        {"message_id": msg.message_id, "text": msg.text, "received_at": msg.received_at.isoformat()},
                        None)
            turn = Turn(session, conv, tracer, msg, msg.rider_id, ist_date(received_at), conv.language, deadline)

            if session.get(Rider, msg.rider_id) is None:
                tracer.decision("rider_lookup", {"rider_id": msg.rider_id}, {"found": False, "action": "reply_only"})
                parts = [R.say("rider_unknown", conv.language)]
            else:
                context = self._context(turn)
                interp, meta = self.interpreter.interpret(msg.text, turn.today, msg.rider_id, context)
                turn.interp = interp
                tracer.tool("interpret_message",
                            {"text": msg.text, "today_ist": turn.today.isoformat(), "context": context},
                            {**interp.model_dump(mode="json"), "engine": meta})
                turn.lang = self._pick_language(conv, interp, msg.text)
                parts = self._route(turn)

            reply = " ".join(p.strip() for p in parts if p and p.strip())
            tracer.step("reply", "reply_to_rider", None, reply)
            session.add(Message(conversation_id=conv.id, rider_id=msg.rider_id, direction="out", text=reply,
                                in_reply_to=msg.message_id, processing_status="done"))
            row.processing_status = "done"
            row.reply_text = reply
            conv.last_message_at = max(conv.last_message_at, received_at)
            conv.language = turn.lang
            session.commit()
            return reply
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _conversation(self, session: Session, rider_id: str, received_at: datetime) -> Conversation:
        conv = session.scalar(select(Conversation).where(
            Conversation.rider_id == rider_id, Conversation.status == "open"
        ).order_by(Conversation.last_message_at.desc()))
        idle = timedelta(hours=self.settings.conversation_idle_hours)
        if conv and received_at - conv.last_message_at <= idle:
            return conv
        if conv:
            conv.status = "closed"
        conv = Conversation(id=new_id("CNV"), rider_id=rider_id, status="open", language="hinglish", state={},
                            started_at=received_at, last_message_at=received_at)
        session.add(conv)
        session.flush()
        return conv

    @staticmethod
    def _pick_language(conv: Conversation, interp: Interpretation, text_: str) -> str:
        words = re.findall(r"[^\W\d_]+", text_)
        if len(words) <= 1 and conv.language:
            return conv.language  # "20", "T926334", "ok": keep the conversation's language
        return interp.language or conv.language or "hinglish"

    def _context(self, turn: Turn) -> dict:
        s, conv = turn.session, turn.conv
        state = conv.state or {}
        disputes = list(s.scalars(select(Dispute).where(Dispute.conversation_id == conv.id).order_by(Dispute.created_at)))
        open_amounts = sorted({d.owed_amount for d in disputes if d.owed_amount and d.status in STATUS_DISPUTES})
        recent = list(s.scalars(select(Message).where(Message.conversation_id == conv.id)
                                .order_by(Message.id.desc()).limit(7)))
        recent_turns = [{"from": "rider" if m.direction == "in" else "agent", "text": m.text[:300]}
                        for m in reversed(recent) if m.external_id != turn.msg.message_id]
        pending = state.get("pending")
        return {
            "pending_question": "date_or_order_id" if pending else None,
            "awaiting_details": bool(pending),
            "open_amounts": open_amounts,
            "last_dispute_day": disputes[-1].dispute_date.isoformat() if disputes and disputes[-1].dispute_date else None,
            "recent_turns": recent_turns,
        }

    # ================================================================== routing
    def _route(self, t: Turn) -> list[str]:
        interp = t.interp
        assert interp is not None
        flags = interp.flags
        state = dict(t.conv.state or {})

        if flags.prompt_injection:
            t.tracer.decision("security_screen", {"flag": "prompt_injection"},
                              {"action": "refuse_and_escalate", "money_moved": False})
            self._escalate(t, "prompt_injection",
                           f"Message tried to instruct the system / force approvals: \"{t.msg.text[:200]}\"")
            return [R.say("injection_refuse", t.lang)]
        if flags.impersonation:
            others = [r for r in interp.mentioned_rider_ids if r != t.rider_id]
            t.tracer.decision("security_screen", {"flag": "impersonation", "mentioned": others},
                              {"action": "refuse_and_escalate", "money_moved": False})
            self._escalate(t, "impersonation",
                           f"Sender {t.rider_id} claimed to be / asked about another rider ({', '.join(others)}): "
                           f"\"{t.msg.text[:200]}\"")
            return [R.say("only_own_account", t.lang, rider=t.rider_id)]

        pending = state.get("pending")
        last_ids = state.get("last_disputes", [])
        claims = [c.model_copy(deep=True) for c in interp.claims]
        inherited = None
        for c in claims:
            if not c.categories:
                if pending:
                    c.categories = list(pending.get("categories") or ["general"])
                    inherited = "pending_question"
                elif state.get("last_categories") and (flags.correction or not c.trip_ids):
                    c.categories = list(state["last_categories"])
                    inherited = "last_dispute"
                else:
                    c.categories = ["general"]
            if pending:
                c.claimed_amount = c.claimed_amount or pending.get("claimed_amount")
                c.claimed_trip_count = c.claimed_trip_count or pending.get("claimed_trip_count")
        if inherited:
            t.tracer.decision("merge_with_memory", {"source": inherited, "pending": pending,
                                                    "last_categories": state.get("last_categories")},
                              {"claims": [c.model_dump(mode="json") for c in claims]})

        # Pushback on our last verdict ("nahi nahi 12 kiye the, dobara check karo")
        if flags.pushback and last_ids:
            last_days = {d.dispute_date for d in (t.session.get(Dispute, i) for i in last_ids) if d}
            if not claims or all((c.date in last_days) or not c.has_scope() for c in claims):
                return self._pushback(t, last_ids)

        parts: list[str] = []
        scoped = [c for c in claims if c.has_scope()]
        unscoped = [c for c in claims if not c.has_scope()]
        if scoped:
            parts += self._process_claims(t, scoped)
            state = dict(t.conv.state or {})
            state["pending"] = None
            state["unclear"] = 0
        elif unscoped:
            ask_count = int((pending or {}).get("asks", 0)) + 1
            c0 = unscoped[0]
            state["pending"] = {
                "categories": sorted({cat for c in unscoped for cat in c.categories}),
                "claimed_amount": c0.claimed_amount, "claimed_trip_count": c0.claimed_trip_count, "asks": ask_count,
            }
            t.tracer.decision("need_more_information", {"claims": [c.model_dump(mode="json") for c in unscoped]},
                              {"ask": "date_or_order_id", "times_asked": ask_count})
            if ask_count >= 3:
                self._escalate(t, "unclear", "Rider could not give a date or order id after 3 attempts.")
                parts.append(R.say("escalated", t.lang))
            else:
                parts.append(R.say("ask_details" if ask_count == 1 else "ask_details_again", t.lang))
        t.conv.state = state

        if flags.accident_or_emergency:
            self._escalate(t, "accident_or_emergency",
                           f"Rider mentions an accident/emergency; review penalty waiver / welfare: \"{t.msg.text[:200]}\"")
            parts = [R.say("accident", t.lang)] + parts + [R.say("accident_escalated", t.lang)]
        if flags.wants_human and not t.escalated:
            self._escalate(t, "rider_requested_human", f"Rider asked to talk to a person: \"{t.msg.text[:200]}\"")
            parts.append(R.say("wants_human", t.lang))
        if parts:
            return parts

        if flags.status_query or flags.payment_request or interp.amount_references:
            return self._status(t, urge=flags.payment_request)
        if flags.pushback:  # pushback but nothing to push back on
            return [R.say("ask_details", t.lang)]
        if flags.acknowledgement:
            open_ = self._status_lines(t, only_open=True)
            return [R.say("thanks", t.lang)] + open_
        if flags.greeting:
            return [R.say("greet", t.lang)]

        unclear = int(state.get("unclear", 0)) + 1
        state["unclear"] = unclear
        t.conv.state = state
        t.tracer.decision("unclear_message", {"text": t.msg.text}, {"times": unclear})
        if unclear >= 3:
            self._escalate(t, "unclear", "Agent could not understand the rider after 3 messages.")
            return [R.say("escalated", t.lang)]
        return [R.say("ask_details" if unclear == 1 else "ask_details_again", t.lang)]

    # ================================================================== claims
    def _process_claims(self, t: Turn, claims: list[Claim]) -> list[str]:
        s = t.session
        parts: list[str] = []
        desk = build_desk_view(s, self.payswift, t.rider_id)
        t.tracer.tool("payswift.list_payouts", {"rider_id": t.rider_id}, desk.summary())

        all_ids = sorted({tid for c in claims for tid in c.trip_ids})
        trips = {tr.trip_id: tr for tr in s.scalars(select(Trip).where(Trip.trip_id.in_(all_ids)))} if all_ids else {}
        not_found = [tid for tid in all_ids if tid not in trips]
        foreign = [tid for tid in all_ids if tid in trips and trips[tid].rider_id != t.rider_id]
        if all_ids:
            t.tracer.tool("lookup_trips", {"rider_id": t.rider_id, "trip_ids": all_ids}, {
                "found": {tid: {"day": tr.ist_date.isoformat(), "status": tr.status}
                          for tid, tr in trips.items() if tr.rider_id == t.rider_id},
                "not_found": not_found, "belongs_to_another_rider": foreign,
            })

        groups: dict[date, DayClaim] = {}
        for c in claims:
            own = [trips[tid] for tid in c.trip_ids if tid in trips and trips[tid].rider_id == t.rider_id]
            if c.trip_ids and not own and c.date is None:
                continue
            days = sorted({tr.ist_date for tr in own}) if own else [c.date]
            if c.date and own and c.date not in days:
                t.tracer.decision("date_mismatch", {"stated": c.date.isoformat(), "trip_days": [d.isoformat() for d in days]},
                                  {"action": "use_trip_records"})
            for d in days:
                g = groups.setdefault(d, DayClaim(day=d))
                g.categories |= set(c.categories)
                g.trip_ids |= {tr.trip_id for tr in own if tr.ist_date == d}
                g.explicit_date |= (c.date == d)
                g.claimed_amount = g.claimed_amount or c.claimed_amount
                g.claimed_trip_count = g.claimed_trip_count or c.claimed_trip_count
                g.claimed_distance_km = g.claimed_distance_km or c.claimed_distance_km

        if foreign:
            self._escalate(t, "trip_of_another_rider",
                           f"{t.rider_id} asked about order(s) {', '.join(foreign)} that belong to another rider.")
        for tid in not_found + foreign:  # same answer for both: never reveal another rider's data
            parts.append(R.say("trip_not_found", t.lang, trip=tid))

        auto_today = desk.auto_payments_on(t.today)
        for d in sorted(groups):
            day_parts, auto_today = self._handle_day(t, groups[d], desk, auto_today)
            parts += day_parts
        state = dict(t.conv.state or {})
        if t.new_dispute_ids:
            state["last_disputes"] = t.new_dispute_ids
            state["last_categories"] = sorted({c for g in groups.values() for c in g.categories})
        t.conv.state = state
        return parts

    def _new_dispute(self, t: Turn, g: DayClaim, status: str) -> Dispute:
        d = Dispute(
            id=new_id("DSP"), rider_id=t.rider_id, conversation_id=t.conv.id, source_message_id=t.msg.message_id,
            business_date=t.today, dispute_date=g.day, categories=sorted(g.categories), trip_ids=sorted(g.trip_ids),
            claimed_amount=g.claimed_amount, claim_text=t.msg.text, status=status, owed_amount=0, paid_amount=0,
            findings={"claim": g.as_dict()},
        )
        t.session.add(d)
        t.session.flush()
        t.new_dispute_ids.append(d.id)
        return d

    def _handle_day(self, t: Turn, g: DayClaim, desk: DeskView, auto_today: int) -> tuple[list[str], int]:
        s, lang, window = t.session, t.lang, self.settings.dispute_window_days
        age = (t.today - g.day).days
        t.tracer.decision("check_dispute_window", {"day": g.day.isoformat(), "today": t.today.isoformat(),
                                                   "window_days": window},
                          {"age_days": age, "within_window": 0 <= age <= window})
        if age < 0:
            return [R.say("future_date", lang, day=R.day(g.day))], auto_today
        if age > window:
            d = self._new_dispute(t, g, ds.OUT_OF_WINDOW)
            d.decision, d.decision_reason = "none", f"{g.day} is {age} days old; policy covers the last {window} days."
            return [R.say("out_of_window", lang, day=R.day(g.day), window=window,
                          start=R.day(t.today - timedelta(days=window)))], auto_today

        ledger = build_day_ledger(s, t.rider_id, g.day, desk.paid_by_item, desk.reserved_by_item)
        t.tracer.tool("compute_expected_vs_actual", {"rider_id": t.rider_id, "day": g.day.isoformat()}, {
            **ledger.summary(),
            "items": [{k: v for k, v in i.as_dict().items() if k != "facts"} for i in ledger.items],
        })
        has_activity = any(i.kind != "incentive" for i in ledger.items) or any(i.paid_lines for i in ledger.items)
        if not has_activity:
            d = self._new_dispute(t, g, ds.NOT_FOUND)
            d.decision, d.decision_reason = "none", "No trips or payout lines on that day."
            return [R.say("no_trips_day", lang, day=R.day(g.day))], auto_today

        whole_day = "general" in g.categories and g.explicit_date
        scope = scope_claim(ledger, set(g.categories), set() if whole_day else set(g.trip_ids))
        t.tracer.decision("verify_claim", {**g.as_dict(), "scope": "whole_day" if whole_day else
                                           ("trips" if g.trip_ids else "category")}, scope.as_dict())

        d = self._new_dispute(t, g, ds.NO_DISCREPANCY)
        d.expected_amount = sum(i.expected for i in scope.scoped)
        d.actual_amount = sum(i.actual for i in scope.scoped)
        d.findings = {**d.findings, "verification": scope.as_dict(), "payswift": {"verified": desk.verified}}

        if not scope.positive:
            settled = [i for i in scope.scoped if i.paid_desk > 0 and i.shortfall == 0]
            if settled:
                d.status = ds.ALREADY_PAID
                amount = sum(i.paid_desk for i in settled)
                refs = sorted({r for i in settled for r in desk.paid_refs_by_item.get(i.key, [])})
                text_ = R.say("already_paid", lang, day=R.day(g.day), amount=amount, ref=", ".join(refs) or "PaySwift")
            else:
                text_ = self._explain_ok(t, g, scope)
            d.decision, d.decision_reason = "none", "No shortfall found in the records for this claim."
            d.findings = {**d.findings, "explanation": text_}
            t.tracer.decision("resolve_dispute", {"dispute_id": d.id}, {"status": d.status, "owed": 0})
            return [text_], auto_today

        lines = R.describe_shortfalls([i.as_dict() for i in scope.positive], lang)
        if scope.widened:
            lines.insert(0, R.say("widened", lang))
        if scope.owed == 0 and scope.reserved_items:
            details = [x for i in scope.reserved_items for x in desk.reserved_detail.get(i.key, [])]
            state = "state_approval" if any(x["source"] == "approval" for x in details) else "state_processing"
            d.status = ds.PENDING_APPROVAL if state == "state_approval" else ds.PAYMENT_PROCESSING
            d.decision, d.decision_reason = "none", f"Already open: {details}"
            amount = sum(min(i.reserved, i.shortfall) for i in scope.reserved_items)
            t.tracer.decision("resolve_dispute", {"dispute_id": d.id, "open_items": details},
                              {"status": "duplicate_of_open_item", "owed_now": 0})
            d.owed_amount = amount
            return lines + [R.say("already_pending", lang, day=R.day(g.day), amount=amount,
                                  state=R.say(state, lang))], auto_today
        if scope.owed == 0:  # capped by an overpayment elsewhere that day
            d.decision, d.decision_reason = "none", "Shortfall offset by an overpayment on the same day."
            text_ = R.say("offset", lang, day=R.day(g.day), gross=scope.gross_shortfall,
                          expected=ledger.expected_total, actual=ledger.actual_total)
            d.findings = {**d.findings, "explanation": text_}
            t.tracer.decision("resolve_dispute", {"dispute_id": d.id}, {"status": d.status, "owed": 0,
                                                                         "reason": "day_net_cap"})
            return [text_], auto_today

        owed = scope.owed
        if g.claimed_amount and g.claimed_amount > owed:
            lines.append(R.say("claimed_vs_found", lang, claimed=g.claimed_amount, day=R.day(g.day), found=owed))
        repeat = any(i.paid_desk > 0 for i in scope.positive)
        decision = authz.authorize(owed=owed, settings=self.settings, auto_payments_today=auto_today,
                                   repeat_of_paid_items=repeat, payswift_verified=desk.verified)
        t.tracer.decision("authorize_payout", {
            "dispute_id": d.id, "owed": owed, "auto_payments_today": auto_today, "repeat_of_paid_items": repeat,
            "payswift_verified": desk.verified, "auto_pay_max_per_dispute": self.settings.auto_pay_max_per_dispute,
            "auto_pay_max_per_rider_per_day": self.settings.auto_pay_max_per_rider_per_day,
        }, decision.as_dict())
        items = allocate(owed, scope.positive)
        d.owed_amount = owed
        d.decision, d.decision_reason = decision.action, decision.reason

        if decision.action == authz.AUTO_PAY:
            tx = self.payments.create_transaction(s, dispute=d, amount=owed, items=items, mode="auto",
                                                  business_date=t.today)
            d.status = ds.PAYMENT_PROCESSING
            s.commit()
            auto_today += 1
            tx = self.payments.execute(tx.id, deadline=t.deadline - 0.3)
            s.refresh(d)
            if tx is not None and tx.status == TX_SUCCEEDED:
                lines.append(R.say("paid", lang, amount=owed, ref=tx.payswift_payout_id))
            else:
                lines.append(R.say("processing", lang, amount=owed))
            return lines, auto_today

        if decision.action == authz.APPROVAL:
            item = OpsItem(
                id=new_id("OPS"), rider_id=t.rider_id, type=ops_m.APPROVAL, amount=owed, reason=decision.reason,
                reason_code=decision.reason_code, status=ops_m.OPS_PENDING, dispute_id=d.id,
                conversation_id=t.conv.id, source_message_id=t.msg.message_id,
                context={"items": items, "day": g.day.isoformat(), "findings": [i.as_dict() for i in scope.positive]},
            )
            s.add(item)
            d.status = ds.PENDING_APPROVAL
            audit(s, "agent", "approval_requested", "ops_item", item.id, t.rider_id,
                  {"amount": owed, "reason_code": decision.reason_code})
            t.tracer.decision("request_ops_approval", {"dispute_id": d.id, "amount": owed},
                              {"ops_item_id": item.id, "reason": decision.reason})
            key = {"over_auto_limit": "approval_over_limit", "daily_auto_limit": "approval_daily",
                   "repeat_dispute": "approval_repeat"}.get(decision.reason_code, "approval_unverified")
            lines.append(R.say(key, lang, amount=owed, limit=self.settings.auto_pay_max_per_dispute))
            return lines, auto_today

        d.status = ds.ESCALATED
        self._escalate(t, decision.reason_code, decision.reason, amount=owed, dispute_id=d.id)
        lines.append(R.say("escalated", lang))
        return lines, auto_today

    def _explain_ok(self, t: Turn, g: DayClaim, scope) -> str:
        lang = t.lang
        ledger = scope.ledger
        cats = set(g.categories)
        inc = next((i for i in ledger.items if i.kind == "incentive"), None)
        offer = ""
        if g.trip_ids:
            items = [i for i in scope.scoped if i.kind == "trip"]
            text_ = " ".join(R.describe_trip_ok(i.as_dict(), lang, cats) for i in items)
            if "distance" in cats:
                offer = R.say("offer_escalation", lang)
            if "incentive" in cats and inc is not None:
                text_ += " " + self._incentive_text(inc, g, lang)
        elif "incentive" in cats and inc is not None:
            text_ = self._incentive_text(inc, g, lang)
            if g.claimed_trip_count and g.claimed_trip_count >= 12 and inc.expected == 0:
                offer = R.say("offer_escalation", lang)
        elif "penalty" in cats and any(i.status in ("cancelled_by_rider", "cancelled_by_customer") for i in ledger.items):
            text_ = " ".join(R.describe_trip_ok(i.as_dict(), lang, cats) for i in ledger.items
                             if i.kind == "trip" and i.status != "completed")
        else:
            text_ = R.say("day_correct", lang, day=R.day(g.day), expected=ledger.expected_total,
                          actual=ledger.actual_total)
            if g.claimed_trip_count:
                text_ = R.say("count_vs_found", lang, day=R.day(g.day), n=ledger.completed_trips) + " " + text_
        return f"{text_} {offer}".strip()

    @staticmethod
    def _incentive_text(inc, g: DayClaim, lang: str) -> str:
        n = inc.facts.get("completed_trips", 0)
        if inc.expected == 0:
            return R.say("incentive_not_eligible", lang, day=R.day(g.day), n=n)
        return R.say("incentive_paid", lang, day=R.day(g.day), n=n)

    # ================================================================== pushback
    def _pushback(self, t: Turn, last_ids: list[str]) -> list[str]:
        s = t.session
        parts: list[str] = []
        disputes = [d for d in (s.get(Dispute, i) for i in last_ids) if d is not None]
        desk = build_desk_view(s, self.payswift, t.rider_id)
        t.tracer.tool("payswift.list_payouts", {"rider_id": t.rider_id}, desk.summary())
        for d in disputes:
            g = DayClaim(day=d.dispute_date, categories=set(d.categories or []), trip_ids=set(d.trip_ids or []),
                         explicit_date=bool((d.findings or {}).get("claim", {}).get("explicit_date")),
                         claimed_amount=d.claimed_amount,
                         claimed_trip_count=(t.interp.claims[0].claimed_trip_count if t.interp and t.interp.claims
                                             else None) or (d.findings or {}).get("claim", {}).get("claimed_trip_count"))
            if d.status in (ds.OUT_OF_WINDOW, ds.NOT_FOUND):
                parts.append(R.say("out_of_window" if d.status == ds.OUT_OF_WINDOW else "no_trips_day", t.lang,
                                   day=R.day(d.dispute_date), window=self.settings.dispute_window_days,
                                   start=R.day(t.today - timedelta(days=self.settings.dispute_window_days))))
                continue
            ledger = build_day_ledger(s, t.rider_id, d.dispute_date, desk.paid_by_item, desk.reserved_by_item)
            whole_day = "general" in g.categories and g.explicit_date
            scope = scope_claim(ledger, set(g.categories), set() if whole_day else set(g.trip_ids))
            t.tracer.decision("recheck_after_pushback", {"dispute_id": d.id, **g.as_dict()}, scope.as_dict())
            if scope.owed > 0 and d.status == ds.NO_DISCREPANCY:
                # Records changed since our last answer: handle it as a fresh claim.
                day_parts, _ = self._handle_day(t, g, desk, desk.auto_payments_on(t.today))
                parts += day_parts
                continue
            if d.status == ds.NO_DISCREPANCY:
                fact = self._explain_ok(t, g, scope)
                fact = fact.replace(R.say("offer_escalation", t.lang), "").strip()
            else:
                fact = R.say("status_" + {"auto_paid": "paid", "approved_paid": "paid",
                                          "pending_approval": "pending_approval"}.get(d.status, "escalated"),
                             t.lang, amount=d.owed_amount, what=self._what(d), ref=self._payout_ref(s, d),
                             status="processed", note="")
            already = s.scalar(select(OpsItem).where(OpsItem.dispute_id == d.id, OpsItem.type == ops_m.ESCALATION,
                                                     OpsItem.status == ops_m.OPS_PENDING))
            if already:
                parts.append(R.say("recheck_already", t.lang, fact=fact))
                continue
            claim_bits = []
            if g.claimed_trip_count:
                claim_bits.append(f"rider says {g.claimed_trip_count} trips")
            if g.claimed_amount:
                claim_bits.append(f"rider says Rs {g.claimed_amount}")
            records = (f"records: {ledger.completed_trips} completed trips, expected Rs {ledger.expected_total}, "
                       f"paid Rs {ledger.actual_total}")
            self._escalate(t, "rider_disputes_records",
                           f"Rider disputes our answer for {d.dispute_date} ({', '.join(claim_bits) or 'asks to re-check'}); "
                           f"{records}.", dispute_id=d.id)
            if d.status == ds.NO_DISCREPANCY:
                d.status = ds.ESCALATED
            parts.append(R.say("recheck_escalated", t.lang, fact=fact))
        return parts or [R.say("ask_details", t.lang)]

    # ================================================================== status
    def _what(self, d: Dispute) -> str:
        positive = ((d.findings or {}).get("verification") or {}).get("items_with_shortfall") or []
        trip_ids = [i["trip_id"] for i in positive if i.get("trip_id")] or list(d.trip_ids or [])
        if trip_ids:
            return ("order " if len(trip_ids) == 1 else "orders ") + ", ".join(trip_ids)
        return R.day(d.dispute_date)

    def _payout_ref(self, s: Session, d: Dispute) -> str:
        tx = s.scalar(select(PaymentTransaction).where(PaymentTransaction.dispute_id == d.id,
                                                       PaymentTransaction.status == TX_SUCCEEDED))
        return tx.payswift_payout_id if tx else "-"

    def _status_lines(self, t: Turn, only_open: bool = False) -> list[str]:
        s = t.session
        since = t.today - timedelta(days=self.settings.dispute_window_days)
        disputes = list(s.scalars(select(Dispute).where(
            Dispute.rider_id == t.rider_id, Dispute.status.in_(STATUS_DISPUTES), Dispute.business_date >= since,
        ).order_by(Dispute.created_at)))
        if only_open:
            disputes = [d for d in disputes if d.status in (ds.PAYMENT_PROCESSING, ds.PENDING_APPROVAL)]
        if not disputes:
            return []
        txs = {tx.dispute_id: tx for tx in s.scalars(select(PaymentTransaction).where(
            PaymentTransaction.rider_id == t.rider_id))}
        live: dict[str, dict] = {}
        live_by_tx: dict[str, dict] = {}
        try:
            for p in self.payswift.list_payouts(t.rider_id):
                live[p.get("payout_id")] = p
                ref = parse_reference(p.get("reference"))
                if ref:
                    live_by_tx[ref["tx"]] = p
            verified = True
        except Exception as exc:  # status replies still work from our records
            verified = False
            log.warning("payswift unavailable for status", extra={"error": str(exc)})
        t.tracer.tool("payswift.verify_payment_status", {"rider_id": t.rider_id},
                      {"verified": verified, "payouts": [{"payout_id": k, "amount": v.get("amount"),
                                                          "status": v.get("status")} for k, v in live.items()]})
        lines = []
        for d in disputes:
            tx = txs.get(d.id)
            what = self._what(d)
            if d.status in (ds.AUTO_PAID, ds.APPROVED_PAID) and tx:
                p = live.get(tx.payswift_payout_id) or {}
                lines.append(R.say("status_paid", t.lang, amount=tx.amount, what=what, ref=tx.payswift_payout_id,
                                   status=p.get("status") or tx.payswift_status or "processed"))
            elif d.status == ds.PAYMENT_PROCESSING and tx and tx.id in live_by_tx:
                # PaySwift already has it; our background worker will record it on its next pass.
                p = live_by_tx[tx.id]
                lines.append(R.say("status_paid", t.lang, amount=tx.amount, what=what, ref=p.get("payout_id"),
                                   status=p.get("status") or "processed"))
            elif d.status == ds.PAYMENT_PROCESSING:
                lines.append(R.say("status_processing", t.lang, amount=d.owed_amount, what=what))
            elif d.status == ds.PENDING_APPROVAL:
                lines.append(R.say("status_pending_approval", t.lang, amount=d.owed_amount, what=what))
            elif d.status == ds.REJECTED:
                item = s.scalar(select(OpsItem).where(OpsItem.dispute_id == d.id, OpsItem.status == ops_m.OPS_REJECTED))
                note = f" ({item.decision_note})" if item and item.decision_note else ""
                lines.append(R.say("status_rejected", t.lang, amount=d.owed_amount, what=what, note=note))
            elif d.status == ds.PAYMENT_FAILED:
                lines.append(R.say("status_failed", t.lang, amount=d.owed_amount, what=what))
            elif d.status == ds.ESCALATED:
                lines.append(R.say("status_escalated", t.lang, what=what))
        return [ln[:1].upper() + ln[1:] for ln in lines]

    def _status(self, t: Turn, urge: bool) -> list[str]:
        lines = self._status_lines(t)
        t.tracer.decision("status_update", {"payment_request": urge}, {"lines": len(lines)})
        if not lines:
            return [R.say("status_none", t.lang)]
        has_pending = any("approval" in ln.lower() or "approve" in ln.lower() or "अप्रूवल" in ln for ln in lines)
        if urge and has_pending:
            lines.insert(0, R.say("urge_policy", t.lang))
        return lines

    # ================================================================== escalation
    def _escalate(self, t: Turn, code: str, reason: str, amount: int | None = None,
                  dispute_id: str | None = None) -> OpsItem:
        s = t.session
        existing = s.scalar(select(OpsItem).where(
            OpsItem.conversation_id == t.conv.id, OpsItem.reason_code == code, OpsItem.status == ops_m.OPS_PENDING,
            OpsItem.type == ops_m.ESCALATION,
            (OpsItem.dispute_id == dispute_id) if dispute_id else OpsItem.dispute_id.is_(None),
        ))
        if existing:
            t.tracer.decision("escalate_to_ops", {"reason_code": code}, {"ops_item_id": existing.id, "deduplicated": True})
            t.escalated = True
            return existing
        item = OpsItem(
            id=new_id("OPS"), rider_id=t.rider_id, type=ops_m.ESCALATION, amount=amount, reason=reason,
            reason_code=code, status=ops_m.OPS_PENDING, dispute_id=dispute_id, conversation_id=t.conv.id,
            source_message_id=t.msg.message_id, context={"text": t.msg.text},
        )
        s.add(item)
        s.flush()
        audit(s, "agent", "escalated", "ops_item", item.id, t.rider_id, {"reason_code": code, "reason": reason})
        t.tracer.decision("escalate_to_ops", {"reason_code": code, "dispute_id": dispute_id},
                          {"ops_item_id": item.id, "reason": reason})
        t.escalated = True
        return item
