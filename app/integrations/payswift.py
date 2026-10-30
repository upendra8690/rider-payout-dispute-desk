"""PaySwift Payouts API client.

Observed sandbox behaviour ("behaves like production"):
  201 created | 200 idempotent replay (same Idempotency-Key + same body)
  400 invalid request (permanent)
  409 idempotency_key_reused (permanent: our bug) | 409 request_in_progress (retry later)
  429 account_blocked: >30 POSTs in 10s blocks the account for ~60s
  503 service_unavailable: payout NOT created
  504 gateway_timeout: payout MAY have been created ("lost response")
  slow responses (several seconds); outages
Every create call carries an Idempotency-Key, so a retry can never create a second payout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.utils.logging import get_logger

log = get_logger(__name__)

# Outcome kinds
CREATED = "created"
REPLAYED = "replayed"
RETRYABLE = "retryable"  # definitely not created; safe to retry
UNKNOWN = "unknown"  # may or may not exist; reconcile, then retry with the same key
IN_PROGRESS = "in_progress"
RATE_LIMITED = "rate_limited"
REJECTED = "rejected"  # permanent
KEY_CONFLICT = "key_conflict"  # permanent


@dataclass
class CreateResult:
    kind: str
    http_status: int | None
    body: dict[str, Any] | None
    error: str | None
    duration_ms: int
    request: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.kind in (CREATED, REPLAYED)


class PaySwiftError(Exception):
    pass


class PaySwiftClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout, transport=self._transport)

    def health(self, timeout: float = 2.0) -> bool:
        try:
            with self._client(timeout) as c:
                return c.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def create_payout(
        self, rider_id: str, amount: int, reference: str, idempotency_key: str, timeout: float
    ) -> CreateResult:
        payload = {"rider_id": rider_id, "amount": int(amount), "reference": reference}
        request_meta = {"payload": payload, "idempotency_key": idempotency_key, "timeout_s": round(timeout, 2)}
        started = time.monotonic()

        def done(kind: str, status: int | None, body: dict | None, error: str | None) -> CreateResult:
            ms = int((time.monotonic() - started) * 1000)
            log.info("payswift create_payout", extra={
                "rider_id": rider_id, "amount": amount, "kind": kind, "http_status": status, "ms": ms,
                "idempotency_key": idempotency_key,
            })
            return CreateResult(kind, status, body, error, ms, request_meta)

        try:
            with self._client(timeout) as c:
                resp = c.post("/v1/payouts", json=payload, headers={"Idempotency-Key": idempotency_key})
        except httpx.TimeoutException as exc:
            return done(UNKNOWN, None, None, f"timeout after {timeout:.1f}s: {type(exc).__name__}")
        except httpx.HTTPError as exc:
            # Connection refused before sending is "not created", but we cannot always tell;
            # treat transport errors as unknown and reconcile before retrying.
            return done(UNKNOWN, None, None, f"transport error: {type(exc).__name__}: {exc}")

        body: dict | None
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        status = resp.status_code
        if status == 201:
            return done(CREATED, status, body, None)
        if status == 200:
            return done(REPLAYED, status, body, None)
        if status == 400:
            return done(REJECTED, status, body, f"invalid request: {body}")
        if status == 409:
            err = (body or {}).get("error")
            if err == "request_in_progress":
                return done(IN_PROGRESS, status, body, "request with this key still in progress")
            return done(KEY_CONFLICT, status, body, f"idempotency conflict: {body}")
        if status == 429:
            return done(RATE_LIMITED, status, body, "rate limited / account temporarily blocked")
        if status == 503:
            return done(RETRYABLE, status, body, "service unavailable")
        if status >= 500:
            return done(UNKNOWN, status, body, f"server error {status}: payout may exist")
        return done(REJECTED, status, body, f"unexpected status {status}")

    def list_payouts(self, rider_id: str | None = None, timeout: float = 4.0, retries: int = 2) -> list[dict]:
        params = {"rider_id": rider_id} if rider_id else None
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with self._client(timeout) as c:
                    resp = c.get("/v1/payouts", params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    return list(data.get("data", []))
                last = PaySwiftError(f"list payouts http {resp.status_code}")
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
            time.sleep(0.2 * (attempt + 1))
        raise PaySwiftError(f"could not list PaySwift payouts: {last}")
