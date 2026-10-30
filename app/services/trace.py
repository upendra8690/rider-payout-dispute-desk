"""Trace recorder: every step the agent (or a human / the payment worker) takes for a rider."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, TraceStep
from app.utils.timeutil import utcnow

STEP_TYPES = {"message_in", "tool_call", "decision", "reply", "error"}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class Tracer:
    """Writes trace steps; each step is committed immediately so the trace survives crashes."""

    def __init__(self, session: Session, rider_id: str, conversation_id: str | None = None,
                 message_id: str | None = None, autocommit: bool = True):
        self.session = session
        self.rider_id = rider_id
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.autocommit = autocommit

    def step(self, type_: str, name: str, input_: Any = None, output: Any = None) -> TraceStep:
        if type_ not in STEP_TYPES:
            raise ValueError(f"invalid trace step type {type_}")
        st = TraceStep(
            rider_id=self.rider_id, conversation_id=self.conversation_id, message_id=self.message_id,
            at=utcnow(), type=type_, name=name, input=_jsonable(input_), output=_jsonable(output),
        )
        self.session.add(st)
        if self.autocommit:
            self.session.commit()
        else:
            self.session.flush()
        return st

    def tool(self, name: str, input_: Any, output: Any) -> None:
        self.step("tool_call", name, input_, output)

    def decision(self, name: str, input_: Any, output: Any) -> None:
        self.step("decision", name, input_, output)

    def error(self, name: str, input_: Any, output: Any) -> None:
        self.step("error", name, input_, output)


def audit(session: Session, actor: str, action: str, entity_type: str, entity_id: str,
          rider_id: str | None = None, details: dict | None = None, note: str | None = None) -> None:
    session.add(AuditLog(
        actor=actor, action=action, entity_type=entity_type, entity_id=entity_id,
        rider_id=rider_id, details=_jsonable(details or {}), note=note,
    ))
