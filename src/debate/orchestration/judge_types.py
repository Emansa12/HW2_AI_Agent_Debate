"""Judge types, constants, and exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field

from debate.sdk.schemas import Phase

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


VALID_ROLE_STRINGS: frozenset[str] = frozenset({"pro", "con"})
