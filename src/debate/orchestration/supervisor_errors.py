"""Supervisor exception types."""

from __future__ import annotations

VALID_ROLES: frozenset[str] = frozenset({"pro", "con"})


class SupervisorError(RuntimeError):
    """Base class for Supervisor errors."""


class UnknownRoleError(SupervisorError):
    """Raised when a role other than `pro` or `con` is requested."""

    def __init__(self, role: str) -> None:
        super().__init__(f"unknown child role: {role!r}; valid roles are {sorted(VALID_ROLES)}")
        self.role = role


class ChildAlreadyRunningError(SupervisorError):
    """Raised when `spawn(role)` is called for a role that is already alive."""

    def __init__(self, role: str) -> None:
        super().__init__(f"child {role!r} is already running")
        self.role = role


class ChildNotRunningError(SupervisorError):
    """Raised when `send` / `receive` / `terminate` target a missing child."""

    def __init__(self, role: str) -> None:
        super().__init__(f"child {role!r} is not running")
        self.role = role


class ChildStreamClosedError(SupervisorError):
    """Raised by `receive` when the child's stdout has closed (EOF)."""

    def __init__(self, role: str) -> None:
        super().__init__(f"child {role!r} stdout stream closed (EOF)")
        self.role = role


class ChildReceiveTimeoutError(SupervisorError, TimeoutError):
    """Raised when `receive(role, timeout)` runs out of time."""

    def __init__(self, role: str, timeout: float) -> None:
        super().__init__(f"timed out waiting for message from {role!r} after {timeout}s")
        self.role = role
        self.timeout = timeout
