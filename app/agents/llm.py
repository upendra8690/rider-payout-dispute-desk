"""Provider-agnostic LLM client (plain HTTPS, no SDKs). Returns validated JSON or raises LLMError."""

from __future__ import annotations

import json
import re
import time
from datetime import date

import httpx

from app.agents.prompts import build_messages
from app.config import Settings
from app.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-haiku-5-5",
}
DEFAULT_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
}


class LLMError(Exception):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("model did not return a JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON from model: {exc}") from exc


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.provider = settings.llm_provider
        self.model = settings.llm_model or DEFAULT_MODELS.get(self.provider, "")
        self.base_url = (settings.llm_base_url or DEFAULT_URLS.get(self.provider, "")).rstrip("/")
        self._transport = transport

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def interpret(self, text: str, today: date, context: dict) -> dict:
        if not self.settings.llm_enabled:
            raise LLMError("LLM disabled")
        system, user = build_messages(text, today, context)
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.settings.llm_timeout_seconds, transport=self._transport) as client:
                content = getattr(self, f"_call_{self.provider}")(client, system, user)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM timeout after {self.settings.llm_timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM transport error: {exc}") from exc
        finally:
            log.info("llm call", extra={"provider": self.provider, "model": self.model,
                                        "ms": int((time.monotonic() - started) * 1000)})
        return _extract_json(content)

    # OpenAI-compatible (Groq, OpenAI, or LLM_BASE_URL pointing at any compatible server)
    def _openai_like(self, client: httpx.Client, system: str, user: str) -> str:
        r = client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            json={
                "model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        if r.status_code != 200:
            raise LLMError(f"{self.provider} http {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    _call_groq = _openai_like
    _call_openai = _openai_like

    def _call_gemini(self, client: httpx.Client, system: str, user: str) -> str:
        r = client.post(
            f"{self.base_url}/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.settings.llm_api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
            },
        )
        if r.status_code != 200:
            raise LLMError(f"gemini http {r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected gemini response: {str(data)[:200]}") from exc

    def _call_anthropic(self, client: httpx.Client, system: str, user: str) -> str:
        r = client.post(
            f"{self.base_url}/messages",
            headers={"x-api-key": self.settings.llm_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.model, "max_tokens": 800, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        if r.status_code != 200:
            raise LLMError(f"anthropic http {r.status_code}: {r.text[:200]}")
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
