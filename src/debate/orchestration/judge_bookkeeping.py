"""Turn IDs, outgoing envelopes, and turn recording."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from debate.orchestration import judge_scoring
from debate.orchestration.judge_logging import log
from debate.orchestration.judge_types import TurnRecord
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Phase, Role

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def next_turn_id(judge: Judge) -> int:
    tid = judge._outgoing_turn_id
    judge._outgoing_turn_id += 1
    return tid


def make_judge_message(judge: Judge, type_: MessageType, payload: dict[str, Any]) -> Message:
    msg = Message(
        v=SCHEMA_VERSION,
        ts=float(judge._clock()),
        turn_id=next_turn_id(judge),
        role=Role.JUDGE,
        type=type_,
        payload=dict(payload),
    )
    return msg


def record_turn(
    judge: Judge,
    role: str,
    phase: Phase,
    round_number: int,
    reply: Message,
) -> TurnRecord:
    score_payload = judge_scoring.score_turn(role, reply, round_number)
    record = TurnRecord(
        role=role,
        phase=phase,
        round_number=round_number,
        content=str(reply.payload.get("content", "")),
        score=int(score_payload["score"]),
        tokens_in=int(score_payload["tokens_in"]),
        tokens_out=int(score_payload["tokens_out"]),
    )
    judge.history.add(record)
    log(
        judge,
        "score_recorded",
        target_role=role,
        phase=phase.value,
        round=round_number,
        score=record.score,
        cumulative_pro=judge.history.cumulative_scores.get("pro", 0),
        cumulative_con=judge.history.cumulative_scores.get("con", 0),
    )
    return record
