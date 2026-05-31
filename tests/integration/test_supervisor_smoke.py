"""Integration smoke test: actually spawn a Python child process and
talk to it through the Supervisor's JSONL pipes.

This complements the FakePopen-based unit tests by exercising the
real ``subprocess.Popen`` path, real OS pipes, and real stderr-to-file
capture end-to-end.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from debate.orchestration import Supervisor
from debate.sdk.schemas import Message, MessageType, Role

ECHO_SCRIPT: Path = Path(__file__).parent / "echo_child.py"


def _echo_command_builder(_role: str) -> list[str]:
    return [sys.executable, "-u", str(ECHO_SCRIPT)]


def _make_message(**overrides: Any) -> Message:
    base: dict[str, Any] = {
        "ts": 1.0,
        "turn_id": 0,
        "role": Role.PRO,
        "type": MessageType.PROMPT,
        "payload": {"text": "hello from the supervisor smoke test"},
    }
    base.update(overrides)
    return Message(**base)


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    p = tmp_path / "smoke-run"
    p.mkdir()
    return p


@pytest.fixture
def supervisor(runs_dir: Path):
    """Real Supervisor wired to ``echo_child.py``.

    Always tears down via ``terminate_all`` so a hung child cannot
    leak past the test boundary. ``SEARCH_API_KEY`` is set in the env
    source so the smoke test also verifies that it does *not* reach
    the child (combined with stderr-capture proof of the spawn).
    """
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "OPENAI_API_KEY": "sk-smoke",
        "SEARCH_API_KEY": "sk-smoke-search-MUST-NOT-LEAK",
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        "WINDIR": __import__("os").environ.get("WINDIR", ""),
        "COMSPEC": __import__("os").environ.get("COMSPEC", ""),
        "TEMP": __import__("os").environ.get("TEMP", ""),
        "TMP": __import__("os").environ.get("TMP", ""),
    }
    sup = Supervisor(
        runs_dir=runs_dir,
        command_builder=_echo_command_builder,
        env=env,
        terminate_timeout_s=3.0,
    )
    try:
        yield sup
    finally:
        sup.terminate_all()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSupervisorSmoke:
    def test_spawn_and_echo_round_trip(self, supervisor: Supervisor) -> None:
        cp = supervisor.spawn("pro")
        assert cp.is_alive()
        assert cp.pid is not None and cp.pid > 0

        msg = _make_message(payload={"text": "round-trip"})
        supervisor.send("pro", msg)

        echoed = supervisor.receive("pro", timeout=10.0)
        assert echoed == msg

    def test_spawn_both_roles(self, supervisor: Supervisor, runs_dir: Path) -> None:
        pro = supervisor.spawn("pro")
        con = supervisor.spawn("con")

        assert pro.pid != con.pid
        assert pro.stderr_path == runs_dir / "pro_stderr.log"
        assert con.stderr_path == runs_dir / "con_stderr.log"

        for role in ("pro", "con"):
            supervisor.send(role, _make_message(payload={"role_tag": role}))
            received = supervisor.receive(role, timeout=10.0)
            assert received.payload["role_tag"] == role

    def test_stderr_capture_file_receives_data(
        self, supervisor: Supervisor, runs_dir: Path
    ) -> None:
        supervisor.spawn("pro")
        time.sleep(0.5)
        supervisor.terminate("pro")

        path = runs_dir / "pro_stderr.log"
        assert path.exists()
        contents = path.read_bytes()
        assert b"echo_child" in contents, (
            f"expected child startup banner in stderr capture; got {contents!r}"
        )

    def test_terminate_actually_kills_the_process(self, supervisor: Supervisor) -> None:
        cp = supervisor.spawn("pro")
        assert cp.is_alive()
        supervisor.terminate("pro")
        assert not cp.is_alive()
        assert cp.process.poll() is not None

    def test_respawn_yields_a_new_pid_and_higher_restart_count(
        self, supervisor: Supervisor
    ) -> None:
        first = supervisor.spawn("pro")
        first_pid = first.pid

        second = supervisor.respawn("pro")
        assert second.pid != first_pid
        assert second.restart_count == first.restart_count + 1
        assert second.is_alive()
        assert not first.is_alive()

    def test_search_api_key_does_not_appear_in_env_passed_to_child(
        self, supervisor: Supervisor
    ) -> None:
        env = supervisor.build_child_env("pro")
        assert "SEARCH_API_KEY" not in env
        for v in env.values():
            assert "MUST-NOT-LEAK" not in v

    def test_terminate_all_cleans_up_both_children(self, supervisor: Supervisor) -> None:
        pro = supervisor.spawn("pro")
        con = supervisor.spawn("con")
        supervisor.terminate_all()
        assert not pro.is_alive()
        assert not con.is_alive()
        assert supervisor.child("pro") is None
        assert supervisor.child("con") is None
