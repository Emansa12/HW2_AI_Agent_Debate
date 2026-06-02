"""Judge debate flow + verdict pipeline (Stage 9)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from debate.orchestration import judge_phases as phases
from debate.orchestration import judge_scoring, judge_turns
from debate.orchestration import verdict_pipeline as vp
from debate.orchestration.judge_types import (
    ALLOWED_TOOL_RESULT_ERRORS,
    DEFAULT_PER_TURN_TIMEOUT_SEC,
    DEFAULT_RECEIVE_MAX_ITERS,
    DEFAULT_VERDICT_MAX_TOKENS,
    MIN_VERDICT_REASONS,
    DebateHistory,
    InvalidReplyError,
    InvalidVerdictError,
    JudgeError,
    TurnRecord,
)
from debate.orchestration.state_machine import DebateStateMachine
from debate.orchestration.supervisor import Supervisor
from debate.sdk.llm_client import LLMClient
from debate.sdk.schemas import Message, Phase, Verdict
from debate.shared.gatekeeper import Gatekeeper
from debate.shared.router import ToolRouter
from debate.shared.transcript_log import DEFAULT_MAX_LOGGED_TEXT_CHARS

__all__ = [
    "ALLOWED_TOOL_RESULT_ERRORS",
    "DEFAULT_PER_TURN_TIMEOUT_SEC",
    "DEFAULT_RECEIVE_MAX_ITERS",
    "DEFAULT_VERDICT_MAX_TOKENS",
    "MIN_VERDICT_REASONS",
    "DebateHistory",
    "InvalidReplyError",
    "InvalidVerdictError",
    "Judge",
    "JudgeError",
    "TurnRecord",
]


class Judge:
    """Parent-side debate controller."""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        fsm: DebateStateMachine,
        router: ToolRouter,
        gatekeeper: Gatekeeper,
        llm_client: LLMClient,
        logger: Any = None,
        motion: str = "",
        max_tokens_per_turn: int | None = None,
        per_turn_timeout_sec: float = DEFAULT_PER_TURN_TIMEOUT_SEC,
        receive_max_iters: int = DEFAULT_RECEIVE_MAX_ITERS,
        max_logged_text_chars: int = DEFAULT_MAX_LOGGED_TEXT_CHARS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if per_turn_timeout_sec <= 0:
            raise ValueError("per_turn_timeout_sec must be > 0")
        if receive_max_iters < 1:
            raise ValueError("receive_max_iters must be >= 1")
        if max_logged_text_chars < 256:
            raise ValueError("max_logged_text_chars must be >= 256")
        self._supervisor, self._fsm, self._router = supervisor, fsm, router
        self._gatekeeper, self._llm, self._logger = gatekeeper, llm_client, logger
        self._motion = motion
        self._per_turn_timeout_sec = float(per_turn_timeout_sec)
        self._receive_max_iters = int(receive_max_iters)
        self._max_logged_text_chars = int(max_logged_text_chars)
        self._clock = clock
        if max_tokens_per_turn is None:
            max_tokens_per_turn = min(
                getattr(gatekeeper.policy, "max_tokens_per_turn", DEFAULT_VERDICT_MAX_TOKENS),
                DEFAULT_VERDICT_MAX_TOKENS,
            )
        self._max_tokens_per_turn = int(max_tokens_per_turn)
        self._outgoing_turn_id = 0
        self.history = DebateHistory(motion=motion)

    @property
    def fsm(self) -> DebateStateMachine:
        return self._fsm

    @property
    def cumulative_scores(self) -> dict[str, int]:
        return dict(self.history.cumulative_scores)

    def run_debate(self, motion: str | None = None, rounds: int | None = None) -> Verdict:
        return phases.run_debate(self, motion, rounds)

    def build_prompt(
        self,
        role: str,
        phase: Phase,
        context: list[str] | None = None,
        opponent_last: str | None = None,
    ) -> Message:
        return judge_turns.build_prompt(
            self, role, phase, context=context, opponent_last=opponent_last
        )

    def build_init(self, role: str, motion: str) -> Message:
        return judge_turns.build_init(self, role, motion)

    def run_turn(self, role: str, phase: Phase, opponent_last: str | None) -> Message:
        return judge_turns.run_turn(self, role, phase, opponent_last)

    def validate_child_reply(self, message: Message, expected_role: str) -> Message:
        return judge_turns.validate_child_reply(message, expected_role)

    def score_turn(self, role: str, reply: Message, round_number: int) -> dict[str, Any]:
        return judge_scoring.score_turn(role, reply, round_number)

    def generate_verdict(self) -> Verdict:
        return vp.generate_verdict(self)

    def validate_verdict(self, verdict: Verdict) -> None:
        vp.validate_verdict(verdict)

    def apply_tie_breaker(self, scores: dict[str, int], *, transcript_blob: str = "") -> Verdict:
        return vp.apply_tie_breaker(scores, transcript_blob=transcript_blob)

    def _finalize_verdict(self, verdict: Verdict) -> tuple[Verdict, bool, str | None]:
        return vp.finalize_verdict(self, verdict)
