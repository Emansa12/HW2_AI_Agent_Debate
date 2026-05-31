"""BaseAgent: shared behavior for child debate processes.

A child agent talks to its parent (the Judge / orchestrator) over
JSONL stdin/stdout using the existing
:mod:`debate.orchestration.ipc` helpers. BaseAgent owns the read /
parse / dispatch loop and the heartbeat (``ping`` -> ``pong``) +
``shutdown`` protocol so that every subclass can focus purely on
debate semantics.

BaseAgent is intentionally **role-neutral**: it knows it is a child,
it knows its own :class:`debate.sdk.schemas.Role`, and it knows how
to emit valid :class:`debate.sdk.schemas.Message` envelopes - but it
does **not** know anything about debate phases, stance, motions, the
LLM, search, or the Watchdog.

Subclass contract
-----------------

Override :meth:`handle` to react to ``init``, ``prompt``, and
``tool_result`` messages. Heartbeats and shutdown are handled here.

Failure handling
----------------

The run loop is defensive on purpose:

- a malformed line (bad UTF-8, bad JSON, schema violation, oversize,
  embedded newline, wrong ``v``) is reported via :meth:`_on_ipc_error`
  and the loop continues with the next line;
- an exception raised by a handler is reported via
  :meth:`_on_handler_error` and the loop continues;
- EOF on stdin or a closed pipe ends the loop with exit code 0.

This keeps a single bad message from killing the child - the Watchdog
(Stage 8) will decide when a child has misbehaved enough to be
respawned.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import IO, Any

from debate.orchestration.ipc import (
    IPCError,
    MalformedMessageError,
    deserialize_message,
    serialize_message,
)
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role

logger = logging.getLogger(__name__)


_DISPATCH_TYPES: frozenset[MessageType] = frozenset(
    {MessageType.INIT, MessageType.PROMPT, MessageType.TOOL_RESULT}
)
"""Message types that go to :meth:`BaseAgent.handle` (subclass hook)."""


class BaseAgent:
    """Base class for Pro/Con child agents.

    Parameters
    ----------
    role:
        The agent's own role (``Role.PRO`` for ProAgent, ``Role.CON``
        for ConAgent). Used as the ``role`` field of every outgoing
        envelope.
    stdin / stdout:
        Injectable binary streams. Default to
        ``sys.stdin.buffer`` / ``sys.stdout.buffer`` so that the
        child reads JSONL bytes from the supervisor's pipe. Tests
        pass in-memory ``BytesIO`` objects.
    clock:
        Injectable clock returning epoch seconds; defaults to
        :func:`time.time`.
    """

    def __init__(
        self,
        *,
        role: Role,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.role: Role = role
        self._stdin: IO[bytes] = stdin if stdin is not None else sys.stdin.buffer
        self._stdout: IO[bytes] = stdout if stdout is not None else sys.stdout.buffer
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._running: bool = True
        self._turn_id: int = 0

    # ----- public API ---------------------------------------------------

    def run(self) -> int:
        """Run the read/dispatch loop until shutdown or EOF.

        Returns 0 on graceful exit. The loop never propagates parse
        or handler exceptions; see :meth:`_on_ipc_error` and
        :meth:`_on_handler_error`.
        """
        while self._running:
            raw = self._read_line()
            if raw is None:
                break
            try:
                msg = self._parse(raw)
            except IPCError as exc:
                self._on_ipc_error(exc, raw)
                continue
            self._dispatch(msg)
        return 0

    def send(self, message: Message) -> None:
        """Write a `Message` to stdout as one JSONL line.

        Uses :func:`debate.orchestration.ipc.serialize_message` - no
        manual JSON serialization is performed by BaseAgent.
        """
        line = serialize_message(message)
        try:
            self._stdout.write(line.encode("utf-8"))
            self._stdout.flush()
        except (OSError, ValueError):
            self._running = False

    def stop(self) -> None:
        """Request a graceful loop exit on the next iteration."""
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def next_turn_id(self) -> int:
        tid = self._turn_id
        self._turn_id += 1
        return tid

    def make_message(
        self,
        type_: MessageType,
        payload: dict[str, Any] | None = None,
        *,
        ts: float | None = None,
    ) -> Message:
        """Build a Message envelope with this agent's role and a fresh turn_id.

        ``ts`` defaults to the injected clock so tests stay deterministic.
        """
        return Message(
            v=SCHEMA_VERSION,
            ts=ts if ts is not None else self._clock(),
            turn_id=self.next_turn_id(),
            role=self.role,
            type=type_,
            payload=dict(payload) if payload is not None else {},
        )

    # ----- subclass hook ------------------------------------------------

    def handle(self, msg: Message) -> None:
        """Override to handle init / prompt / tool_result messages.

        Default behavior: log at debug level and ignore. Subclasses
        are expected to inspect ``msg.type`` and react accordingly.
        """
        logger.debug("BaseAgent.handle ignored type=%s", msg.type.value)

    # ----- internals ----------------------------------------------------

    def _read_line(self) -> bytes | None:
        try:
            raw = self._stdin.readline()
        except (OSError, ValueError):
            return None
        if not raw:
            return None
        return raw

    def _parse(self, raw: bytes) -> Message:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedMessageError(f"invalid utf-8 on stdin: {exc}") from exc
        return deserialize_message(line)

    def _dispatch(self, msg: Message) -> None:
        try:
            if msg.type is MessageType.PING:
                self._handle_ping(msg)
            elif msg.type is MessageType.SHUTDOWN:
                self._handle_shutdown(msg)
            elif msg.type in _DISPATCH_TYPES:
                self.handle(msg)
            else:
                self._on_unknown_type(msg)
        except Exception as exc:  # noqa: BLE001 - we never want to crash the loop
            self._on_handler_error(exc, msg)

    def _handle_ping(self, ping: Message) -> None:
        pong = self.make_message(
            MessageType.PONG,
            {"in_reply_to": ping.turn_id},
        )
        self.send(pong)

    def _handle_shutdown(self, _shutdown: Message) -> None:
        self._running = False

    # ----- error hooks (overridable) ------------------------------------

    def _on_ipc_error(self, exc: IPCError, raw: bytes) -> None:
        """Called when a single line cannot be parsed. Loop continues."""
        logger.warning(
            "ipc error on %s: %s (%d bytes)",
            self.role.value,
            exc,
            len(raw),
        )

    def _on_handler_error(self, exc: Exception, msg: Message) -> None:
        """Called when handle()/heartbeat raises. Loop continues."""
        logger.exception(
            "handler error on %s for type=%s: %s",
            self.role.value,
            msg.type.value,
            exc,
        )

    def _on_unknown_type(self, msg: Message) -> None:
        """Called for message types BaseAgent does not route. Loop continues."""
        logger.debug(
            "unhandled message type on %s: %s",
            self.role.value,
            msg.type.value,
        )


__all__ = ["BaseAgent"]
