"""Identifier helpers."""

from __future__ import annotations

import hashlib
import re
import secrets

RIDER_RE = re.compile(r"^\s*[Rr]0*(\d{1,6})\s*$")


def normalize_rider_id(raw: object) -> str | None:
    """'R7', 'r19', 'R007' -> 'R007', 'R019'. Returns None when it is not a rider id."""
    if raw is None:
        return None
    m = RIDER_RE.match(str(raw))
    if not m:
        return None
    return f"R{int(m.group(1)):03d}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5).upper()}"


def stable_hash(*parts: object, length: int = 16) -> str:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return h[:length]
