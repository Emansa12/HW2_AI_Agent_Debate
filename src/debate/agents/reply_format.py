"""Reply line limits and truncation for debater output."""

from __future__ import annotations

MAX_REPLY_LINES: int = 5
"""Maximum number of lines allowed in each debater reply."""


def truncate_reply_lines(text: str, *, max_lines: int) -> str:
    """Keep at most ``max_lines`` non-empty lines from a debater reply."""
    if max_lines < 1:
        max_lines = 1
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.strip()
    return "\n".join(lines[:max_lines]).strip()
