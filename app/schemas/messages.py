"""API request / response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import normalize_rider_id


class InboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    rider_id: str = Field(min_length=1, max_length=16)
    text: str = Field(default="", max_length=4000)
    received_at: datetime

    @field_validator("rider_id")
    @classmethod
    def _rider(cls, v: str) -> str:
        norm = normalize_rider_id(v)
        if not norm:
            raise ValueError("rider_id must look like R003")
        return norm

    @field_validator("message_id")
    @classmethod
    def _mid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_id is required")
        return v


class ReplyOut(BaseModel):
    reply: str
    message_id: str | None = None
    duplicate: bool = False


class TraceStepOut(BaseModel):
    at: str
    type: str
    name: str
    input: Any = None
    output: Any = None


class PendingItemOut(BaseModel):
    id: str
    rider_id: str
    type: str
    amount: int | None
    reason: str
    created_at: str


class OpsDecisionIn(BaseModel):
    actor: str = Field(default="ops", min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=1000)
