"""Verdict generation, logging fields, and retry loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from debate.orchestration.judge_types import InvalidVerdictError
from debate.orchestration.state_machine import Event, State
from debate.orchestration.verdict_parse import (
    build_verdict_prompt,
    parse_verdict,
    validate_verdict,
)
from debate.orchestration.verdict_tiebreak import (
    apply_tie_breaker,
    finalize_verdict,
    transcript_fingerprint,
)
from debate.sdk.schemas import Verdict
from debate.shared.gatekeeper import BudgetExceededError

__all__ = [
    "apply_tie_breaker",
    "finalize_verdict",
    "generate_verdict",
    "validate_verdict",
    "verdict_log_fields",
    "verdict_with_retry",
]

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def generate_verdict(judge: Judge) -> Verdict:
    """Ask the LLM (under Gatekeeper budget) for a verdict and parse it."""
    from debate.orchestration.judge_logging import log

    prompt = build_verdict_prompt(judge)
    try:
        response = judge._gatekeeper.call_llm(
            judge._llm,
            prompt=prompt,
            max_tokens=judge._max_tokens_per_turn,
        )
    except BudgetExceededError:
        raise
    text = response.text
    log(
        judge,
        "verdict_llm_response",
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        text_length=len(text),
        verdict_text=text,
    )
    return parse_verdict(text)


def verdict_log_fields(verdict: Verdict) -> dict[str, Any]:
    from debate.shared.transcript_log import format_transcript_dict

    extra = verdict.model_extra or {}
    reasons = extra.get("reasons")
    reasons_list = reasons if isinstance(reasons, list) else []
    payload: dict[str, Any] = {
        "winner": verdict.winner,
        "scores": extra.get("scores"),
        "reasons": reasons_list,
        "reasons_count": len(reasons_list),
        "rationale": verdict.rationale,
    }
    payload["verdict_text"] = format_transcript_dict(
        {
            "winner": verdict.winner,
            "scores": extra.get("scores"),
            "reasons": reasons_list,
            "rationale": verdict.rationale,
        }
    )
    return payload


def verdict_with_retry(judge: Judge) -> Verdict:
    from debate.orchestration.judge_logging import log, log_verdict_record

    attempts = 0
    while True:
        attempts += 1
        try:
            verdict = generate_verdict(judge)
            validate_verdict(verdict)
            verdict, tiebreak_applied, tiebreak_reason = finalize_verdict(judge, verdict)
            judge._fsm.transition(Event.JUDGE_REPLY)
            judge._fsm.transition(Event.VALID_NON_TIE)
            log_verdict_record(
                judge,
                verdict,
                attempt=attempts,
                source="llm",
                verdict_tiebreak_applied=tiebreak_applied,
                tiebreak_reason=tiebreak_reason,
            )
            return verdict
        except InvalidVerdictError as exc:
            log(judge, "verdict_invalid", attempt=attempts, reason=str(exc))
            judge._fsm.transition(Event.JUDGE_REPLY)
            next_state = judge._fsm.transition(Event.INVALID_OR_TIE)
            if next_state is State.VERDICT:
                continue
            if next_state is State.TIE_BREAK:
                cumulative = judge.history.cumulative_scores
                tied = apply_tie_breaker(
                    cumulative,
                    transcript_blob=transcript_fingerprint(judge.history.turns),
                )
                judge._fsm.transition(Event.JUDGE_REPLY)
                pro = int(cumulative.get("pro", 0))
                con = int(cumulative.get("con", 0))
                scores_were_equal = pro == con
                log_verdict_record(
                    judge,
                    tied,
                    attempt=attempts,
                    source="tie_break",
                    verdict_tiebreak_applied=scores_were_equal,
                    tiebreak_reason="cumulative_scores_equal" if scores_were_equal else None,
                )
                return tied
            raise InvalidVerdictError(
                f"unexpected FSM state after invalid verdict: {next_state}"
            ) from exc
