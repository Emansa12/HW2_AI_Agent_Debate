"""Wire schemas for the debate IPC protocol.

This module defines the on-the-wire shape of every message exchanged
between the orchestrator and the agents. It intentionally has no
runtime behavior beyond validation - it is the source of truth for
the protocol.

Envelope (`Message`):
    v        - schema version (int, must equal SCHEMA_VERSION)
    ts       - timestamp, epoch seconds (float, >= 0)
    turn_id  - monotonic turn counter (int, >= 0)
    role     - sender / target role  (`Role`)
    type     - discriminator         (`MessageType`)
    payload  - free-form dict; some `type` values impose extra checks

Verdict payload constraint:
    `winner` must be exactly "pro" or "con". Ties are forbidden by
    protocol - the Judge must always pick a side.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION: Literal[1] = 1
"""Wire protocol version. Bump on any breaking change to the envelope."""


class Role(StrEnum):
    """Who is speaking / receiving a message."""

    JUDGE = "judge"
    PRO = "pro"
    CON = "con"


class MessageType(StrEnum):
    """Envelope `type` discriminator. Closed set."""

    INIT = "init"
    PROMPT = "prompt"
    REPLY = "reply"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PING = "ping"
    PONG = "pong"
    SCORE = "score"
    VERDICT = "verdict"
    EVENT = "event"
    SHUTDOWN = "shutdown"


class Phase(StrEnum):
    """Debate phase. Carried inside relevant payloads, not the envelope."""

    OPENING = "opening"
    ARGUMENT = "argument"
    CLOSING = "closing"


class Verdict(BaseModel):
    """Verdict payload.

    `winner` is strictly `pro` or `con`. Ties are forbidden in this
    debate protocol - the Judge must always pick a side.
    """

    model_config = ConfigDict(extra="allow")

    winner: Literal["pro", "con"]
    rationale: str | None = None


class Message(BaseModel):
    """Wire envelope for every IPC message."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = SCHEMA_VERSION
    ts: float = Field(ge=0.0)
    turn_id: int = Field(ge=0)
    role: Role
    type: MessageType
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_typed_payload(self) -> Message:
        if self.type == MessageType.VERDICT:
            try:
                Verdict.model_validate(self.payload)
            except ValidationError as exc:
                raise ValueError(f"invalid verdict payload: {exc.errors()}") from exc
        return self
