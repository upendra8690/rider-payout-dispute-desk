"""Structured interpretation of one rider message. Produced by the LLM or the rule parser.

This is the ONLY thing the language model decides. It never sees or sets amounts to pay.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal["surge", "incentive", "penalty", "distance", "unpaid", "general"]
Language = Literal["en", "hinglish", "hi"]
Intent = Literal[
    "complaint", "provide_info", "status", "pushback", "acknowledge", "greeting",
    "wants_human", "payment_request", "other",
]


class Claim(BaseModel):
    categories: list[Category] = Field(default_factory=list)
    date: dt.date | None = None
    trip_ids: list[str] = Field(default_factory=list)
    claimed_amount: int | None = None
    claimed_trip_count: int | None = None
    claimed_distance_km: float | None = None

    @field_validator("trip_ids")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t in v:
            t = str(t).strip().upper()
            if t and t not in out:
                out.append(t)
        return out

    def has_scope(self) -> bool:
        return bool(self.date or self.trip_ids)


class Flags(BaseModel):
    prompt_injection: bool = False
    impersonation: bool = False
    accident_or_emergency: bool = False
    correction: bool = False
    wants_human: bool = False
    pushback: bool = False
    status_query: bool = False
    payment_request: bool = False
    acknowledgement: bool = False
    greeting: bool = False


class Interpretation(BaseModel):
    language: Language = "hinglish"
    intent: Intent = "other"
    claims: list[Claim] = Field(default_factory=list)
    mentioned_rider_ids: list[str] = Field(default_factory=list)
    amount_references: list[int] = Field(default_factory=list)
    flags: Flags = Field(default_factory=Flags)
    source: str = "rules"
    notes: list[str] = Field(default_factory=list)

    def all_trip_ids(self) -> list[str]:
        out: list[str] = []
        for c in self.claims:
            for t in c.trip_ids:
                if t not in out:
                    out.append(t)
        return out
