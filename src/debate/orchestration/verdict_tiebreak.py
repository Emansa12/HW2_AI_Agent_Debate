"""Verdict score tie-break and cumulative winner resolution."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from debate.orchestration.judge_types import VALID_ROLE_STRINGS, TurnRecord
from debate.sdk.schemas import Verdict

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def transcript_fingerprint(turns: list[TurnRecord]) -> str:
    """Stable digest input for tie-break when cumulative scores are equal."""
    return "|".join(f"{r.role}:{r.phase.value}:{r.round_number}:{r.content}" for r in turns)


def hash_pick_winner(transcript_blob: str) -> str:
    """Pick pro or con from transcript content (50/50 over varied debates)."""
    digest = hashlib.sha256(transcript_blob.encode("utf-8")).hexdigest()
    return "pro" if int(digest, 16) % 2 == 0 else "con"


def resolve_cumulative_winner(
    judge: Judge,
    *,
    cumulative: dict[str, int] | None = None,
) -> tuple[str, str]:
    """Return ``(winner, reason)`` from per-turn cumulative scores."""
    totals = cumulative if cumulative is not None else judge.history.cumulative_scores
    pro = int(totals.get("pro", 0))
    con = int(totals.get("con", 0))
    if pro > con:
        return "pro", "cumulative_higher"
    if con > pro:
        return "con", "cumulative_higher"
    winner = hash_pick_winner(transcript_fingerprint(judge.history.turns))
    return winner, "cumulative_equal_content_hash"


def verdict_scores(verdict: Verdict) -> dict[str, int]:
    extra = verdict.model_extra or {}
    scores = extra.get("scores")
    if not isinstance(scores, dict):
        return {"pro": 0, "con": 0}
    return {"pro": int(scores.get("pro", 0)), "con": int(scores.get("con", 0))}


def resolve_tiebreak_winner(
    judge: Judge,
    *,
    preferred_winner: str | None,
    cumulative: dict[str, int] | None = None,
) -> str:
    """Pick a side when visible verdict scores are tied."""
    winner, _reason = resolve_cumulative_winner(judge, cumulative=cumulative)
    if preferred_winner in VALID_ROLE_STRINGS and preferred_winner == winner:
        return preferred_winner
    return winner


def adjust_scores_for_winner(scores: dict[str, int], winner: str) -> dict[str, int]:
    """Ensure the selected winner has a strictly higher visible score."""
    pro = int(scores["pro"])
    con = int(scores["con"])
    if pro != con:
        return {"pro": pro, "con": con}
    if winner == "pro":
        return {"pro": pro + 1, "con": con}
    return {"pro": pro, "con": con + 1}


def rebuild_verdict(
    verdict: Verdict,
    *,
    winner: str,
    scores: dict[str, int],
) -> Verdict:
    extra = dict(verdict.model_extra or {})
    reasons = extra.get("reasons")
    return Verdict(
        winner=winner,
        rationale=verdict.rationale,
        scores=scores,
        reasons=reasons if isinstance(reasons, list) else None,
    )


def finalize_verdict(judge: Judge, verdict: Verdict) -> tuple[Verdict, bool, str | None]:
    """Align winner and visible scores with cumulative debate performance."""
    auth_winner, reason = resolve_cumulative_winner(judge)
    cumulative = judge.history.cumulative_scores
    display = adjust_scores_for_winner(
        {"pro": int(cumulative.get("pro", 0)), "con": int(cumulative.get("con", 0))},
        auth_winner,
    )
    llm_scores = verdict_scores(verdict)
    changed = (
        auth_winner != verdict.winner
        or display["pro"] != llm_scores["pro"]
        or display["con"] != llm_scores["con"]
    )
    if not changed:
        return verdict, False, None
    return rebuild_verdict(verdict, winner=auth_winner, scores=display), True, reason


def apply_tie_breaker(scores: dict[str, int], *, transcript_blob: str = "") -> Verdict:
    """Deterministic fallback verdict when the LLM verdict pipeline fails."""
    pro = int(scores.get("pro", 0))
    con = int(scores.get("con", 0))
    if pro > con:
        winner = "pro"
        rationale = f"tie-break: pro cumulative score {pro} > con {con}"
    elif con > pro:
        winner = "con"
        rationale = f"tie-break: con cumulative score {con} > pro {pro}"
    else:
        winner = hash_pick_winner(transcript_blob)
        rationale = (
            f"tie-break: cumulative scores equal at {pro}; {winner} wins from transcript hash"
        )
    final_scores = adjust_scores_for_winner({"pro": pro, "con": con}, winner)
    return Verdict(
        winner=winner,
        rationale=rationale,
        scores=final_scores,
        reasons=[
            "Verdict pipeline exhausted retries; cumulative tie-break applied.",
            rationale,
            "Tie-break rule: higher cumulative score wins; on exact ties use transcript hash.",
        ],
    )
