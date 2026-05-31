"""Structural redaction of sensitive fields.

Used by `debate.shared.logger.RunLogger` to scrub credentials before
records hit the transcript on disk. Pure function, no IO.

A key is considered sensitive (case-insensitively) if it *contains*
any of `SENSITIVE_KEY_TOKENS` as a substring, so `openai_api_key`,
`Authorization`, `user_password`, `auth_token`, and `client_secret`
are all redacted, not just exact matches.
"""

from __future__ import annotations

from typing import Any

SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
)
"""Substrings (case-insensitive) that mark a dict key as sensitive."""

REDACTION_PLACEHOLDER: str = "<redacted>"
"""String written in place of any sensitive value."""


def is_sensitive_key(key: str) -> bool:
    """Return True if `key` should be redacted.

    Matching is case-insensitive and substring-based.
    """
    low = key.lower()
    return any(tok in low for tok in SENSITIVE_KEY_TOKENS)


def redact(value: Any) -> Any:
    """Return a deep-copy of `value` with sensitive fields replaced.

    Recurses through ``dict`` (keys matched, values redacted),
    ``list``, and ``tuple``. Scalars and other types are returned
    unchanged. The input is never mutated.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTION_PLACEHOLDER if is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
