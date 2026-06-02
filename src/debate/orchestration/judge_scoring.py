"""Deterministic per-turn scoring."""

from __future__ import annotations

from typing import Any

from debate.orchestration.judge_types import VALID_ROLE_STRINGS
from debate.sdk.schemas import Message

_REBUTTAL_MARKERS: tuple[str, ...] = (
    "opponent",
    "in response",
    "previous point",
    "my opponent argued",
)


def score_turn(role: str, reply: Message, round_number: int) -> dict[str, Any]:
    """Score a single child reply and return a structured score payload.

    Uses content length plus quality signals (citations, rebuttals) so
    either side can lead when debate performance differs across runs.
    """
    if role not in VALID_ROLE_STRINGS:
        raise ValueError(f"invalid role for score_turn: {role!r}")
    content = reply.payload.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    stripped = content.strip()
    base = 1
    length_bonus = min(len(stripped) // 50, 6)
    citation_bonus = min(stripped.lower().count("http"), 2)
    lower = stripped.lower()
    rebuttal_bonus = 1 if any(marker in lower for marker in _REBUTTAL_MARKERS) else 0
    score = min(base + length_bonus + citation_bonus + rebuttal_bonus, 12)
    return {
        "role": role,
        "round": round_number,
        "score": score,
        "content_length": len(content),
        "tokens_in": int(reply.payload.get("tokens_in", 0) or 0),
        "tokens_out": int(reply.payload.get("tokens_out", 0) or 0),
    }
