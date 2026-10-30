"""Application settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Infrastructure
    database_url: str = Field(
        default="postgresql+psycopg://qd:qd@localhost:5432/dispute_desk",
        description="SQLAlchemy URL for Postgres",
    )
    payswift_base_url: str = "http://localhost:8081"
    data_dir: Path = PROJECT_ROOT / "data"
    log_level: str = "INFO"

    # LLM (interpretation only; money decisions are deterministic Python)
    llm_provider: str = Field(default="none", description="none | groq | gemini | openai | anthropic")
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_seconds: float = 4.0

    # Ops authentication for mutating / PII endpoints
    ops_api_token: str = "change-me-ops-token"

    # Finance policy knobs (defaults = the note from Finance and docs/policy.md)
    auto_pay_max_per_dispute: int = 200
    auto_pay_max_per_rider_per_day: int = 1
    dispute_window_days: int = 7

    # Latency budget: the vendor retries if we take more than ~10s
    message_budget_seconds: float = 8.0
    payswift_attempt_timeout_seconds: float = 6.0
    payment_max_attempts: int = 12
    worker_poll_seconds: float = 1.0
    conversation_idle_hours: int = 48

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = (v or "none").strip().lower()
        allowed = {"none", "groq", "gemini", "openai", "anthropic"}
        if v not in allowed:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(allowed)}")
        return v

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
