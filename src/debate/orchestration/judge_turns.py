"""Per-turn prompt/reply loop and child reply validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from debate.orchestration import judge_bookkeeping as bk
from debate.orchestration.judge_logging import log
from debate.orchestration.judge_tool_calls import handle_tool_call
from debate.orchestration.judge_types import VALID_ROLE_STRINGS, InvalidReplyError
from debate.orchestration.supervisor import (
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    SupervisorError,
)
from debate.sdk.schemas import Message, MessageType, Phase, Role
from debate.shared.transcript_log import format_transcript_dict

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def build_prompt(
    judge: Judge,
    role: str,
    phase: Phase,
    context: list[str] | None = None,
    opponent_last: str | None = None,
) -> Message:
    """Build a ``prompt`` envelope to send to ``role``."""
    if role not in VALID_ROLE_STRINGS:
        raise ValueError(f"invalid role for prompt: {role!r}")
    payload: dict[str, Any] = {
        "phase": phase.value,
        "round": judge._fsm.current_round,
    }
    if opponent_last is not None:
        if not isinstance(opponent_last, str):
            raise TypeError("opponent_last must be a string (content only, not a Message)")
        payload["opponent_last"] = opponent_last
    if context:
        payload["selected_context"] = list(context)
    return bk.make_judge_message(judge, MessageType.PROMPT, payload)


def build_init(judge: Judge, role: str, motion: str) -> Message:
    """Build the per-side ``init`` envelope (stance + motion)."""
    if role not in VALID_ROLE_STRINGS:
        raise ValueError(f"invalid role for init: {role!r}")
    payload: dict[str, Any] = {
        "stance": role,
        "motion": motion,
        "max_tokens": judge._max_tokens_per_turn,
    }
    return bk.make_judge_message(judge, MessageType.INIT, payload)


def validate_child_reply(message: Message, expected_role: str) -> Message:
    """Validate a ``reply`` message and return it unchanged."""
    if expected_role not in VALID_ROLE_STRINGS:
        raise InvalidReplyError(f"invalid expected_role: {expected_role!r}")
    expected = Role(expected_role)
    if message.role is not expected:
        raise InvalidReplyError(
            f"sender role mismatch: expected {expected.value!r}, got {message.role.value!r}"
        )
    if message.type is not MessageType.REPLY:
        raise InvalidReplyError(
            f"expected message type 'reply', got {message.type.value!r} from {expected_role!r}"
        )
    content = message.payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InvalidReplyError(f"reply from {expected_role!r} has empty content")
    stance = message.payload.get("stance")
    if stance is not None and stance != expected_role:
        raise InvalidReplyError(f"stance {stance!r} does not match role {expected_role!r}")
    return message


def run_turn(judge: Judge, role: str, phase: Phase, opponent_last: str | None) -> Message:
    """Send a prompt to ``role`` and pull back exactly one reply."""
    prompt = build_prompt(judge, role, phase, opponent_last=opponent_last)
    judge._supervisor.send(role, prompt)
    prompt_payload = dict(prompt.payload)
    prompt_text = format_transcript_dict(prompt_payload)
    log(
        judge,
        "prompt_sent",
        target_role=role,
        phase=phase.value,
        round=judge._fsm.current_round,
        prompt_turn_id=prompt.turn_id,
        prompt_payload=prompt_payload,
        prompt_text=prompt_text,
        prompt_length=len(prompt_text),
    )

    for _ in range(judge._receive_max_iters):
        try:
            msg = judge._supervisor.receive(role, timeout=judge._per_turn_timeout_sec)
        except (ChildReceiveTimeoutError, ChildStreamClosedError) as exc:
            log(
                judge,
                "turn_failed",
                target_role=role,
                error=type(exc).__name__,
                phase=phase.value,
            )
            raise
        except SupervisorError as exc:
            log(
                judge,
                "turn_failed",
                target_role=role,
                error=type(exc).__name__,
                phase=phase.value,
            )
            raise

        if msg.type is MessageType.TOOL_CALL:
            handle_tool_call(judge, role, msg)
            continue
        if msg.type is MessageType.REPLY:
            validate_child_reply(msg, expected_role=role)
            content = msg.payload.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            log(
                judge,
                "reply_received",
                target_role=role,
                phase=phase.value,
                round=judge._fsm.current_round,
                reply_turn_id=msg.turn_id,
                content=content,
                content_length=len(content),
            )
            return msg

        raise InvalidReplyError(f"unexpected message type from {role!r}: {msg.type.value}")

    raise InvalidReplyError(
        f"too many tool_call iterations from {role!r} (max {judge._receive_max_iters})"
    )
