"""State transition dispatch table for :class:`DebateStateMachine`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from debate.orchestration.state_machine import (
    _MAX_VERDICT_RETRIES,
    _TURN_STATES,
    Event,
    State,
)

if TYPE_CHECKING:
    from debate.orchestration.state_machine import DebateStateMachine


def dispatch_state(fsm: DebateStateMachine, ev: Event, _data: Any) -> State | None:
    """Return the next state for ``(fsm.state, ev)`` without assigning it."""
    s = fsm.state
    if s is State.INIT:
        return State.SPAWNING if ev is Event.START else None

    if s is State.SPAWNING:
        if ev is Event.CHILDREN_READY:
            return State.OPENING
        if ev is Event.SPAWN_FAILED:
            return State.ABORT
        return None

    if s is State.OPENING:
        return State.PRO_TURN if ev is Event.SENT_OPENINGS else None

    if s is State.PRO_TURN:
        if ev is Event.PRO_REPLY:
            return State.SCORE_PRO
        if ev is Event.HEARTBEAT_MISS:
            fsm.last_missed_role = "pro"
            fsm.remembered_turn_state = State.PRO_TURN
            return State.RECOVER
        return None

    if s is State.SCORE_PRO:
        return State.CON_TURN if ev is Event.SCORED else None

    if s is State.CON_TURN:
        if ev is Event.CON_REPLY:
            return State.SCORE_CON
        if ev is Event.HEARTBEAT_MISS:
            fsm.last_missed_role = "con"
            fsm.remembered_turn_state = State.CON_TURN
            return State.RECOVER
        return None

    if s is State.SCORE_CON:
        if ev is Event.SCORED:
            fsm.current_round += 1
            return State.NEXT_ROUND
        return None

    if s is State.NEXT_ROUND:
        if ev is Event.ROUND_LIMIT_REACHED:
            return State.CLOSING
        if ev is Event.SCORED:
            return State.PRO_TURN
        return None

    if s is State.CLOSING:
        return State.VERDICT if ev is Event.CLOSINGS_RECEIVED else None

    if s is State.VERDICT:
        return State.VALIDATE_VERDICT if ev is Event.JUDGE_REPLY else None

    if s is State.VALIDATE_VERDICT:
        if ev is Event.VALID_NON_TIE:
            return State.DONE
        if ev is Event.INVALID_OR_TIE:
            if fsm.verdict_retry_count < _MAX_VERDICT_RETRIES:
                fsm.verdict_retry_count += 1
                return State.VERDICT
            return State.TIE_BREAK
        return None

    if s is State.TIE_BREAK:
        return State.DONE if ev is Event.JUDGE_REPLY else None

    if s is State.RECOVER:
        if ev is Event.RESPAWNED:
            target = fsm.remembered_turn_state
            if target is None or target not in _TURN_STATES:
                return None
            fsm.remembered_turn_state = None
            fsm.last_missed_role = None
            return target
        if ev is Event.RESTARTS_EXHAUSTED:
            return State.ABORT
        return None

    return None
