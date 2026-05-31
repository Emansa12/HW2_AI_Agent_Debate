"""Deterministic debate state machine (Stage 5).

This module is intentionally **pure**:

- no file writes;
- no API / network calls;
- no subprocess calls;
- no imports from `debate.sdk.llm_client`, `debate.sdk.search_client`,
  `debate.shared.gatekeeper`, `debate.shared.router`, or anything
  related to the Supervisor / Watchdog / Agent layers.

It only knows about states and events. The orchestrator (added in a
later stage) will drive it by calling `transition(event)`, and the
FSM mutates its own bookkeeping fields (`current_round`,
`verdict_retry_count`, `last_missed_role`, `remembered_turn_state`)
in a strictly event-driven way.

Normal happy flow for `N` rounds::

    INIT --start--> SPAWNING --children_ready--> OPENING
    OPENING --sent_openings--> PRO_TURN

    [ for each round r in 1..N: ]
        PRO_TURN  --pro_reply--> SCORE_PRO  --scored--> CON_TURN
        CON_TURN  --con_reply--> SCORE_CON  --scored--> NEXT_ROUND
        if r < N: NEXT_ROUND --scored--> PRO_TURN
        if r == N: NEXT_ROUND --round_limit_reached--> CLOSING

    CLOSING --closings_received--> VERDICT
    VERDICT --judge_reply--> VALIDATE_VERDICT
    VALIDATE_VERDICT --valid_non_tie--> DONE

Failure / recovery::

    SPAWNING --spawn_failed--> ABORT
    PRO_TURN / CON_TURN --heartbeat_miss--> RECOVER
        (remembers the turn it came from and which role timed out)
    RECOVER --respawned--> <remembered turn>
    RECOVER --restarts_exhausted--> ABORT

Verdict retry::

    VALIDATE_VERDICT --invalid_or_tie (1st)--> VERDICT  (retry once)
    VALIDATE_VERDICT --invalid_or_tie (2nd)--> TIE_BREAK
    TIE_BREAK --judge_reply--> DONE

Budget::

    <any non-terminal> --budget_exhausted--> ABORT

`DONE` and `ABORT` are terminal; any further `transition` call from
them raises `IllegalTransitionError`.

The `scored` event is overloaded with two well-defined meanings:

- From `SCORE_PRO` and `SCORE_CON` it means "this side's score has
  been recorded".
- From `NEXT_ROUND` it means "begin the next round" (the orchestrator
  emits it explicitly when `current_round < max_rounds`).

If the orchestrator instead emits `round_limit_reached` from
`NEXT_ROUND`, the FSM advances to `CLOSING`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class State(StrEnum):
    """All debate states. `DONE` and `ABORT` are terminal."""

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
    """Events the orchestrator may emit."""

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
"""How many times an `invalid_or_tie` triggers a retry before
escalating to `TIE_BREAK`. 1 means: original attempt + 1 retry,
then tie-break.
"""


class IllegalTransitionError(RuntimeError):
    """Raised when no transition is defined for `(state, event)`,
    or when the FSM is already terminal.
    """

    def __init__(self, state: State, event: Event | str) -> None:
        super().__init__(f"illegal transition: state={state.value!r} event={str(event)!r}")
        self.state: State = state
        self.event: Event | str = event


@dataclass
class DebateStateMachine:
    """Pure event-driven debate state machine.

    Only mutates its own dataclass fields - never the outside world.
    """

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
        """Apply an event and return the new state.

        Raises `IllegalTransitionError` if the transition is not
        defined for the current `(state, event)` pair or if the FSM
        is already terminal.

        The `data` parameter is accepted for future expansion (and
        for symmetry with typical FSM APIs) but is not used by any
        Stage 5 transition.
        """
        if self.is_terminal():
            raise IllegalTransitionError(self.state, event)

        ev = self._normalize_event(event)

        # Budget exhaustion is a universal short-circuit to ABORT
        # from every non-terminal state.
        if ev is Event.BUDGET_EXHAUSTED:
            self.state = State.ABORT
            return self.state

        next_state = self._dispatch(ev, data)
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

    def _dispatch(self, ev: Event, _data: Any) -> State | None:
        s = self.state
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
                self.last_missed_role = "pro"
                self.remembered_turn_state = State.PRO_TURN
                return State.RECOVER
            return None

        if s is State.SCORE_PRO:
            return State.CON_TURN if ev is Event.SCORED else None

        if s is State.CON_TURN:
            if ev is Event.CON_REPLY:
                return State.SCORE_CON
            if ev is Event.HEARTBEAT_MISS:
                self.last_missed_role = "con"
                self.remembered_turn_state = State.CON_TURN
                return State.RECOVER
            return None

        if s is State.SCORE_CON:
            if ev is Event.SCORED:
                self.current_round += 1
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
                if self.verdict_retry_count < _MAX_VERDICT_RETRIES:
                    self.verdict_retry_count += 1
                    return State.VERDICT
                return State.TIE_BREAK
            return None

        if s is State.TIE_BREAK:
            return State.DONE if ev is Event.JUDGE_REPLY else None

        if s is State.RECOVER:
            if ev is Event.RESPAWNED:
                target = self.remembered_turn_state
                if target is None or target not in _TURN_STATES:
                    return None
                self.remembered_turn_state = None
                self.last_missed_role = None
                return target
            if ev is Event.RESTARTS_EXHAUSTED:
                return State.ABORT
            return None

        # DONE / ABORT are caught by `is_terminal()` above; reaching
        # here means the dispatch table is out of sync with `State`.
        return None
