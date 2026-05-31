"""Watchdog / health monitor for child agents (Stage 8).

The Watchdog runs on the Judge / parent side. It periodically asks
each child (Pro, Con) "are you still alive?" via the heartbeat
protocol already implemented by :class:`debate.agents.base_agent.BaseAgent`
(``ping`` -> ``pong``), going through the Supervisor's JSONL pipes.

Responsibilities
----------------

The Watchdog only **detects** liveness failures. It does **not**:

- decide what to do about them (no respawn, no FSM transitions);
- run the debate loop or own any debate state;
- know about Judge verdict logic, Pro/Con prompts, or the LLM.

When a miss is detected for a role, it calls the user-supplied
``on_miss(role)`` callback. The Judge / FSM layer added in later
stages will be the one that turns ``on_miss`` into one of the
``heartbeat_miss`` / ``respawned`` / ``restarts_exhausted`` events
from :mod:`debate.orchestration.state_machine`.

What counts as a "heartbeat miss"
---------------------------------

For each configured role, exactly one of these happens per
:meth:`Watchdog.check_once` call:

1. **No child** registered for ``role`` (`supervisor.child(role)` is
   ``None``) or the child is no longer alive
   (``child.is_alive() is False``). The Watchdog records a miss
   *without* sending a ping.
2. The Watchdog tries to send a ``ping`` but the Supervisor raises
   (``ChildNotRunningError`` etc.). Miss.
3. ``supervisor.receive(role, timeout=heartbeat_timeout_sec)`` raises
   ``ChildReceiveTimeoutError``. Miss.
4. ``supervisor.receive`` raises ``ChildStreamClosedError`` (EOF).
   Miss.
5. ``supervisor.receive`` raises any other ``SupervisorError``,
   ``IPCError``, or unexpected exception. Miss (defensive).
6. The reply ``Message`` has ``type != MessageType.PONG``. Miss
   ("malformed pong" - the wire still parsed, but it was not a
   pong).

Anything else is a successful heartbeat, which is what we want.

Threading model
---------------

:meth:`start` spawns one background daemon thread that repeatedly
calls :meth:`check_once`, then waits on an internal
:class:`threading.Event` for ``heartbeat_interval_sec`` seconds.
:meth:`stop` sets that event and joins the thread.

:meth:`check_once` itself is fully synchronous and easy to unit
test: tests can drive it directly with a fake Supervisor without
ever starting a thread.

Stage boundary
--------------

This module deliberately does **not** import:

- :mod:`debate.agents.*` (Pro / Con / Debater agents);
- :mod:`debate.judge.*` (Judge logic);
- :mod:`debate.orchestration.state_machine` (the FSM stays pure;
  the Judge layer in Stage 9/10 is what wires this Watchdog into
  the FSM).

It also never calls :meth:`debate.orchestration.supervisor.Supervisor.respawn`.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from debate.orchestration.ipc import IPCError
from debate.orchestration.supervisor import (
    ChildNotRunningError,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    Supervisor,
    SupervisorError,
)
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role

logger = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_INTERVAL_SEC: float = 1.0
"""Default seconds between consecutive :meth:`Watchdog.check_once` calls."""

DEFAULT_HEARTBEAT_TIMEOUT_SEC: float = 0.5
"""Default seconds to wait for a ``pong`` before declaring a miss."""

DEFAULT_ROLES: tuple[str, ...] = ("pro", "con")
"""Roles the Watchdog monitors by default."""


class MissReason:
    """Symbolic strings the Watchdog uses when reporting a miss.

    Kept as a tiny constants holder rather than an enum to avoid
    over-engineering and to keep log payloads JSON-trivial.
    """

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


class Watchdog:
    """Liveness monitor for Pro / Con children, sitting on the Judge side.

    Parameters
    ----------
    supervisor:
        The :class:`Supervisor` whose children we monitor. We only
        use its public ``child`` / ``send`` / ``receive`` surface;
        we never call ``respawn``.
    heartbeat_interval_sec:
        Seconds between consecutive ``check_once`` cycles when run
        in the background via :meth:`start`. ``check_once`` itself
        does not consult this; tests can call it directly.
    heartbeat_timeout_sec:
        Seconds to wait for a ``pong`` after sending a ``ping``
        before marking the role as having missed a beat.
    on_miss:
        Callback invoked with the role string (e.g. ``"pro"``) when
        a miss is detected. Exceptions raised here are swallowed
        and logged so a buggy callback cannot crash the watchdog
        thread.
    roles:
        Iterable of role strings to monitor. Defaults to
        ``("pro", "con")``. The order is preserved across
        :meth:`check_once` so behavior is deterministic.
    run_logger:
        Optional duck-typed logger object with a
        ``log(role, turn_id, event_type, **fields)`` method (e.g.
        :class:`debate.shared.logger.RunLogger`). Used to emit
        ``watchdog_ping`` / ``watchdog_pong`` / ``watchdog_miss``
        events. Logger errors are swallowed.
    clock:
        Injectable monotonic-ish clock returning epoch-style
        seconds. Used as the ``ts`` field on outgoing ``ping``
        messages.
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        heartbeat_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
        heartbeat_timeout_sec: float = DEFAULT_HEARTBEAT_TIMEOUT_SEC,
        on_miss: OnMissCallback,
        roles: Iterable[str] = DEFAULT_ROLES,
        run_logger: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if heartbeat_interval_sec < 0:
            raise ValueError("heartbeat_interval_sec must be >= 0")
        if heartbeat_timeout_sec <= 0:
            raise ValueError("heartbeat_timeout_sec must be > 0")
        if on_miss is None:
            raise ValueError("on_miss callback is required")

        self._supervisor: Supervisor = supervisor
        self._heartbeat_interval_sec: float = float(heartbeat_interval_sec)
        self._heartbeat_timeout_sec: float = float(heartbeat_timeout_sec)
        self._on_miss: OnMissCallback = on_miss
        self._roles: tuple[str, ...] = tuple(roles)
        if not self._roles:
            raise ValueError("at least one role must be monitored")

        self._run_logger: Any = run_logger
        self._clock: Callable[[], float] = clock

        self._stop_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ping_counter: int = 0

    # ----- public API ---------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the background thread is currently alive."""
        t = self._thread
        return t is not None and t.is_alive() and not self._stop_event.is_set()

    @property
    def roles(self) -> tuple[str, ...]:
        return self._roles

    def start(self) -> None:
        """Start the background heartbeat thread.

        Idempotent: calling ``start`` a second time while a previous
        thread is still alive does nothing.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="debate-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Signal the background thread to exit and join it.

        Safe to call even if :meth:`start` was never called.
        """
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def check_once(self) -> None:
        """Run exactly one heartbeat round for every monitored role.

        Deterministic and single-threaded - the unit tests call this
        directly without going through :meth:`start` / :meth:`stop`.
        """
        for role in self._roles:
            self._check_role(role)

    # ----- internals ----------------------------------------------------

    def _loop(self) -> None:
        """Background thread body: check_once, wait, repeat."""
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("watchdog check_once raised; continuing")
            if self._stop_event.wait(self._heartbeat_interval_sec):
                break

    def _check_role(self, role: str) -> None:
        child = self._supervisor.child(role)
        if child is None:
            self._record_miss(role, MissReason.NO_CHILD, ping_turn_id=None)
            return
        if not child.is_alive():
            self._record_miss(role, MissReason.CHILD_NOT_ALIVE, ping_turn_id=None)
            return

        ping = self._build_ping()
        try:
            self._supervisor.send(role, ping)
        except ChildNotRunningError:
            self._record_miss(role, MissReason.SEND_FAILED, ping_turn_id=ping.turn_id)
            return
        except SupervisorError:
            self._record_miss(role, MissReason.SUPERVISOR_ERROR, ping_turn_id=ping.turn_id)
            return
        except Exception:  # noqa: BLE001 - never let an obscure send error kill us
            self._record_miss(role, MissReason.UNEXPECTED_ERROR, ping_turn_id=ping.turn_id)
            return

        self._log_event(
            event_type="watchdog_ping",
            turn_id=ping.turn_id,
            target_role=role,
        )

        try:
            reply = self._supervisor.receive(role, timeout=self._heartbeat_timeout_sec)
        except ChildReceiveTimeoutError:
            self._record_miss(role, MissReason.TIMEOUT, ping_turn_id=ping.turn_id)
            return
        except ChildStreamClosedError:
            self._record_miss(role, MissReason.STREAM_CLOSED, ping_turn_id=ping.turn_id)
            return
        except IPCError:
            self._record_miss(role, MissReason.IPC_ERROR, ping_turn_id=ping.turn_id)
            return
        except SupervisorError:
            self._record_miss(role, MissReason.SUPERVISOR_ERROR, ping_turn_id=ping.turn_id)
            return
        except Exception:  # noqa: BLE001
            self._record_miss(role, MissReason.UNEXPECTED_ERROR, ping_turn_id=ping.turn_id)
            return

        if reply.type is not MessageType.PONG:
            self._record_miss(
                role,
                MissReason.NOT_A_PONG,
                ping_turn_id=ping.turn_id,
                actual_type=reply.type.value,
            )
            return

        self._log_event(
            event_type="watchdog_pong",
            turn_id=ping.turn_id,
            target_role=role,
            pong_turn_id=reply.turn_id,
        )

    def _build_ping(self) -> Message:
        self._ping_counter += 1
        turn_id = self._ping_counter
        ts = float(self._clock())
        if ts < 0.0:
            ts = 0.0
        return Message(
            v=SCHEMA_VERSION,
            ts=ts,
            turn_id=turn_id,
            role=Role.JUDGE,
            type=MessageType.PING,
            payload={"watchdog_ping_id": turn_id},
        )

    def _record_miss(
        self,
        role: str,
        reason: str,
        *,
        ping_turn_id: int | None,
        **extra: Any,
    ) -> None:
        self._log_event(
            event_type="watchdog_miss",
            turn_id=ping_turn_id if ping_turn_id is not None else 0,
            target_role=role,
            reason=reason,
            **extra,
        )
        try:
            self._on_miss(role)
        except Exception:  # noqa: BLE001 - protect the watchdog thread
            logger.exception("watchdog on_miss callback raised for role=%s", role)

    def _log_event(self, *, event_type: str, turn_id: int, **fields: Any) -> None:
        if self._run_logger is None:
            return
        with contextlib.suppress(Exception):
            self._run_logger.log(
                role="watchdog",
                turn_id=turn_id,
                event_type=event_type,
                **fields,
            )

    # ----- context manager ---------------------------------------------

    def __enter__(self) -> Watchdog:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SEC",
    "DEFAULT_HEARTBEAT_TIMEOUT_SEC",
    "DEFAULT_ROLES",
    "MissReason",
    "OnMissCallback",
    "Watchdog",
]
