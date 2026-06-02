"""Types and constants for the Stage 8 Watchdog."""

from __future__ import annotations

from collections.abc import Callable

DEFAULT_HEARTBEAT_INTERVAL_SEC: float = 1.0
DEFAULT_HEARTBEAT_TIMEOUT_SEC: float = 0.5
DEFAULT_ROLES: tuple[str, ...] = ("pro", "con")


class MissReason:
    """Symbolic strings the Watchdog uses when reporting a miss."""

    NO_CHILD: str = "no_child"
    CHILD_NOT_ALIVE: str = "child_not_alive"
    SEND_FAILED: str = "send_failed"
    TIMEOUT: str = "timeout"
    STREAM_CLOSED: str = "stream_closed"
    IPC_ERROR: str = "ipc_error"
    NOT_A_PONG: str = "not_a_pong"
    SUPERVISOR_ERROR: str = "supervisor_error"
    UNEXPECTED_ERROR: str = "unexpected_error"


OnMissCallback = Callable[[str], None]
