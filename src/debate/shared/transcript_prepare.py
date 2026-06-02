"""Prepare and format transcript log field values."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_LOGGED_TEXT_CHARS: int = 65_536
"""Default cap per string field written into the transcript."""

DEFAULT_MAX_PRINTED_TEXT_CHARS: int = 3000
"""Default cap per answer / long text field in terminal summaries."""

_TRUNCATION_SUFFIX: str = "…[truncated]"


def format_transcript_dict(payload: dict[str, Any]) -> str:
    """Stable, human-readable JSON text for transcript log fields only."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def prepare_transcript_field(value: Any, *, max_chars: int) -> Any:
    """Return a copy of ``value`` safe for transcript logging."""
    if max_chars < 1:
        max_chars = 1
    return _prepare(value, max_chars=max_chars)


def _prepare(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_str(value, max_chars)
    if isinstance(value, dict):
        return {k: _prepare(v, max_chars=max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_prepare(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(_prepare(item, max_chars=max_chars) for item in value)
    return value


def _truncate_str(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = _TRUNCATION_SUFFIX
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)] + suffix
