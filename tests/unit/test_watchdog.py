"""Unit tests for :mod:`debate.orchestration.watchdog`.

These tests drive the Watchdog with a ``FakeSupervisor`` test
double, so no real subprocesses, OS pipes, or threads-with-sleep
are needed for the core logic. The one thread test uses tiny
intervals and a hard upper-bound on join time.
"""

from __future__ import annotations

import inspect
import queue
import threading
import time
from collections import deque
from typing import Any

import pytest

from debate.orchestration import watchdog as watchdog_module
from debate.orchestration.ipc import MalformedMessageError
from debate.orchestration.supervisor import (
    ChildNotRunningError,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
)
from debate.orchestration.watchdog import (
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_HEARTBEAT_TIMEOUT_SEC,
    DEFAULT_ROLES,
    MissReason,
    Watchdog,
)
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeChild:
    """Mimics the bit of :class:`ChildProcess` the Watchdog reads."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

    def die(self) -> None:
        self._alive = False


class FakeSupervisor:
    """Minimal Supervisor double matching the surface the Watchdog uses.

    Tests can:
    - register a child via ``set_child(role, FakeChild)``;
    - pre-load the next ``receive`` response (either a ``Message``
      or an exception class/instance) via ``queue_receive``;
    - inspect every ``send`` via ``sent``.
    """

    def __init__(self) -> None:
        self._children: dict[str, FakeChild | None] = {"pro": None, "con": None}
        self._receive_queue: dict[str, deque[Any]] = {"pro": deque(), "con": deque()}
        self.sent: list[tuple[str, Message]] = []
        self.send_exceptions: dict[str, deque[BaseException]] = {
            "pro": deque(),
            "con": deque(),
        }

    def set_child(self, role: str, child: FakeChild | None) -> None:
        self._children[role] = child

    def child(self, role: str) -> FakeChild | None:
        return self._children.get(role)

    def queue_receive(self, role: str, item: Any) -> None:
        """Push a Message instance or an Exception (class or instance)."""
        self._receive_queue[role].append(item)

    def queue_send_exception(self, role: str, exc: BaseException) -> None:
        self.send_exceptions[role].append(exc)

    def send(self, role: str, message: Message) -> None:
        self.sent.append((role, message))
        if self.send_exceptions[role]:
            raise self.send_exceptions[role].popleft()

    def receive(self, role: str, timeout: float | None = None) -> Message:
        del timeout
        if not self._receive_queue[role]:
            raise ChildReceiveTimeoutError(role, 0.0)
        item = self._receive_queue[role].popleft()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            # Some test helpers pass the class - instantiate sensibly.
            if issubclass(item, ChildReceiveTimeoutError):
                raise item(role, 0.0)
            if issubclass(item, (ChildStreamClosedError, ChildNotRunningError)):
                raise item(role)
            raise item()
        return item


class RecordingLogger:
    """Captures every ``log(...)`` call the Watchdog makes."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, *, role: str, turn_id: int, event_type: str, **fields: Any) -> None:
        self.events.append(
            {
                "role": role,
                "turn_id": turn_id,
                "event_type": event_type,
                **fields,
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pong(role: Role, in_reply_to: int, *, turn_id: int = 99) -> Message:
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=turn_id,
        role=role,
        type=MessageType.PONG,
        payload={"in_reply_to": in_reply_to},
    )


def _make_reply(role: Role) -> Message:
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=7,
        role=role,
        type=MessageType.REPLY,
        payload={"text": "not a pong"},
    )


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def misses() -> list[str]:
    return []


@pytest.fixture
def make_watchdog(supervisor: FakeSupervisor, misses: list[str]):
    def _make(**overrides: Any) -> Watchdog:
        kwargs: dict[str, Any] = {
            "supervisor": supervisor,
            "heartbeat_interval_sec": 0.0,
            "heartbeat_timeout_sec": 0.05,
            "on_miss": misses.append,
            "clock": lambda: 100.0,
        }
        kwargs.update(overrides)
        return Watchdog(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_roles(self, make_watchdog) -> None:
        wd = make_watchdog()
        assert wd.roles == DEFAULT_ROLES
        assert DEFAULT_ROLES == ("pro", "con")

    def test_explicit_roles_preserve_order(self, make_watchdog) -> None:
        wd = make_watchdog(roles=("con", "pro"))
        assert wd.roles == ("con", "pro")

    def test_is_running_starts_false(self, make_watchdog) -> None:
        wd = make_watchdog()
        assert wd.is_running is False

    def test_rejects_negative_interval(self, make_watchdog) -> None:
        with pytest.raises(ValueError):
            make_watchdog(heartbeat_interval_sec=-0.1)

    def test_rejects_non_positive_timeout(self, make_watchdog) -> None:
        with pytest.raises(ValueError):
            make_watchdog(heartbeat_timeout_sec=0.0)
        with pytest.raises(ValueError):
            make_watchdog(heartbeat_timeout_sec=-1.0)

    def test_rejects_empty_roles(self, make_watchdog) -> None:
        with pytest.raises(ValueError):
            make_watchdog(roles=())

    def test_defaults_documented_are_sane(self) -> None:
        assert DEFAULT_HEARTBEAT_INTERVAL_SEC > 0
        assert DEFAULT_HEARTBEAT_TIMEOUT_SEC > 0


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestSuccessfulHeartbeat:
    def test_pro_ping_pong_does_not_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=1))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=2))

        wd = make_watchdog()
        wd.check_once()

        assert misses == []
        assert len(supervisor.sent) == 2
        roles_pinged = [r for r, _ in supervisor.sent]
        assert roles_pinged == ["pro", "con"]

    def test_con_ping_pong_does_not_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=1))

        wd = make_watchdog(roles=("con",))
        wd.check_once()

        assert misses == []
        assert [r for r, _ in supervisor.sent] == ["con"]
        sent_msg = supervisor.sent[0][1]
        assert sent_msg.type is MessageType.PING

    def test_ping_increments_turn_id_across_calls(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))
        for _ in range(4):
            supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))
            supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        wd = make_watchdog()
        wd.check_once()
        wd.check_once()

        assert misses == []
        turn_ids = [m.turn_id for _, m in supervisor.sent]
        assert turn_ids == sorted(turn_ids), "ping turn_ids must be monotonic"
        assert len(set(turn_ids)) == len(turn_ids), "ping turn_ids must be unique"


# ---------------------------------------------------------------------------
# Miss scenarios
# ---------------------------------------------------------------------------


class TestMissScenarios:
    def test_no_child_for_role_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", None)
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        wd = make_watchdog()
        wd.check_once()

        assert misses == ["pro"]
        assert [r for r, _ in supervisor.sent] == ["con"], (
            "no ping should be sent to a role that has no child"
        )

    def test_dead_child_triggers_miss_without_sending(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=False))
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        wd = make_watchdog()
        wd.check_once()

        assert misses == ["pro"]
        assert [r for r, _ in supervisor.sent] == ["con"]

    def test_timeout_on_receive_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()

        assert misses == ["pro"]
        assert len(supervisor.sent) == 1, "ping was sent before the timeout"

    def test_explicit_timeout_exception_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", ChildReceiveTimeoutError("pro", 0.1))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_stream_closed_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", ChildStreamClosedError("pro"))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_send_failure_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_send_exception("pro", ChildNotRunningError("pro"))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_malformed_pong_is_not_a_pong(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_reply(Role.PRO))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_ipc_error_during_receive_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", MalformedMessageError("garbage"))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_unexpected_exception_during_receive_triggers_miss(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", RuntimeError("oh no"))
        wd = make_watchdog(roles=("pro",))
        wd.check_once()
        assert misses == ["pro"]

    def test_on_miss_exception_does_not_crash_check(
        self,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))
        # pro misses; on_miss raises; con must still be checked.
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        def boom(role: str) -> None:
            raise RuntimeError(f"boom for {role}")

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.0,
            heartbeat_timeout_sec=0.01,
            on_miss=boom,
        )
        wd.check_once()  # must not raise

        assert [r for r, _ in supervisor.sent] == ["pro", "con"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_check_once_visits_roles_in_configured_order(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        wd = make_watchdog(roles=("con", "pro"))
        wd.check_once()

        assert [r for r, _ in supervisor.sent] == ["con", "pro"]

    def test_check_once_is_pure_per_call(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
        misses: list[str],
    ) -> None:
        """Two identical setups yield two identical miss / send sequences."""
        supervisor.set_child("pro", None)
        supervisor.set_child("con", None)

        wd = make_watchdog()
        wd.check_once()
        wd.check_once()

        assert misses == ["pro", "con", "pro", "con"]
        assert supervisor.sent == []


# ---------------------------------------------------------------------------
# Ping wire-format
# ---------------------------------------------------------------------------


class TestPingMessage:
    def test_ping_uses_schema_types(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))

        wd = make_watchdog(roles=("pro",))
        wd.check_once()

        assert len(supervisor.sent) == 1
        sent_role, msg = supervisor.sent[0]
        assert sent_role == "pro"
        assert isinstance(msg, Message)
        assert msg.type is MessageType.PING
        assert msg.role is Role.JUDGE
        assert msg.v == SCHEMA_VERSION
        assert msg.turn_id >= 0
        assert msg.ts >= 0.0
        assert isinstance(msg.payload, dict)
        assert "watchdog_ping_id" in msg.payload

    def test_ping_ts_uses_injected_clock(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))

        wd = make_watchdog(roles=("pro",), clock=lambda: 1234.5)
        wd.check_once()

        _, msg = supervisor.sent[0]
        assert msg.ts == 1234.5

    def test_ping_clamps_negative_clock_to_zero(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))

        wd = make_watchdog(roles=("pro",), clock=lambda: -5.0)
        wd.check_once()
        _, msg = supervisor.sent[0]
        assert msg.ts == 0.0


# ---------------------------------------------------------------------------
# Threaded start/stop
# ---------------------------------------------------------------------------


class TestThreadLifecycle:
    def test_start_runs_check_once_in_background(
        self,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))

        # Enqueue many pongs so the loop has work for as many cycles
        # as it manages to fit into the join window.
        for _ in range(50):
            supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))
            supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        ticks = queue.Queue()

        def on_miss_capture(role: str) -> None:
            ticks.put(("miss", role))

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.05,
            on_miss=on_miss_capture,
        )
        wd.start()
        # Tight busy-wait until at least one ping was sent or 1s passes.
        deadline = time.time() + 1.0
        while time.time() < deadline and len(supervisor.sent) < 1:
            time.sleep(0.005)
        wd.stop(timeout=2.0)

        assert len(supervisor.sent) >= 1
        assert wd.is_running is False

    def test_stop_sets_thread_to_none_and_stops_cleanly(
        self,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        wd.start()
        wd.stop(timeout=2.0)
        assert wd.is_running is False
        # second stop must be a no-op
        wd.stop(timeout=0.5)

    def test_start_is_idempotent(self, supervisor: FakeSupervisor) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        wd.start()
        # Capture the thread, call start again, expect the SAME thread.
        t1 = wd._thread
        wd.start()
        t2 = wd._thread
        assert t1 is t2
        wd.stop(timeout=2.0)

    def test_loop_swallows_check_once_exceptions(
        self,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))

        class BoomWD(Watchdog):
            calls = 0

            def check_once(self) -> None:
                BoomWD.calls += 1
                if BoomWD.calls == 1:
                    raise RuntimeError("first call explodes")

        wd = BoomWD(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        wd.start()
        deadline = time.time() + 1.0
        while time.time() < deadline and BoomWD.calls < 3:
            time.sleep(0.005)
        wd.stop(timeout=2.0)
        assert BoomWD.calls >= 2, "loop must keep running after a check_once exception"

    def test_context_manager_starts_and_stops(self, supervisor: FakeSupervisor) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        with wd:
            assert wd.is_running is True or wd._thread is not None
        assert wd.is_running is False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_logs_ping_and_pong_on_success(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))

        rl = RecordingLogger()
        wd = make_watchdog(roles=("pro",), run_logger=rl)
        wd.check_once()

        types = [e["event_type"] for e in rl.events]
        assert "watchdog_ping" in types
        assert "watchdog_pong" in types
        # every event we emit must be on the watchdog role channel
        assert {e["role"] for e in rl.events} == {"watchdog"}

    def test_logs_miss_with_reason(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        supervisor.set_child("pro", None)
        supervisor.set_child("con", FakeChild(alive=True))
        supervisor.queue_receive("con", _make_pong(Role.CON, in_reply_to=0))

        rl = RecordingLogger()
        wd = make_watchdog(run_logger=rl)
        wd.check_once()

        miss_events = [e for e in rl.events if e["event_type"] == "watchdog_miss"]
        assert len(miss_events) == 1
        assert miss_events[0]["target_role"] == "pro"
        assert miss_events[0]["reason"] == MissReason.NO_CHILD

    def test_logger_exception_does_not_crash_check(
        self,
        make_watchdog,
        supervisor: FakeSupervisor,
    ) -> None:
        class BoomLogger:
            def log(self, **_: Any) -> None:
                raise RuntimeError("logger blew up")

        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.queue_receive("pro", _make_pong(Role.PRO, in_reply_to=0))

        wd = make_watchdog(roles=("pro",), run_logger=BoomLogger())
        wd.check_once()  # must not raise


# ---------------------------------------------------------------------------
# Stage boundary
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORT_TOKENS = (
    "from debate.agents",
    "import debate.agents",
    "from debate.orchestration.judge",
    "import debate.orchestration.judge",
    "JudgeAgent",
    "ProAgent",
    "ConAgent",
    "from debate.main",
    "import debate.main",
)


_FORBIDDEN_CALL_TOKENS = (
    ".respawn(",
    "supervisor.respawn",
)


class TestStageBoundary:
    def test_watchdog_does_not_import_judge_or_agents(self) -> None:
        src = inspect.getsource(watchdog_module)
        for token in _FORBIDDEN_IMPORT_TOKENS:
            assert token not in src, (
                f"watchdog.py must not reference {token!r}; "
                "Judge and agent modules belong to later stages"
            )

    def test_watchdog_does_not_call_supervisor_respawn(self) -> None:
        src = inspect.getsource(watchdog_module)
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src, (
                f"watchdog.py must not call {token!r}; "
                "recovery decisions belong to the Judge/FSM layer in Stage 9/10"
            )

    def test_on_miss_callback_is_the_only_recovery_path(
        self,
        supervisor: FakeSupervisor,
    ) -> None:
        """If on_miss is the only escape hatch, removing it must not be possible."""
        with pytest.raises(TypeError):
            Watchdog(  # type: ignore[call-arg]
                supervisor=supervisor,
                heartbeat_interval_sec=0.01,
                heartbeat_timeout_sec=0.01,
            )

    def test_watchdog_does_not_call_state_machine(self) -> None:
        src = inspect.getsource(watchdog_module)
        for token in (
            "DebateStateMachine",
            "from debate.orchestration.state_machine",
            "import debate.orchestration.state_machine",
        ):
            assert token not in src, (
                f"watchdog.py must not touch the FSM directly ({token!r}); the Judge layer mediates"
            )


# ---------------------------------------------------------------------------
# Sanity: monitored roles default and threading primitives
# ---------------------------------------------------------------------------


class TestMisc:
    def test_thread_is_daemon(self, supervisor: FakeSupervisor) -> None:
        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        wd.start()
        try:
            assert wd._thread is not None
            assert wd._thread.daemon is True
        finally:
            wd.stop(timeout=2.0)

    def test_stop_without_start_is_safe(self, make_watchdog) -> None:
        wd = make_watchdog()
        wd.stop()  # never called start; must not raise
        assert wd.is_running is False

    def test_threading_event_is_used_for_stop(self) -> None:
        """Sanity: the implementation should rely on a threading.Event."""
        src = inspect.getsource(watchdog_module)
        assert "threading.Event" in src or "Event()" in src

    def test_supervisor_attribute_is_not_a_supervisor_subclass_call(
        self,
        supervisor: FakeSupervisor,
        make_watchdog,
    ) -> None:
        """Just confirm the Watchdog stores its supervisor and uses it
        through duck typing (no isinstance gate that would reject the
        FakeSupervisor)."""
        wd = make_watchdog()
        assert wd._supervisor is supervisor
        # If isinstance(Supervisor) was being enforced, construction
        # with FakeSupervisor would have failed already.

    def test_threaded_run_does_not_leak_threads(self, supervisor: FakeSupervisor) -> None:
        supervisor.set_child("pro", FakeChild(alive=True))
        supervisor.set_child("con", FakeChild(alive=True))

        before = threading.active_count()
        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.001,
            heartbeat_timeout_sec=0.01,
            on_miss=lambda _r: None,
        )
        wd.start()
        time.sleep(0.02)
        wd.stop(timeout=2.0)
        # Allow a small window for OS to reap the joined thread.
        for _ in range(20):
            if threading.active_count() <= before:
                break
            time.sleep(0.02)
        assert threading.active_count() <= before + 0, "watchdog thread must not survive stop()"
