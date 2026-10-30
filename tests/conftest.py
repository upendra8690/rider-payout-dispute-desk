"""Test fixtures.

Integration tests use a real Postgres (TEST_DATABASE_URL) and the real PaySwift sandbox binary
(started here with chaos off and a fresh in-memory ledger for every test that needs it).
Failure modes (503 / 504 / timeouts / 409 / 429) are tested with a scripted fake transport in
test_payments.py so they are deterministic.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAYSWIFT_PORT = int(os.environ.get("TEST_PAYSWIFT_PORT", "18081"))
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://qd:qd@localhost:5432/dispute_desk_test")
os.environ["PAYSWIFT_BASE_URL"] = f"http://127.0.0.1:{PAYSWIFT_PORT}"
os.environ["OPS_API_TOKEN"] = "test-token"
os.environ["LLM_PROVIDER"] = "none"
os.environ.setdefault("LOG_LEVEL", "WARNING")
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, get_engine, init_db, session_scope  # noqa: E402
from app.services.seed import seed_reference_data  # noqa: E402

DYNAMIC_TABLES = ("trace_steps", "audit_logs", "payment_attempts", "payment_transactions", "ops_items",
                  "disputes", "messages", "conversations")


def _payswift_python() -> str | None:
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    return shutil.which("python3.12")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Sandbox:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.python = _payswift_python()

    def start(self) -> None:
        self.stop()
        if not self.python:
            pytest.skip("PaySwift sandbox needs Python 3.12 (server.pyc)")
        env = {**os.environ, "PORT": str(PAYSWIFT_PORT), "PROVIDER_CHAOS": "off"}
        self.proc = subprocess.Popen([self.python, "-u", "server.pyc"], cwd=ROOT / "payswift", env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if _port_open(PAYSWIFT_PORT):
                return
            time.sleep(0.05)
        raise RuntimeError("PaySwift sandbox did not start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc = None
        for _ in range(50):
            if not _port_open(PAYSWIFT_PORT):
                break
            time.sleep(0.05)


@pytest.fixture(scope="session")
def database():
    get_settings.cache_clear()
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment problem
        pytest.skip(f"test database not reachable: {exc}")
    Base.metadata.drop_all(engine)
    init_db()
    with session_scope() as s:
        seed_reference_data(s, get_settings().data_dir, force=True)
    yield engine


@pytest.fixture()
def clean_db(database):
    with database.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(DYNAMIC_TABLES)} RESTART IDENTITY CASCADE"))
    yield database


_sandbox = Sandbox()


@pytest.fixture()
def payswift():
    """A fresh PaySwift sandbox (empty ledger, chaos off) for this test."""
    _sandbox.start()
    yield f"http://127.0.0.1:{PAYSWIFT_PORT}"


def pytest_sessionfinish(session, exitstatus):
    _sandbox.stop()


@pytest.fixture()
def settings():
    return get_settings()
