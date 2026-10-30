"""Turn a rider message into an Interpretation: LLM when configured, rule parser otherwise.

The LLM output is never trusted blindly:
  * it must validate against the Interpretation schema,
  * trip ids / rider ids it returns must literally appear in the message (no invented ids),
  * dates must not be in the future,
  * safety flags (prompt injection, impersonation, accident) are OR-ed with the rule parser.
Any LLM failure falls back to the deterministic parser, and the trace records which one was used.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import ValidationError

from app.agents import nlu_rules
from app.agents.llm import LLMClient, LLMError
from app.config import Settings
from app.schemas.interpretation import Claim, Interpretation


class Interpreter:
    def __init__(self, settings: Settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or (LLMClient(settings) if settings.llm_enabled else None)

    def interpret(self, text: str, today: date, rider_id: str, context: dict) -> tuple[Interpretation, dict]:
        rules = nlu_rules.parse(text, today, rider_id, context)
        if self.llm is None:
            return rules, {"engine": "rules"}
        try:
            raw = self.llm.interpret(text, today, context)
            llm_interp = Interpretation.model_validate({**raw, "source": f"llm:{self.llm.name}"})
        except (LLMError, ValidationError, KeyError, TypeError, ValueError) as exc:
            rules.notes.append(f"llm_fallback: {type(exc).__name__}")
            return rules, {"engine": "rules", "llm_error": str(exc)[:300], "llm": self.llm.name}
        merged, adjustments = self.merge(llm_interp, rules, today, rider_id)
        return merged, {"engine": "llm", "llm": self.llm.name, "adjustments": adjustments}

    @staticmethod
    def merge(llm: Interpretation, rules: Interpretation, today: date, rider_id: str) -> tuple[Interpretation, list[str]]:
        adjustments: list[str] = []
        allowed_trips = set(rules.all_trip_ids())
        oldest = today - timedelta(days=400)
        claims: list[Claim] = []
        for c in llm.claims:
            kept = [t for t in c.trip_ids if t in allowed_trips]
            if len(kept) != len(c.trip_ids):
                adjustments.append(f"dropped trip ids not present in text: {sorted(set(c.trip_ids) - set(kept))}")
            d = c.date
            if d and (d > today or d < oldest):
                adjustments.append(f"dropped implausible date {d}")
                d = None
            claims.append(c.model_copy(update={"trip_ids": kept, "date": d}))
        covered = {t for c in claims for t in c.trip_ids}
        missing = allowed_trips - covered
        if missing:
            adjustments.append(f"added trip ids the model missed: {sorted(missing)}")
            claims += [c for c in rules.claims if set(c.trip_ids) & missing]
        if not claims and rules.claims:
            adjustments.append("model returned no claims; using rule-parser claims")
            claims = rules.claims

        flags = llm.flags.model_copy()
        for name in ("prompt_injection", "accident_or_emergency", "correction", "pushback", "status_query",
                     "payment_request", "wants_human"):
            if getattr(rules.flags, name) and not getattr(flags, name):
                setattr(flags, name, True)
                adjustments.append(f"flag {name} raised by rule parser")
        flags.impersonation = rules.flags.impersonation  # computed from literal ids only

        intent = llm.intent
        if flags.prompt_injection or flags.impersonation:
            intent = "other"
        merged = Interpretation(
            language=llm.language, intent=intent, claims=claims,
            mentioned_rider_ids=rules.mentioned_rider_ids,
            amount_references=sorted(set(llm.amount_references) | set(rules.amount_references)),
            flags=flags, source=llm.source, notes=adjustments,
        )
        return merged, adjustments
