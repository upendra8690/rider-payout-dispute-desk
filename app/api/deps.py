"""Shared API dependencies: services from app.state and ops authentication."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings


def services(request: Request):
    return request.app.state.services


def require_ops_token(
    x_ops_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    """Ops endpoints that change state or expose rider PII need the shared ops token."""
    token = x_ops_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    expected = get_settings().ops_api_token
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid ops token",
                            headers={"WWW-Authenticate": "Bearer"})
    return token
