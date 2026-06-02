"""Verdict prompt construction, JSON extraction, and validation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from debate.orchestration.judge_types import (
    MIN_VERDICT_REASONS,
    VALID_ROLE_STRINGS,
    InvalidVerdictError,
)
from debate.sdk.schemas import Verdict

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def build_verdict_prompt(judge: Judge) -> str:
    lines: list[str] = [
        "You are the impartial Judge of a debate.",
        f"Motion: {judge.history.motion}",
        "",
        "Cumulative scores (from per-turn performance; higher side led the debate):",
        f"  pro: {judge.history.cumulative_scores.get('pro', 0)}",
        f"  con: {judge.history.cumulative_scores.get('con', 0)}",
        "",
        "Judging rules:",
        "- Pick the side with stronger arguments and evidence, NOT the motion-affirming side by default.",
        "- Do NOT favor Pro because they speak first or defend the motion.",
        "- When cumulative scores differ, the higher-scoring side usually performed better.",
        "",
        "Transcript (most recent first):",
    ]
    for record in reversed(judge.history.turns[-10:]):
        lines.append(
            f"  [{record.role} / {record.phase.value} / round {record.round_number}] "
            f"{record.content[:200]}"
        )
    lines.extend(
        [
            "",
            "Return a JSON object with EXACTLY these fields:",
            '  "winner":   "pro" or "con" (NEVER "tie"),',
            '  "scores":   {"pro": <int>, "con": <int>} (must NOT be equal),',
            f'  "reasons":  list of at least {MIN_VERDICT_REASONS} short strings,',
            '  "rationale": optional one-sentence summary.',
            "Output STRICT JSON only - no prose, no code fences.",
        ]
    )
    return "\n".join(lines)


def extract_json(text: str) -> str:
    """Pull the outermost JSON object out of an LLM response.

    LLMs sometimes wrap JSON in code fences or prose. We accept
    both, but anything still un-parseable bubbles up as
    :class:`InvalidVerdictError`.
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s


def parse_verdict(text: str) -> Verdict:
    body = extract_json(text)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidVerdictError(f"verdict text is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise InvalidVerdictError("verdict JSON root must be an object")
    try:
        return Verdict.model_validate(data)
    except ValidationError as exc:
        raise InvalidVerdictError(f"verdict failed schema validation: {exc.errors()}") from exc


def validate_verdict(verdict: Verdict) -> None:
    """Verify the verdict is structurally complete.

    Beyond the schema (which already forbids ``winner="tie"``),
    the Judge requires a ``scores`` dict with both sides and at
    least :data:`MIN_VERDICT_REASONS` non-empty reason strings.
    """
    if not isinstance(verdict, Verdict):
        raise InvalidVerdictError("validate_verdict requires a Verdict instance")
    if verdict.winner not in VALID_ROLE_STRINGS:
        raise InvalidVerdictError(f"invalid verdict winner: {verdict.winner!r}")
    extra = verdict.model_extra or {}
    scores = extra.get("scores")
    if not isinstance(scores, dict):
        raise InvalidVerdictError("verdict must include a 'scores' dict")
    for side in ("pro", "con"):
        if side not in scores:
            raise InvalidVerdictError(f"verdict scores missing key {side!r}")
        if not isinstance(scores[side], (int, float)) or isinstance(scores[side], bool):
            raise InvalidVerdictError(f"verdict scores[{side!r}] must be numeric")
    reasons = extra.get("reasons")
    if not isinstance(reasons, list) or len(reasons) < MIN_VERDICT_REASONS:
        raise InvalidVerdictError(f"verdict must include at least {MIN_VERDICT_REASONS} reasons")
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            raise InvalidVerdictError("each reason must be a non-empty string")
