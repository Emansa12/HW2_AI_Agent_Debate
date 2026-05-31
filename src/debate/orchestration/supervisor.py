"""Supervisor / child process manager (Stage 6).

The Supervisor owns the lifecycle of the Pro and Con child processes:

- spawns them with `subprocess.Popen`;
- pipes JSONL bytes to their stdin and reads JSONL bytes from their
  stdout, always via the existing :mod:`debate.orchestration.ipc`
  helpers (it never serializes JSON itself);
- captures their stderr into per-role log files (`pro_stderr.log`,
  `con_stderr.log`) inside the run directory;
- gracefully terminates them (SIGTERM-equivalent first, then
  hard-kills if they don't exit within a small timeout);
- can respawn a child after a crash, incrementing its
  `restart_count`.

It does **not** know about the debate flow, the Judge, the Watchdog,
or any retry policy. Those live in later stages and on top of this
module.

Environment / secret rules
--------------------------

Children are spawned with an *allow-list*-filtered environment - only
keys that the agent process is known to need flow through. As an
extra defense-in-depth step, a small *deny-list* is applied
afterwards so that even if the allow-list is extended carelessly,
the secrets listed in :data:`_DENIED_CHILD_ENV_KEYS` (most notably
``SEARCH_API_KEY``) can never reach Pro or Con. Search is
intentionally only available to the Judge / parent process via the
:class:`debate.shared.router.ToolRouter`; child agents must go
through the parent for any tool calls.

Stderr filenames
----------------

We use ``pro_stderr.log`` and ``con_stderr.log`` (underscore, not
dot) for the same reason :mod:`debate.shared.logger` does: on
Windows, any filename whose basename is ``con`` (regardless of
extension) matches the reserved DOS device name CON and cannot be
opened. ``con.stderr.log`` would fail to create on Windows.

Stage boundary
--------------

Stage 6 deliberately stops here:

- no Watchdog / heartbeat / liveness monitoring;
- no Pro / Con real agent logic;
- no Judge debate flow;
- no automatic child recovery orchestration (the `respawn` primitive
  is provided, but the *decision* of when to call it is left to a
  later stage).
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from debate.orchestration.ipc import deserialize_message, serialize_message
from debate.sdk.schemas import Message

VALID_ROLES: frozenset[str] = frozenset({"pro", "con"})

DEFAULT_TERMINATE_TIMEOUT_S: float = 2.0
"""Seconds to wait for a graceful exit before hard-killing."""

PRO_STDERR_FILENAME: str = "pro_stderr.log"
CON_STDERR_FILENAME: str = "con_stderr.log"

_STDERR_FILENAMES: dict[str, str] = {
    "pro": PRO_STDERR_FILENAME,
    "con": CON_STDERR_FILENAME,
}

_DEFAULT_CHILD_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Interpreter / loader essentials. Without PATH and the Windows
        # *ROOT / WINDIR / SYSTEM32 set, a `python -m ...` child often
        # cannot even start on Windows.
        "PATH",
        "PATHEXT",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        # Windows essentials.
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        # Temp + user dirs (used by stdlib for tempfile / caches).
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOMEPATH",
        "HOMEDRIVE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        # Locale.
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        # API keys child agents legitimately need. Search is *not*
        # here: search must be brokered by the parent process. The
        # generic ``LLM_API_KEY`` is the Stage 11 canonical name;
        # ``OPENAI_API_KEY`` is kept as a provider-specific alias.
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        # Stage 11: tells the child's __main__ block to swap
        # FakeLLMClient for RealLLMClient. Set to "1" by the parent
        # CLI when ``--real-llm`` is passed.
        "DEBATE_REAL_LLM",
        # Debate-config-relevant passthroughs.
        "DEBATE_ROUNDS",
        "DEBATE_MAX_TOKENS",
        "DEBATE_MOTION",
    }
)

_DENIED_CHILD_ENV_KEYS: frozenset[str] = frozenset(
    {
        # Search is *only* brokered by the parent ToolRouter +
        # Gatekeeper; child processes must never see a search key.
        # Both the canonical name and the Stage 11 Tavily-specific
        # alias are blocked, even if the allow-list grew carelessly.
        "SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "SERPAPI_API_KEY",
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ChildProcess data structure
# ---------------------------------------------------------------------------


class _ReaderSentinel:
    """Marker placed on the reader queue when the child closes stdout."""

    __slots__ = ()


_EOF = _ReaderSentinel()


@dataclass
class ChildProcess:
    """Bundle describing one running child (Pro or Con)."""

    role: str
    process: subprocess.Popen
    stdin: IO[Any]
    stdout: IO[Any]
    stderr_path: Path
    start_time: float
    restart_count: int = 0

    read_queue: queue.Queue = field(default_factory=queue.Queue, repr=False, compare=False)
    reader_thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    stderr_fh: IO[Any] | None = field(default=None, repr=False, compare=False)

    @property
    def pid(self) -> int | None:
        return getattr(self.process, "pid", None)

    def is_alive(self) -> bool:
        try:
            return self.process.poll() is None
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Default command builder
# ---------------------------------------------------------------------------


def default_command_builder(role: str) -> list[str]:
    """Default `argv` for spawning a child agent.

    Resolves to ``python -m debate.agents.<role>_agent``. This is the
    target wired up in later stages; tests / smoke runs pass their
    own builder.
    """
    return [sys.executable, "-m", f"debate.agents.{role}_agent"]


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Manages Pro and Con child processes.

    The Supervisor is intentionally narrow:

    - it does not know what messages mean (`send` / `receive` operate
      on validated :class:`debate.sdk.schemas.Message` objects, going
      through :func:`debate.orchestration.ipc.serialize_message` and
      :func:`debate.orchestration.ipc.deserialize_message`);
    - it does not decide when to respawn (it exposes `respawn` and
      `restart_count` but never calls them itself);
    - it does not implement watchdog / heartbeat logic.

    Parameters
    ----------
    runs_dir:
        Directory where ``<role>_stderr.log`` files will be opened.
        Created if it does not already exist.
    command_builder:
        Function mapping a role to the `argv` used to spawn that
        child. Defaults to :func:`default_command_builder`.
    env:
        Source environment (defaults to ``os.environ``). The actual
        child environment is built from this via
        :meth:`build_child_env`.
    env_allowlist / denied_env_keys:
        Allow- and deny-lists used by :meth:`build_child_env`.
    terminate_timeout_s:
        Seconds to wait for a graceful exit before hard-killing.
    popen:
        Injectable factory; tests replace this with a `FakePopen`.
    clock:
        Injectable monotonic clock; tests use a fake to assert on
        `start_time`.
    """

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

    # ----- introspection -------------------------------------------------

    def child(self, role: str) -> ChildProcess | None:
        self._validate_role(role)
        return self._children.get(role)

    @property
    def children(self) -> Mapping[str, ChildProcess]:
        return dict(self._children)

    # ----- spawn / respawn -----------------------------------------------

    def spawn(self, role: str) -> ChildProcess:
        """Spawn a new child for `role`. The role must not be running."""
        self._validate_role(role)
        existing = self._children.get(role)
        if existing is not None and existing.is_alive():
            raise ChildAlreadyRunningError(role)
        if existing is not None:
            self._teardown_child(existing)
            self._children.pop(role, None)
        return self._spawn_internal(role, restart_count=0)

    def respawn(self, role: str) -> ChildProcess:
        """Kill the current child for `role` (if any) and spawn a fresh one.

        The new child's ``restart_count`` is the old child's
        ``restart_count + 1``. If no previous child exists, the new
        child starts with ``restart_count = 1`` (the very first spawn
        is via :meth:`spawn` and uses 0).
        """
        self._validate_role(role)
        old = self._children.get(role)
        old_count = old.restart_count if old is not None else 0
        if old is not None:
            self._teardown_child(old)
            self._children.pop(role, None)
        return self._spawn_internal(role, restart_count=old_count + 1)

    def _spawn_internal(self, role: str, restart_count: int) -> ChildProcess:
        command = self._command_builder(role)
        child_env = self.build_child_env(role)
        stderr_path = self.runs_dir / _STDERR_FILENAMES[role]
        stderr_fh = stderr_path.open("ab", buffering=0)

        try:
            process = self._popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_fh,
                env=child_env,
                bufsize=0,
            )
        except Exception:
            stderr_fh.close()
            raise

        cp = ChildProcess(
            role=role,
            process=process,
            stdin=process.stdin,
            stdout=process.stdout,
            stderr_path=stderr_path,
            start_time=self._clock(),
            restart_count=restart_count,
            stderr_fh=stderr_fh,
        )
        cp.reader_thread = threading.Thread(
            target=_reader_loop,
            args=(cp,),
            name=f"supervisor-reader-{role}",
            daemon=True,
        )
        cp.reader_thread.start()
        self._children[role] = cp
        return cp

    # ----- send / receive ------------------------------------------------

    def send(self, role: str, message: Message) -> None:
        """Send a `Message` to the child via JSONL on its stdin.

        Uses :func:`debate.orchestration.ipc.serialize_message` - the
        Supervisor never serializes JSON itself.
        """
        self._validate_role(role)
        child = self._children.get(role)
        if child is None or not child.is_alive():
            raise ChildNotRunningError(role)

        line = serialize_message(message)
        payload = line.encode("utf-8")
        try:
            child.stdin.write(payload)
            child.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ChildNotRunningError(role) from exc

    def receive(self, role: str, timeout: float | None = None) -> Message:
        """Read one `Message` from the child's stdout.

        Uses :func:`debate.orchestration.ipc.deserialize_message` -
        the Supervisor never parses JSON itself.

        Raises:
            ChildReceiveTimeoutError: ``timeout`` expired with no
                line available.
            ChildStreamClosedError: child closed stdout (EOF).
            ChildNotRunningError: no child registered for `role`.
        """
        self._validate_role(role)
        child = self._children.get(role)
        if child is None:
            raise ChildNotRunningError(role)

        try:
            if timeout is None:
                item = child.read_queue.get()
            else:
                item = child.read_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise ChildReceiveTimeoutError(role, timeout or 0.0) from exc

        if isinstance(item, _ReaderSentinel):
            raise ChildStreamClosedError(role)

        return deserialize_message(item)

    # ----- terminate -----------------------------------------------------

    def terminate(self, role: str) -> None:
        """Graceful-then-forced termination, then forget the child."""
        self._validate_role(role)
        child = self._children.get(role)
        if child is None:
            raise ChildNotRunningError(role)
        self._teardown_child(child)
        self._children.pop(role, None)

    def terminate_all(self) -> None:
        """Terminate every registered child. Never raises for missing ones."""
        for role in list(self._children):
            with contextlib.suppress(ChildNotRunningError):
                self.terminate(role)

    def _teardown_child(self, child: ChildProcess) -> None:
        proc = child.process

        if _safe_poll(proc) is None:
            _safe_close(child.stdin)
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=self._terminate_timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=self._terminate_timeout_s)
            except Exception:
                pass

        _safe_close(child.stdin)
        _safe_close(child.stdout)
        _safe_close(child.stderr_fh)

        if child.reader_thread is not None and child.reader_thread.is_alive():
            child.reader_thread.join(timeout=self._terminate_timeout_s)

    # ----- env -----------------------------------------------------------

    def build_child_env(self, role: str) -> dict[str, str]:
        """Build the env dict passed to a child process.

        - Only keys in ``env_allowlist`` are copied from the source
          environment.
        - Then every key in ``denied_env_keys`` is removed (defense
          in depth - so that adding a key to the allow-list does not
          accidentally leak a secret).
        - ``DEBATE_ROLE`` is set to the role.
        """
        out: dict[str, str] = {}
        for key in self._env_allowlist:
            if key in self._env_source:
                out[key] = self._env_source[key]
        for key in self._denied_env_keys:
            out.pop(key, None)
        out["DEBATE_ROLE"] = role
        return out

    # ----- helpers -------------------------------------------------------

    def _validate_role(self, role: str) -> None:
        if role not in VALID_ROLES:
            raise UnknownRoleError(role)

    # ----- context manager ----------------------------------------------

    def __enter__(self) -> Supervisor:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.terminate_all()


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _reader_loop(child: ChildProcess) -> None:
    """Background thread: drain `child.stdout` into `child.read_queue`."""
    stream = child.stdout
    try:
        while True:
            try:
                raw = stream.readline()
            except (ValueError, OSError):
                break
            if not raw:
                break
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            child.read_queue.put(line)
    finally:
        child.read_queue.put(_EOF)


def _safe_poll(proc: subprocess.Popen) -> int | None:
    try:
        return proc.poll()
    except Exception:
        return -1


def _safe_close(stream: IO[Any] | None) -> None:
    if stream is None:
        return
    try:
        if not getattr(stream, "closed", False):
            stream.close()
    except Exception:
        pass


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
]
