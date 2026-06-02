"""Judge debate flow + verdict pipeline (Stage 9).

The :class:`Judge` is the parent / central controller. It owns the
debate protocol end-to-end:

- spawns Pro and Con through the :class:`Supervisor`;
- sends every ``init`` / ``prompt`` / ``tool_result`` envelope itself,
  so Pro and Con never communicate directly - every byte that
  reaches a child first passed through the Judge;
- validates every reply coming back from a child (correct sender
  role, correct message type, non-empty content, schema-valid);
- routes ``tool_call`` envelopes through the
  :class:`debate.shared.router.ToolRouter` so search calls remain
  Gatekeeper-bounded and cached;
- scores each turn and keeps cumulative scores per side;
- generates the final verdict, retrying once on invalid output, and
  falling back to a deterministic tie-break otherwise;
- logs every prompt, reply, tool call, score, and verdict via the
  injected :class:`debate.shared.logger.RunLogger` (or any
  duck-typed logger with the same shape).

Stage boundary
--------------

The Judge **does not**:

- import :class:`debate.agents.pro_agent.ProAgent`,
  :class:`debate.agents.con_agent.ConAgent`, or
  :class:`debate.agents.debater_agent.DebaterAgent` (children run as
  subprocesses, the Judge only talks JSONL through the Supervisor);
- implement the full automatic recovery loop (Watchdog ``on_miss``
  is acknowledged via the FSM but no respawn / retry orchestration
  is wired in - that polish ships in Stage 10);
- ship a CLI entry point or transcript writer beyond the
  per-event :meth:`RunLogger.log` calls (Stage 10).
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from debate.orchestration.state_machine import DebateStateMachine, Event, State
from debate.orchestration.supervisor import (
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    Supervisor,
    SupervisorError,
)
from debate.sdk.llm_client import LLMClient
from debate.sdk.schemas import (
    SCHEMA_VERSION,
    Message,
    MessageType,
    Phase,
    Role,
    Verdict,
)
from debate.shared.gatekeeper import BudgetExceededError, Gatekeeper
from debate.shared.redaction import redact
from debate.shared.router import ToolRouter, UnknownToolError
from debate.shared.transcript_log import (
    DEFAULT_MAX_LOGGED_TEXT_CHARS,
    format_transcript_dict,
    prepare_transcript_field,
)

DEFAULT_PER_TURN_TIMEOUT_SEC: float = 30.0
"""Seconds the Judge waits for a single child reply before giving up."""

DEFAULT_RECEIVE_MAX_ITERS: int = 8
"""How many ``tool_call`` rounds the Judge will service inside a
single turn before declaring the child stuck.

A turn ends when the child sends a ``reply``. Each ``tool_call``
the child emits before its ``reply`` consumes one iteration.
"""

DEFAULT_VERDICT_MAX_TOKENS: int = 600
"""Per-call max tokens used when the Judge asks the LLM for a verdict."""

MIN_VERDICT_REASONS: int = 3
"""Minimum number of free-form ``reasons`` strings a verdict must
include to be considered valid."""

ALLOWED_TOOL_RESULT_ERRORS: frozenset[str] = frozenset(
    {"unknown_tool", "invalid_arguments", "budget_exceeded", "tool_error"}
)
"""Error tags the Judge stamps onto a ``tool_result`` payload when
the underlying tool call fails. Symbolic so logs and downstream
consumers can branch on the exact failure mode without parsing
free-form messages."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JudgeError(RuntimeError):
    """Base class for Judge-level errors."""


class InvalidReplyError(JudgeError):
    """Raised when a child reply fails Judge-side validation.

    Examples: wrong sender role, wrong message type, empty content,
    stance mismatch, ``tool_call`` budget exhausted past
    :data:`DEFAULT_RECEIVE_MAX_ITERS`.
    """


class InvalidVerdictError(JudgeError):
    """Raised when a verdict is malformed or fails Judge-side validation.

    Caught by :meth:`Judge.run_debate` to drive the FSM
    ``invalid_or_tie`` retry / tie-break path.
    """


# ---------------------------------------------------------------------------
# Score / history bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class TurnRecord:
    """One per-side turn captured by the Judge while a debate runs.

    Used as the in-memory transcript the verdict prompt is built
    from. Kept deliberately small / serializable - this struct ends
    up logged.
    """

    role: str
    """``"pro"`` or ``"con"``."""

    phase: Phase
    """Which debate phase the turn belongs to."""

    round_number: int
    """``0`` for openings/closings, ``>=1`` for argument rounds."""

    content: str
    """Reply text emitted by the child."""

    score: int
    """Score the Judge assigned to this turn."""

    tokens_in: int = 0
    """Reported input tokens (best-effort, child-supplied)."""

    tokens_out: int = 0
    """Reported output tokens (best-effort, child-supplied)."""


@dataclass
class DebateHistory:
    """Aggregated per-debate state the Judge maintains.

    Exposed as a public attribute on :class:`Judge` so tests can
    inspect cumulative scores, turn order, and recorded contents.
    """

    motion: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    cumulative_scores: dict[str, int] = field(default_factory=lambda: {"pro": 0, "con": 0})
    last_pro: str = ""
    last_con: str = ""

    def add(self, record: TurnRecord) -> None:
        self.turns.append(record)
        self.cumulative_scores[record.role] = (
            self.cumulative_scores.get(record.role, 0) + record.score
        )
        if record.role == "pro":
            self.last_pro = record.content
        elif record.role == "con":
            self.last_con = record.content


# ---------------------------------------------------------------------------
# Helper Protocol surface
# ---------------------------------------------------------------------------

_VALID_ROLE_STRINGS: frozenset[str] = frozenset({"pro", "con"})


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class Judge:
    """Parent-side debate controller.

    Parameters
    ----------
    supervisor:
        The :class:`Supervisor` that owns the Pro / Con child
        processes. The Judge talks to children only through this
        object's ``send`` / ``receive`` methods.
    fsm:
        A :class:`DebateStateMachine`. The Judge drives the FSM by
        emitting the events documented in
        :mod:`debate.orchestration.state_machine`. ``fsm.max_rounds``
        is used as the default round count when ``rounds`` is not
        passed to :meth:`run_debate`.
    router:
        :class:`ToolRouter` used to service ``tool_call`` envelopes
        from children. Stage 9 only knows ``"search"``.
    gatekeeper:
        Used by :meth:`generate_verdict` to call the LLM under the
        usual token / USD / rate-limit policy.
    llm_client:
        LLM used by :meth:`generate_verdict`. Tests inject a
        :class:`debate.sdk.llm_client.FakeLLMClient` configured to
        return JSON-shaped verdict text.
    logger:
        Optional duck-typed structured logger (a
        :class:`debate.shared.logger.RunLogger` works). If ``None``,
        events are silently dropped. Logger errors are swallowed
        defensively so a buggy logger cannot crash a debate.
    motion:
        Optional default motion string. Can be overridden at
        :meth:`run_debate` call time.
    max_tokens_per_turn:
        Per-turn token cap stamped onto child ``init`` envelopes.
        Defaults to the FSM's tokens-per-turn allowance read from
        the gatekeeper policy if available, else
        ``DEFAULT_VERDICT_MAX_TOKENS``.
    per_turn_timeout_sec:
        Seconds the Judge waits for any single child reply before
        treating the turn as failed.
    receive_max_iters:
        Upper bound on ``tool_call`` iterations per turn before the
        Judge gives up and raises :class:`InvalidReplyError`.
    max_logged_text_chars:
        Maximum characters per string field written into the run
        transcript. Longer values are truncated with a suffix.
    clock:
        Injectable epoch-seconds clock for deterministic ``ts``
        fields in outgoing envelopes.

    Notes
    -----
    The Judge never imports :mod:`debate.agents.*`. The whole
    interaction with children is JSONL-on-pipes through the
    Supervisor.
    """

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

        self._supervisor: Supervisor = supervisor
        self._fsm: DebateStateMachine = fsm
        self._router: ToolRouter = router
        self._gatekeeper: Gatekeeper = gatekeeper
        self._llm: LLMClient = llm_client
        self._logger: Any = logger
        self._motion: str = motion
        self._per_turn_timeout_sec: float = float(per_turn_timeout_sec)
        self._receive_max_iters: int = int(receive_max_iters)
        self._max_logged_text_chars: int = int(max_logged_text_chars)
        self._clock: Callable[[], float] = clock

        if max_tokens_per_turn is None:
            max_tokens_per_turn = min(
                getattr(gatekeeper.policy, "max_tokens_per_turn", DEFAULT_VERDICT_MAX_TOKENS),
                DEFAULT_VERDICT_MAX_TOKENS,
            )
        self._max_tokens_per_turn: int = int(max_tokens_per_turn)

        self._outgoing_turn_id: int = 0
        self.history: DebateHistory = DebateHistory(motion=motion)

    # ----- public read-only views ---------------------------------------

    @property
    def fsm(self) -> DebateStateMachine:
        return self._fsm

    @property
    def cumulative_scores(self) -> dict[str, int]:
        return dict(self.history.cumulative_scores)

    # ----- top-level entry point ----------------------------------------

    def run_debate(self, motion: str | None = None, rounds: int | None = None) -> Verdict:
        """Run a full debate end-to-end and return the final verdict.

        Always closes the spawned children on the way out, even on
        failure. Drives the FSM through every transition documented
        in :mod:`debate.orchestration.state_machine`.
        """
        topic = motion if motion is not None else self._motion
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("motion must be a non-empty string")
        round_count = rounds if rounds is not None else self._fsm.max_rounds
        if round_count < 1:
            raise ValueError("rounds must be >= 1")

        self.history = DebateHistory(motion=topic)
        self._log("debate_started", motion=topic, rounds=round_count)

        try:
            self._fsm.transition(Event.START)
            self._spawn_children()
            self._fsm.transition(Event.CHILDREN_READY)

            self._send_init_to_both(topic)

            self._run_opening_phase()
            self._fsm.transition(Event.SENT_OPENINGS)

            self._run_argument_rounds(round_count)

            self._run_closing_phase()
            self._fsm.transition(Event.CLOSINGS_RECEIVED)

            verdict = self._verdict_with_retry()
            self._log("debate_done", **self._verdict_log_fields(verdict))
            return verdict
        finally:
            with contextlib.suppress(Exception):
                self._send_shutdown_to_both()
            with contextlib.suppress(Exception):
                self._supervisor.terminate_all()

    # ----- prompt / init building ---------------------------------------

    def build_prompt(
        self,
        role: str,
        phase: Phase,
        context: list[str] | None = None,
        opponent_last: str | None = None,
    ) -> Message:
        """Build a ``prompt`` envelope to send to ``role``.

        ``role`` must be ``"pro"`` or ``"con"``. The envelope is
        always sent *as the Judge* (``role=Role.JUDGE``); the
        recipient is encoded by the Supervisor channel, not by the
        envelope's ``role`` field.

        ``opponent_last`` is the *content string* of the other side's
        last reply - never the other side's :class:`Message`
        envelope. This is how Pro and Con stay isolated from each
        other on the wire.
        """
        if role not in _VALID_ROLE_STRINGS:
            raise ValueError(f"invalid role for prompt: {role!r}")
        payload: dict[str, Any] = {
            "phase": phase.value,
            "round": self._fsm.current_round,
        }
        if opponent_last is not None:
            if not isinstance(opponent_last, str):
                raise TypeError("opponent_last must be a string (content only, not a Message)")
            payload["opponent_last"] = opponent_last
        if context:
            payload["selected_context"] = list(context)
        return self._make_judge_message(MessageType.PROMPT, payload)

    def build_init(self, role: str, motion: str) -> Message:
        """Build the per-side ``init`` envelope (stance + motion)."""
        if role not in _VALID_ROLE_STRINGS:
            raise ValueError(f"invalid role for init: {role!r}")
        payload: dict[str, Any] = {
            "stance": role,
            "motion": motion,
            "max_tokens": self._max_tokens_per_turn,
        }
        return self._make_judge_message(MessageType.INIT, payload)

    # ----- per-turn driver ----------------------------------------------

    def run_turn(self, role: str, phase: Phase, opponent_last: str | None) -> Message:
        """Send a prompt to ``role`` and pull back exactly one reply.

        ``tool_call`` envelopes received from the child while
        waiting for the reply are routed through the Judge's
        :class:`ToolRouter` and answered with a ``tool_result``
        envelope. The loop is bounded by
        :data:`DEFAULT_RECEIVE_MAX_ITERS` so a babbling child cannot
        burn budget forever.
        """
        prompt = self.build_prompt(role, phase, opponent_last=opponent_last)
        self._supervisor.send(role, prompt)
        prompt_payload = dict(prompt.payload)
        prompt_text = format_transcript_dict(prompt_payload)
        self._log(
            "prompt_sent",
            target_role=role,
            phase=phase.value,
            round=self._fsm.current_round,
            prompt_turn_id=prompt.turn_id,
            prompt_payload=prompt_payload,
            prompt_text=prompt_text,
            prompt_length=len(prompt_text),
        )

        for _ in range(self._receive_max_iters):
            try:
                msg = self._supervisor.receive(role, timeout=self._per_turn_timeout_sec)
            except (ChildReceiveTimeoutError, ChildStreamClosedError) as exc:
                self._log(
                    "turn_failed",
                    target_role=role,
                    error=type(exc).__name__,
                    phase=phase.value,
                )
                raise
            except SupervisorError as exc:
                self._log(
                    "turn_failed",
                    target_role=role,
                    error=type(exc).__name__,
                    phase=phase.value,
                )
                raise

            if msg.type is MessageType.TOOL_CALL:
                self._handle_tool_call(role, msg)
                continue
            if msg.type is MessageType.REPLY:
                self.validate_child_reply(msg, expected_role=role)
                content = msg.payload.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
                self._log(
                    "reply_received",
                    target_role=role,
                    phase=phase.value,
                    round=self._fsm.current_round,
                    reply_turn_id=msg.turn_id,
                    content=content,
                    content_length=len(content),
                )
                return msg

            raise InvalidReplyError(f"unexpected message type from {role!r}: {msg.type.value}")

        raise InvalidReplyError(
            f"too many tool_call iterations from {role!r} (max {self._receive_max_iters})"
        )

    # ----- validation ----------------------------------------------------

    def validate_child_reply(self, message: Message, expected_role: str) -> Message:
        """Validate a ``reply`` message and return it unchanged.

        Raises :class:`InvalidReplyError` for:

        - wrong sender role,
        - wrong message type (anything other than ``REPLY``),
        - empty / whitespace-only content,
        - stance field that disagrees with the expected role.
        """
        if expected_role not in _VALID_ROLE_STRINGS:
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

    # ----- scoring -------------------------------------------------------

    def score_turn(self, role: str, reply: Message, round_number: int) -> dict[str, Any]:
        """Score a single child reply and return a structured score payload.

        Stage 9 uses a deterministic, content-length-derived scorer
        so tests can predict cumulative scores without an LLM call.
        Real-LLM scoring can drop in later by overriding this
        method - the API contract (``role`` / ``score`` / ``round``
        / token usage echoed) stays stable.
        """
        if role not in _VALID_ROLE_STRINGS:
            raise ValueError(f"invalid role for score_turn: {role!r}")
        content = reply.payload.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        base = 1
        length_bonus = min(len(content.strip()) // 50, 9)
        score = base + length_bonus
        return {
            "role": role,
            "round": round_number,
            "score": score,
            "content_length": len(content),
            "tokens_in": int(reply.payload.get("tokens_in", 0) or 0),
            "tokens_out": int(reply.payload.get("tokens_out", 0) or 0),
        }

    # ----- verdict generation -------------------------------------------

    def generate_verdict(self) -> Verdict:
        """Ask the LLM (under Gatekeeper budget) for a verdict and parse it.

        The prompt asks for strict JSON; the response is parsed and
        run through :class:`debate.sdk.schemas.Verdict`. Any parse
        or schema error is wrapped as :class:`InvalidVerdictError`
        so the caller can drive the FSM ``invalid_or_tie`` path.
        """
        prompt = self._build_verdict_prompt()
        try:
            response = self._gatekeeper.call_llm(
                self._llm,
                prompt=prompt,
                max_tokens=self._max_tokens_per_turn,
            )
        except BudgetExceededError:
            raise
        text = response.text
        self._log(
            "verdict_llm_response",
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            text_length=len(text),
            verdict_text=text,
        )
        return self._parse_verdict(text)

    def validate_verdict(self, verdict: Verdict) -> None:
        """Verify the verdict is structurally complete.

        Beyond the schema (which already forbids ``winner="tie"``),
        the Judge requires a ``scores`` dict with both sides and at
        least :data:`MIN_VERDICT_REASONS` non-empty reason strings.
        """
        if not isinstance(verdict, Verdict):
            raise InvalidVerdictError("validate_verdict requires a Verdict instance")
        if verdict.winner not in _VALID_ROLE_STRINGS:
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
            raise InvalidVerdictError(
                f"verdict must include at least {MIN_VERDICT_REASONS} reasons"
            )
        for r in reasons:
            if not isinstance(r, str) or not r.strip():
                raise InvalidVerdictError("each reason must be a non-empty string")

    @staticmethod
    def _verdict_scores(verdict: Verdict) -> dict[str, int]:
        extra = verdict.model_extra or {}
        scores = extra.get("scores")
        if not isinstance(scores, dict):
            return {"pro": 0, "con": 0}
        return {"pro": int(scores.get("pro", 0)), "con": int(scores.get("con", 0))}

    def _resolve_tiebreak_winner(
        self,
        *,
        preferred_winner: str | None,
        cumulative: dict[str, int] | None = None,
    ) -> str:
        """Pick a side when verdict scores are tied.

        Prefer a valid ``preferred_winner`` (from the LLM verdict).
        Otherwise use cumulative debate scores; on an exact cumulative
        tie, ``con`` wins by deterministic rule.
        """
        if preferred_winner in _VALID_ROLE_STRINGS:
            return preferred_winner
        totals = cumulative if cumulative is not None else self.history.cumulative_scores
        pro = int(totals.get("pro", 0))
        con = int(totals.get("con", 0))
        if pro > con:
            return "pro"
        return "con"

    @staticmethod
    def _adjust_scores_for_winner(scores: dict[str, int], winner: str) -> dict[str, int]:
        """Ensure the selected winner has a strictly higher visible score."""
        pro = int(scores["pro"])
        con = int(scores["con"])
        if pro != con:
            return {"pro": pro, "con": con}
        if winner == "pro":
            return {"pro": pro + 1, "con": con}
        return {"pro": pro, "con": con + 1}

    @staticmethod
    def _rebuild_verdict(
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

    def _finalize_verdict(self, verdict: Verdict) -> tuple[Verdict, bool, str | None]:
        """Return a verdict whose visible scores are never tied."""
        scores = self._verdict_scores(verdict)
        if scores["pro"] != scores["con"]:
            return verdict, False, None
        winner = self._resolve_tiebreak_winner(preferred_winner=verdict.winner)
        adjusted = self._adjust_scores_for_winner(scores, winner)
        return self._rebuild_verdict(verdict, winner=winner, scores=adjusted), True, "scores_equal"

    def apply_tie_breaker(self, scores: dict[str, int]) -> Verdict:
        """Deterministic fallback verdict.

        Higher cumulative score wins. If the cumulative scores are
        exactly equal, ``con`` wins by deterministic rule. Visible
        scores are bumped by one point when still tied so the winner
        is strictly ahead.
        """
        pro = int(scores.get("pro", 0))
        con = int(scores.get("con", 0))
        cumulative = {"pro": pro, "con": con}
        if pro > con:
            winner = "pro"
            rationale = f"tie-break: pro cumulative score {pro} > con {con}"
        elif con > pro:
            winner = "con"
            rationale = f"tie-break: con cumulative score {con} > pro {pro}"
        else:
            winner = "con"
            rationale = (
                f"tie-break: cumulative scores equal at {pro}; con wins by deterministic rule"
            )
        final_scores = self._adjust_scores_for_winner(cumulative, winner)
        return Verdict(
            winner=winner,
            rationale=rationale,
            scores=final_scores,
            reasons=[
                "Verdict pipeline exhausted retries; deterministic tie-break applied.",
                rationale,
                "Tie-break rule: higher cumulative score wins; on exact ties con wins.",
            ],
        )

    # ============= internals =============================================

    # ---- spawn / init ----

    def _spawn_children(self) -> None:
        try:
            self._supervisor.spawn("pro")
            self._supervisor.spawn("con")
        except SupervisorError as exc:
            self._log("spawn_failed", error=type(exc).__name__, message=str(exc))
            with contextlib.suppress(Exception):
                self._fsm.transition(Event.SPAWN_FAILED)
            raise
        self._log("children_spawned", roles=["pro", "con"])

    def _send_init_to_both(self, motion: str) -> None:
        for role in ("pro", "con"):
            init_msg = self.build_init(role, motion)
            self._supervisor.send(role, init_msg)
            self._log(
                "init_sent",
                target_role=role,
                init_turn_id=init_msg.turn_id,
                motion_length=len(motion),
            )

    def _send_shutdown_to_both(self) -> None:
        for role in ("pro", "con"):
            child = self._supervisor.child(role)
            if child is None:
                continue
            shutdown_msg = self._make_judge_message(MessageType.SHUTDOWN, {})
            with contextlib.suppress(Exception):
                self._supervisor.send(role, shutdown_msg)

    # ---- phase drivers ----

    def _run_opening_phase(self) -> None:
        pro_open = self.run_turn("pro", Phase.OPENING, opponent_last=None)
        self._record_turn("pro", Phase.OPENING, round_number=0, reply=pro_open)
        con_open = self.run_turn("con", Phase.OPENING, opponent_last=pro_open.payload["content"])
        self._record_turn("con", Phase.OPENING, round_number=0, reply=con_open)

    def _run_argument_rounds(self, round_count: int) -> None:
        for r in range(1, round_count + 1):
            pro_reply = self.run_turn("pro", Phase.ARGUMENT, opponent_last=self.history.last_con)
            self._fsm.transition(Event.PRO_REPLY)
            self._record_turn("pro", Phase.ARGUMENT, round_number=r, reply=pro_reply)
            self._fsm.transition(Event.SCORED)

            con_reply = self.run_turn("con", Phase.ARGUMENT, opponent_last=self.history.last_pro)
            self._fsm.transition(Event.CON_REPLY)
            self._record_turn("con", Phase.ARGUMENT, round_number=r, reply=con_reply)
            self._fsm.transition(Event.SCORED)

            if r < round_count:
                self._fsm.transition(Event.SCORED)
            else:
                self._fsm.transition(Event.ROUND_LIMIT_REACHED)

    def _run_closing_phase(self) -> None:
        pro_close = self.run_turn("pro", Phase.CLOSING, opponent_last=self.history.last_con)
        self._record_turn(
            "pro", Phase.CLOSING, round_number=self._fsm.current_round, reply=pro_close
        )
        con_close = self.run_turn("con", Phase.CLOSING, opponent_last=pro_close.payload["content"])
        self._record_turn(
            "con", Phase.CLOSING, round_number=self._fsm.current_round, reply=con_close
        )

    # ---- verdict pipeline ----

    def _verdict_with_retry(self) -> Verdict:
        attempts = 0
        while True:
            attempts += 1
            try:
                verdict = self.generate_verdict()
                self.validate_verdict(verdict)
                verdict, tiebreak_applied, tiebreak_reason = self._finalize_verdict(verdict)
                self._fsm.transition(Event.JUDGE_REPLY)
                self._fsm.transition(Event.VALID_NON_TIE)
                self._log_verdict_record(
                    verdict,
                    attempt=attempts,
                    source="llm",
                    verdict_tiebreak_applied=tiebreak_applied,
                    tiebreak_reason=tiebreak_reason,
                )
                return verdict
            except InvalidVerdictError as exc:
                self._log("verdict_invalid", attempt=attempts, reason=str(exc))
                self._fsm.transition(Event.JUDGE_REPLY)
                next_state = self._fsm.transition(Event.INVALID_OR_TIE)
                if next_state is State.VERDICT:
                    continue
                if next_state is State.TIE_BREAK:
                    cumulative = self.history.cumulative_scores
                    tied = self.apply_tie_breaker(cumulative)
                    self._fsm.transition(Event.JUDGE_REPLY)
                    pro = int(cumulative.get("pro", 0))
                    con = int(cumulative.get("con", 0))
                    scores_were_equal = pro == con
                    self._log_verdict_record(
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

    def _build_verdict_prompt(self) -> str:
        lines: list[str] = [
            "You are the impartial Judge of a debate.",
            f"Motion: {self.history.motion}",
            "",
            "Cumulative scores so far (deterministic Judge scoring):",
            f"  pro: {self.history.cumulative_scores.get('pro', 0)}",
            f"  con: {self.history.cumulative_scores.get('con', 0)}",
            "",
            "Transcript (most recent first):",
        ]
        for record in reversed(self.history.turns[-10:]):
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

    def _parse_verdict(self, text: str) -> Verdict:
        body = self._extract_json(text)
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

    @staticmethod
    def _extract_json(text: str) -> str:
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

    # ---- tool calls ----

    def _handle_tool_call(self, role: str, tool_msg: Message) -> None:
        if tool_msg.role is not Role(role):
            self._log(
                "tool_call_role_mismatch",
                target_role=role,
                actual_role=tool_msg.role.value,
            )
            raise InvalidReplyError(
                f"tool_call from {role!r} carried sender role {tool_msg.role.value!r}"
            )

        payload = tool_msg.payload
        tool_name = payload.get("tool")
        tool_call_payload = dict(payload) if isinstance(payload, dict) else {"tool": tool_name}
        self._log(
            "tool_call_received",
            target_role=role,
            tool=tool_name,
            tool_call_turn_id=tool_msg.turn_id,
            tool_call_payload=tool_call_payload,
        )

        result_payload: dict[str, Any]
        if not isinstance(tool_name, str) or not tool_name:
            result_payload = {
                "tool": tool_name if isinstance(tool_name, str) else "",
                "error": "invalid_arguments",
                "message": "tool_call payload missing 'tool' name",
            }
        else:
            kwargs = {k: v for k, v in payload.items() if k != "tool"}
            try:
                result_payload = self._router.call(tool_name, **kwargs)
            except UnknownToolError as exc:
                result_payload = {
                    "tool": tool_name,
                    "error": "unknown_tool",
                    "message": str(exc),
                }
            except BudgetExceededError as exc:
                result_payload = {
                    "tool": tool_name,
                    "error": "budget_exceeded",
                    "kind": exc.kind.value,
                    "message": str(exc),
                }
            except ValueError as exc:
                result_payload = {
                    "tool": tool_name,
                    "error": "invalid_arguments",
                    "message": str(exc),
                }
            except Exception as exc:  # noqa: BLE001 - never crash the debate
                result_payload = {
                    "tool": tool_name,
                    "error": "tool_error",
                    "message": str(exc),
                }

        result_msg = self._make_judge_message(MessageType.TOOL_RESULT, result_payload)
        self._supervisor.send(role, result_msg)
        self._log(
            "tool_result_sent",
            target_role=role,
            tool=tool_name,
            tool_result_turn_id=result_msg.turn_id,
            error=result_payload.get("error"),
            tool_result_payload=dict(result_payload),
        )

    # ---- bookkeeping helpers ----

    def _record_turn(
        self,
        role: str,
        phase: Phase,
        round_number: int,
        reply: Message,
    ) -> TurnRecord:
        score_payload = self.score_turn(role, reply, round_number)
        record = TurnRecord(
            role=role,
            phase=phase,
            round_number=round_number,
            content=str(reply.payload.get("content", "")),
            score=int(score_payload["score"]),
            tokens_in=int(score_payload["tokens_in"]),
            tokens_out=int(score_payload["tokens_out"]),
        )
        self.history.add(record)
        self._log(
            "score_recorded",
            target_role=role,
            phase=phase.value,
            round=round_number,
            score=record.score,
            cumulative_pro=self.history.cumulative_scores.get("pro", 0),
            cumulative_con=self.history.cumulative_scores.get("con", 0),
        )
        return record

    def _make_judge_message(self, type_: MessageType, payload: dict[str, Any]) -> Message:
        msg = Message(
            v=SCHEMA_VERSION,
            ts=float(self._clock()),
            turn_id=self._next_turn_id(),
            role=Role.JUDGE,
            type=type_,
            payload=dict(payload),
        )
        return msg

    def _next_turn_id(self) -> int:
        tid = self._outgoing_turn_id
        self._outgoing_turn_id += 1
        return tid

    # ---- logging helpers ----

    def _log(self, event_type: str, **fields: Any) -> None:
        if self._logger is None:
            return
        safe_fields = redact(
            prepare_transcript_field(fields, max_chars=self._max_logged_text_chars)
        )
        with contextlib.suppress(Exception):
            self._logger.log(
                role="judge",
                turn_id=self._outgoing_turn_id,
                event_type=event_type,
                **safe_fields,
            )

    def _log_verdict_record(
        self,
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
            **self._verdict_log_fields(verdict),
        }
        if verdict_tiebreak_applied:
            fields["verdict_tiebreak_applied"] = True
        if tiebreak_reason is not None:
            fields["tiebreak_reason"] = tiebreak_reason
        self._log("verdict_recorded", **fields)

    @staticmethod
    def _verdict_log_fields(verdict: Verdict) -> dict[str, Any]:
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
