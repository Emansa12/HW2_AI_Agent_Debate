"""Parse OpenAI-compatible Chat Completions JSON responses."""

from __future__ import annotations

from typing import Any

import httpx


def _short_reason(resp: httpx.Response) -> str:
    """Best-effort short reason string for an error response.

    Never returns request headers or the API key.
    """
    reason = resp.reason_phrase or ""
    text = (resp.text or "").strip()
    if text:
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        return f"{reason} - {text}" if reason else text
    return reason or "(no reason)"


def _parse_chat_completion(payload: Any) -> tuple[str, int, int]:
    """Extract ``(text, tokens_in, tokens_out)`` from an OpenAI Chat
    Completions JSON body. Raises :class:`LLMProviderResponseError`
    on any structural problem.
    """
    from debate.sdk.real_llm_client import LLMProviderResponseError

    if not isinstance(payload, dict):
        raise LLMProviderResponseError("upstream JSON root must be an object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderResponseError("upstream JSON missing non-empty 'choices' array")

    first = choices[0]
    if not isinstance(first, dict):
        raise LLMProviderResponseError("choices[0] must be an object")

    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMProviderResponseError("choices[0].message must be an object")

    text = message.get("content")
    if not isinstance(text, str):
        raise LLMProviderResponseError("choices[0].message.content must be a string")

    usage = payload.get("usage")
    if isinstance(usage, dict):
        tokens_in = _coerce_non_negative_int(
            usage.get("prompt_tokens"),
            field="usage.prompt_tokens",
        )
        tokens_out = _coerce_non_negative_int(
            usage.get("completion_tokens"),
            field="usage.completion_tokens",
        )
    else:
        tokens_in, tokens_out = 0, 0

    return text, tokens_in, tokens_out


def _coerce_non_negative_int(value: Any, *, field: str) -> int:
    """Coerce a number-shaped JSON value into a non-negative ``int``."""
    from debate.sdk.real_llm_client import LLMProviderResponseError

    if value is None:
        return 0
    if isinstance(value, bool):
        raise LLMProviderResponseError(f"{field} must be a non-negative integer, got bool")
    if not isinstance(value, (int, float)):
        raise LLMProviderResponseError(f"{field} must be a number, got {type(value).__name__}")
    ivalue = int(value)
    if ivalue < 0:
        raise LLMProviderResponseError(f"{field} must be non-negative, got {ivalue}")
    return ivalue
