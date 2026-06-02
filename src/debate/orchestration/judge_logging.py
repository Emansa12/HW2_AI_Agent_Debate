"""Structured run logging helpers."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from debate.sdk.schemas import Verdict
from debate.shared.redaction import redact
from debate.shared.transcript_log import prepare_transcript_field

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge

from debate.orchestration.verdict_pipeline import verdict_log_fields


def log(judge: Judge, event_type: str, **fields: Any) -> None:
    if judge._logger is None:
        return
    safe_fields = redact(prepare_transcript_field(fields, max_chars=judge._max_logged_text_chars))
    with contextlib.suppress(Exception):
        judge._logger.log(
            role="judge",
            turn_id=judge._outgoing_turn_id,
            event_type=event_type,
            **safe_fields,
        )


def log_verdict_record(
    judge: Judge,
    verdict: Verdict,
    *,
    attempt: int,
    source: str,
    verdict_tiebreak_applied: bool = False,
    tiebreak_reason: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "attempt": attempt,
        "source": source,
        **verdict_log_fields(verdict),
    }
    if verdict_tiebreak_applied:
        fields["verdict_tiebreak_applied"] = True
    if tiebreak_reason is not None:
        fields["tiebreak_reason"] = tiebreak_reason
    log(judge, "verdict_recorded", **fields)
