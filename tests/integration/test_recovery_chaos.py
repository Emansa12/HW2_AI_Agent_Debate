"""Lightweight chaos integration test for the Stage 8 Watchdog.

This complements the unit tests in ``tests/unit/test_watchdog.py``
by exercising the real ``Supervisor`` + real ``subprocess.Popen`` +
real OS pipes through a tiny ping/pong child
(``tests/integration/heartbeat_child.py``).

Two scenarios:

1. Happy heartbeat: spawn the child, call ``Watchdog.check_once()``,
   confirm no miss.
2. Chaos: terminate the child out from under the Watchdog, call
   ``check_once()`` again, confirm the Watchdog reports a miss via
   the callback.

We deliberately do NOT exercise full recovery orchestration: there
is no respawn loop, no FSM, no Judge. Stage 8 is detection only.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from debate.orchestration.supervisor import Supervisor
from debate.orchestration.watchdog import Watchdog

HEARTBEAT_CHILD: Path = Path(__file__).parent / "heartbeat_child.py"


def _heartbeat_command_builder(_role: str) -> list[str]:
    return [sys.executable, "-u", str(HEARTBEAT_CHILD)]


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    p = tmp_path / "watchdog-run"
    p.mkdir()
    return p


@pytest.fixture
def supervisor(runs_dir: Path):
    """Real Supervisor wired to ``heartbeat_child.py``."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
    }
    sup = Supervisor(
        runs_dir=runs_dir,
        command_builder=_heartbeat_command_builder,
        env=env,
        terminate_timeout_s=3.0,
    )
    try:
        yield sup
    finally:
        sup.terminate_all()


class TestWatchdogChaos:
    def test_happy_heartbeat_then_kill_detected(self, supervisor: Supervisor) -> None:
        misses: list[str] = []
        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.0,
            heartbeat_timeout_sec=5.0,
            on_miss=misses.append,
            roles=("pro",),
        )

        cp = supervisor.spawn("pro")
        assert cp.is_alive()
        time.sleep(0.2)

        wd.check_once()
        assert misses == [], f"a healthy ping/pong cycle must not record a miss; got {misses!r}"

        supervisor.terminate("pro")

        wd.check_once()
        assert misses == ["pro"], (
            f"watchdog must report a miss after the child is terminated; got {misses!r}"
        )

    def test_background_loop_detects_dead_child(self, supervisor: Supervisor) -> None:
        """The background thread also detects a missed beat after termination."""
        miss_seen = threading.Event()
        captured: list[str] = []

        def on_miss(role: str) -> None:
            captured.append(role)
            miss_seen.set()

        wd = Watchdog(
            supervisor=supervisor,
            heartbeat_interval_sec=0.02,
            heartbeat_timeout_sec=1.0,
            on_miss=on_miss,
            roles=("pro",),
        )

        supervisor.spawn("pro")
        time.sleep(0.2)
        wd.start()
        try:
            time.sleep(0.2)
            supervisor.terminate("pro")
            assert miss_seen.wait(timeout=5.0), (
                "background watchdog did not observe the miss in time; "
                f"captured so far: {captured!r}"
            )
            assert "pro" in captured
        finally:
            wd.stop(timeout=3.0)
