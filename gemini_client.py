"""Lazy Gemini client wrapper using google.genai.

This module intentionally performs no network calls during import.
"""

from __future__ import annotations

import os
from typing import Any

_DEFAULT_MODEL = "gemini-2.5-flash"
_client: Any | None = None


def _get_client() -> Any:
    """Lazily instantiate and cache a google.genai client."""
    global _client
    if _client is not None:
        return _client

    from google import genai

    _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client


def get_model_name(model: str | None = None) -> str:
    """Resolve model name preserving GEMINI_MODEL env behavior."""
    if model is not None:
        return model
    return os.getenv("GEMINI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL


def generate_text(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    response_mime_type: str | None = None,
    system_instruction: str | None = None,
    timeout: float | None = None,
) -> str:
    """Generate plain text from Gemini.

    Raises provider/client exceptions to let callers preserve existing fallback behavior.
    """
    client = _get_client()

    config: dict[str, Any] = {}
    if temperature is not None:
        config["temperature"] = temperature
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    if response_mime_type is not None:
        config["response_mime_type"] = response_mime_type
    if system_instruction is not None:
        config["system_instruction"] = system_instruction

    kwargs: dict[str, Any] = {
        "model": get_model_name(model),
        "contents": prompt,
        "config": config or None,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout

    response = client.models.generate_content(**kwargs)

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return str(text or "")
