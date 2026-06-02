"""BaseAgent: shared behavior for child debate processes."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import IO, Any

from debate.agents.base_agent_handlers import (
    _handle_ping,
    _handle_shutdown,
    _on_handler_error,
    _on_ipc_error,
    _on_unknown_type,
)
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


class BaseAgent:
    """Base class for Pro/Con child agents."""

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

    def run(self) -> int:
        while self._running:
            raw = self._read_line()
            if raw is None:
                break
            try:
                msg = self._parse(raw)
            except IPCError as exc:
                _on_ipc_error(self, exc, raw)
                continue
            self._dispatch(msg)
        return 0

    def send(self, message: Message) -> None:
        line = serialize_message(message)
        try:
            self._stdout.write(line.encode("utf-8"))
            self._stdout.flush()
        except (OSError, ValueError):
            self._running = False

    def stop(self) -> None:
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
        return Message(
            v=SCHEMA_VERSION,
            ts=ts if ts is not None else self._clock(),
            turn_id=self.next_turn_id(),
            role=self.role,
            type=type_,
            payload=dict(payload) if payload is not None else {},
        )

    def handle(self, msg: Message) -> None:
        logger.debug("BaseAgent.handle ignored type=%s", msg.type.value)

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
                _handle_ping(self, msg)
            elif msg.type is MessageType.SHUTDOWN:
                _handle_shutdown(self, msg)
            elif msg.type in _DISPATCH_TYPES:
                self.handle(msg)
            else:
                _on_unknown_type(self, msg)
        except Exception as exc:  # noqa: BLE001
            _on_handler_error(self, exc, msg)


__all__ = ["BaseAgent"]
