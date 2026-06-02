"""Deterministic debate state machine (Stage 5, pure, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class State(StrEnum):
    INIT = "init"
    SPAWNING = "spawning"
    OPENING = "opening"
    PRO_TURN = "pro_turn"
    SCORE_PRO = "score_pro"
    CON_TURN = "con_turn"
    SCORE_CON = "score_con"
    NEXT_ROUND = "next_round"
    CLOSING = "closing"
    VERDICT = "verdict"
    VALIDATE_VERDICT = "validate_verdict"
    TIE_BREAK = "tie_break"
    RECOVER = "recover"
    ABORT = "abort"
    DONE = "done"


class Event(StrEnum):
    START = "start"
    CHILDREN_READY = "children_ready"
    SENT_OPENINGS = "sent_openings"
    PRO_REPLY = "pro_reply"
    CON_REPLY = "con_reply"
    SCORED = "scored"
    ROUND_LIMIT_REACHED = "round_limit_reached"
    CLOSINGS_RECEIVED = "closings_received"
    JUDGE_REPLY = "judge_reply"
    INVALID_OR_TIE = "invalid_or_tie"
    VALID_NON_TIE = "valid_non_tie"
    HEARTBEAT_MISS = "heartbeat_miss"
    RESPAWNED = "respawned"
    RESTARTS_EXHAUSTED = "restarts_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SPAWN_FAILED = "spawn_failed"


_TERMINAL_STATES: frozenset[State] = frozenset({State.DONE, State.ABORT})
_TURN_STATES: frozenset[State] = frozenset({State.PRO_TURN, State.CON_TURN})
_MAX_VERDICT_RETRIES: int = 1


class IllegalTransitionError(RuntimeError):
    def __init__(self, state: State, event: Event | str) -> None:
        super().__init__(f"illegal transition: state={state.value!r} event={str(event)!r}")
        self.state: State = state
        self.event: Event | str = event


@dataclass
class DebateStateMachine:
    """Pure event-driven debate state machine."""

    max_rounds: int = 10
    state: State = State.INIT
    current_round: int = 0
    verdict_retry_count: int = 0
    last_missed_role: str | None = None
    remembered_turn_state: State | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def transition(self, event: Event | str, data: Any = None) -> State:
        if self.is_terminal():
            raise IllegalTransitionError(self.state, event)

        ev = self._normalize_event(event)

        if ev is Event.BUDGET_EXHAUSTED:
            self.state = State.ABORT
            return self.state

        from debate.orchestration.fsm_dispatch import dispatch_state

        next_state = dispatch_state(self, ev, data)
        if next_state is None:
            raise IllegalTransitionError(self.state, ev)

        self.state = next_state
        return self.state

    def _normalize_event(self, event: Event | str) -> Event:
        if isinstance(event, Event):
            return event
        try:
            return Event(event)
        except ValueError as exc:
            raise IllegalTransitionError(self.state, event) from exc
