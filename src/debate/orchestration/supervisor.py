"""Supervisor / child process manager (Stage 6)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from debate.orchestration.ipc import deserialize_message, serialize_message
from debate.orchestration.supervisor_env import (
    _DEFAULT_CHILD_ENV_ALLOWLIST,
    _DENIED_CHILD_ENV_KEYS,
    spawn_child,
)
from debate.orchestration.supervisor_env import (
    build_child_env as _build_child_env,
)
from debate.orchestration.supervisor_errors import (
    VALID_ROLES,
    ChildAlreadyRunningError,
    ChildNotRunningError,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    SupervisorError,
    UnknownRoleError,
)
from debate.orchestration.supervisor_io import (
    CON_STDERR_FILENAME,
    PRO_STDERR_FILENAME,
    STDERR_FILENAMES,
    ChildProcess,
    child_receive,
    child_send,
    teardown_child,
)
from debate.sdk.schemas import Message

DEFAULT_TERMINATE_TIMEOUT_S: float = 2.0


def default_command_builder(role: str) -> list[str]:
    return [sys.executable, "-m", f"debate.agents.{role}_agent"]


class Supervisor:
    def __init__(
        self,
        *,
        runs_dir: Path,
        command_builder: Callable[[str], list[str]] | None = None,
        env: Mapping[str, str] | None = None,
        env_allowlist: Iterable[str] = _DEFAULT_CHILD_ENV_ALLOWLIST,
        denied_env_keys: Iterable[str] = _DENIED_CHILD_ENV_KEYS,
        terminate_timeout_s: float = DEFAULT_TERMINATE_TIMEOUT_S,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runs_dir: Path = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._command_builder: Callable[[str], list[str]] = (
            command_builder if command_builder is not None else default_command_builder
        )
        self._env_source: dict[str, str] = dict(env) if env is not None else dict(os.environ)
        self._env_allowlist: frozenset[str] = frozenset(env_allowlist)
        self._denied_env_keys: frozenset[str] = frozenset(denied_env_keys)
        self._terminate_timeout_s: float = float(terminate_timeout_s)
        self._popen: Callable[..., subprocess.Popen] = popen
        self._clock: Callable[[], float] = clock
        self._children: dict[str, ChildProcess] = {}

    def child(self, role: str) -> ChildProcess | None:
        self._validate_role(role)
        return self._children.get(role)

    @property
    def children(self) -> Mapping[str, ChildProcess]:
        return dict(self._children)

    def spawn(self, role: str) -> ChildProcess:
        self._validate_role(role)
        existing = self._children.get(role)
        if existing is not None and existing.is_alive():
            raise ChildAlreadyRunningError(role)
        self._teardown_if_present(role)
        return self._spawn_internal(role, restart_count=0)

    def respawn(self, role: str) -> ChildProcess:
        self._validate_role(role)
        return self._spawn_internal(role, restart_count=self._teardown_if_present(role) + 1)

    def _teardown_if_present(self, role: str) -> int:
        existing = self._children.get(role)
        if existing is None:
            return 0
        count = existing.restart_count
        teardown_child(existing, terminate_timeout_s=self._terminate_timeout_s)
        self._children.pop(role, None)
        return count

    def _spawn_internal(self, role: str, restart_count: int) -> ChildProcess:
        cp = spawn_child(
            role=role,
            restart_count=restart_count,
            runs_dir=self.runs_dir,
            stderr_filename=STDERR_FILENAMES[role],
            command=self._command_builder(role),
            child_env=self.build_child_env(role),
            popen=self._popen,
            clock=self._clock,
        )
        self._children[role] = cp
        return cp

    def send(self, role: str, message: Message) -> None:
        self._validate_role(role)
        child = self._children.get(role)
        if child is None:
            raise ChildNotRunningError(role)
        child_send(child, role, message)

    def receive(self, role: str, timeout: float | None = None) -> Message:
        self._validate_role(role)
        child = self._children.get(role)
        if child is None:
            raise ChildNotRunningError(role)
        return child_receive(child, role, timeout)

    def terminate(self, role: str) -> None:
        self._validate_role(role)
        if role not in self._children:
            raise ChildNotRunningError(role)
        teardown_child(self._children.pop(role), terminate_timeout_s=self._terminate_timeout_s)

    def terminate_all(self) -> None:
        for role in list(self._children):
            with contextlib.suppress(ChildNotRunningError):
                self.terminate(role)

    def build_child_env(self, role: str) -> dict[str, str]:
        return _build_child_env(role, self._env_source, self._env_allowlist, self._denied_env_keys)

    def _validate_role(self, role: str) -> None:
        if role not in VALID_ROLES:
            raise UnknownRoleError(role)

    def __enter__(self) -> Supervisor:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.terminate_all()


__all__ = [
    "CON_STDERR_FILENAME",
    "DEFAULT_TERMINATE_TIMEOUT_S",
    "PRO_STDERR_FILENAME",
    "VALID_ROLES",
    "ChildAlreadyRunningError",
    "ChildNotRunningError",
    "ChildProcess",
    "ChildReceiveTimeoutError",
    "ChildStreamClosedError",
    "Supervisor",
    "SupervisorError",
    "UnknownRoleError",
    "default_command_builder",
    "deserialize_message",
    "serialize_message",
]
