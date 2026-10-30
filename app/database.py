"""Database engine, sessions and per-rider locking."""

from __future__ import annotations

import contextlib
import zlib
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {"options": "-c timezone=UTC"} if settings.database_url.startswith("postgresql") else {}
        _engine = create_engine(
            settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True,
            connect_args=connect_args,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Used by tests that point the app at a different database."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:  # noqa: N802 - factory with the conventional name
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _lock_key(name: str) -> int:
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


@contextlib.contextmanager
def rider_lock(rider_id: str, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialise all money-affecting work for one rider across processes.

    Uses a Postgres session-level advisory lock held on a dedicated connection, so the
    work inside can commit in its own sessions (traces stay durable even if a later
    step fails).
    """
    engine = get_engine()
    conn = engine.connect()
    key = _lock_key(f"rider:{rider_id}")
    try:
        conn.execute(text(f"SET lock_timeout = '{int(timeout_seconds * 1000)}ms'"))
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        conn.commit()
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    engine = get_engine()
    with engine.begin() as conn:
        # Serialise schema creation if several app replicas start at once.
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key("schema-init")})
        Base.metadata.create_all(bind=conn)
