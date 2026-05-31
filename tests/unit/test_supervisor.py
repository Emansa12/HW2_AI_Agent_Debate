"""Unit tests for :mod:`debate.orchestration.supervisor`.

These tests use a ``FakePopen`` that wires real OS pipes for stdin /
stdout so the Supervisor's reader thread is exercised end-to-end,
but no real child process is spawned.

The smoke / integration test for actually spawning a Python child
lives in ``tests/integration/test_supervisor_smoke.py``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, Any

import pytest

from debate.orchestration import (
    ChildAlreadyRunningError,
    ChildNotRunningError,
    ChildProcess,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    Supervisor,
    UnknownRoleError,
    serialize_message,
)
from debate.orchestration import supervisor as supervisor_module
from debate.sdk.schemas import Message, MessageType, Role

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakePopen:
    """Mimics ``subprocess.Popen`` using real ``os.pipe`` pipes.

    Tests can:
    - push raw lines onto the simulated stdout via ``push_stdout_line``;
    - read everything the supervisor wrote to stdin via
      ``read_stdin_until_close`` (after the supervisor closes its
      write end) or by polling ``stdin_chunks``;
    - decide whether ``terminate()`` actually exits (default: yes) or
      whether ``kill()`` is needed (set ``terminate_actually_exits =
      False`` before calling ``terminate``).
    """

    _next_pid = 70_000
    _lock = threading.Lock()

    def __init__(
        self,
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: IO[Any] | int | None = None,
        env: dict[str, str] | None = None,
        bufsize: int = 0,
        **_kwargs: Any,
    ) -> None:
        assert stdin == subprocess.PIPE
        assert stdout == subprocess.PIPE

        self.command: list[str] = list(command)
        self.env_arg: dict[str, str] | None = dict(env) if env is not None else None
        self.stderr_arg = stderr

        stdin_r_fd, stdin_w_fd = os.pipe()
        stdout_r_fd, stdout_w_fd = os.pipe()

        self._stdin_r: IO[bytes] = os.fdopen(stdin_r_fd, "rb")
        self.stdin: IO[bytes] = os.fdopen(stdin_w_fd, "wb", buffering=0)
        self.stdout: IO[bytes] = os.fdopen(stdout_r_fd, "rb")
        self._stdout_w: IO[bytes] = os.fdopen(stdout_w_fd, "wb", buffering=0)

        with FakePopen._lock:
            FakePopen._next_pid += 1
            self.pid: int = FakePopen._next_pid

        self._returncode: int | None = None
        self.terminate_called: bool = False
        self.kill_called: bool = False
        self.wait_timeouts: list[float | None] = []
        self.terminate_actually_exits: bool = True

    # ----- subprocess.Popen-like API ------------------------------------

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_called = True
        if self.terminate_actually_exits:
            self._set_exit(0)

    def kill(self) -> None:
        self.kill_called = True
        self._set_exit(-9)

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._returncode is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)
            self._set_exit(0)
        return self._returncode  # type: ignore[return-value]

    # ----- test helpers --------------------------------------------------

    def push_stdout_line(self, payload: bytes) -> None:
        if not payload.endswith(b"\n"):
            payload = payload + b"\n"
        self._stdout_w.write(payload)
        self._stdout_w.flush()

    def read_stdin_line(self, timeout: float = 1.0) -> bytes:
        """Read one newline-terminated line the supervisor wrote.

        Blocks until a newline is available or ``timeout`` seconds
        elapse. Implemented with a background thread because Windows
        ``os.pipe`` reads cannot be interrupted by ``select``.
        """
        result: dict[str, bytes | BaseException] = {}

        def _reader() -> None:
            try:
                result["data"] = self._stdin_r.readline()
            except BaseException as exc:  # noqa: BLE001
                result["exc"] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError("read_stdin_line: no data in time")
        if "exc" in result:
            raise result["exc"]  # type: ignore[misc]
        return result["data"]  # type: ignore[return-value]

    def close_internal_handles(self) -> None:
        for stream in (self._stdin_r, self._stdout_w):
            try:
                if not stream.closed:
                    stream.close()
            except OSError:
                pass

    def _set_exit(self, code: int) -> None:
        if self._returncode is None:
            self._returncode = code
        try:
            if not self._stdout_w.closed:
                self._stdout_w.close()
        except OSError:
            pass


class PopenFactory:
    """Captures every ``FakePopen`` the Supervisor creates."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.instances: list[FakePopen] = []

    def __call__(self, command: list[str], **kwargs: Any) -> FakePopen:
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        fp = FakePopen(command, **kwargs)
        self.instances.append(fp)
        return fp

    @property
    def last(self) -> FakePopen:
        return self.instances[-1]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_message(**overrides: Any) -> Message:
    base: dict[str, Any] = {
        "ts": 1.0,
        "turn_id": 0,
        "role": Role.PRO,
        "type": MessageType.PROMPT,
        "payload": {"text": "hello"},
    }
    base.update(overrides)
    return Message(**base)


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    p = tmp_path / "run-abc"
    p.mkdir()
    return p


@pytest.fixture
def factory() -> PopenFactory:
    return PopenFactory()


@pytest.fixture
def env_source() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "PYTHONUNBUFFERED": "1",
        "OPENAI_API_KEY": "sk-test-llm-key",
        "OPENAI_BASE_URL": "https://api.example.com",
        "OPENAI_MODEL": "gpt-test",
        "SEARCH_API_KEY": "sk-test-search-key-MUST-NOT-LEAK",
        "MY_ARBITRARY_VAR": "leak-me",
        "DEBATE_ROUNDS": "10",
        "DEBATE_MAX_TOKENS": "400",
        "SYSTEMROOT": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "TEMP": r"C:\Users\test\AppData\Local\Temp",
    }


def _cmd_builder(role: str) -> list[str]:
    return ["python", "-m", f"fake_agent_{role}"]


@pytest.fixture
def make_supervisor(runs_dir: Path, factory: PopenFactory, env_source: dict[str, str]):
    created: list[Supervisor] = []

    def _make(**overrides: Any) -> Supervisor:
        kwargs: dict[str, Any] = {
            "runs_dir": runs_dir,
            "command_builder": _cmd_builder,
            "env": env_source,
            "popen": factory,
            "terminate_timeout_s": 0.5,
        }
        kwargs.update(overrides)
        sup = Supervisor(**kwargs)
        created.append(sup)
        return sup

    yield _make

    for sup in created:
        with contextlib.suppress(Exception):
            sup.terminate_all()
    for fp in factory.instances:
        fp.close_internal_handles()


# ---------------------------------------------------------------------------
# Role validation
# ---------------------------------------------------------------------------


class TestRoleValidation:
    @pytest.mark.parametrize(
        "method",
        ["spawn", "send", "receive", "terminate", "respawn", "child"],
    )
    def test_unknown_role_rejected(self, make_supervisor, method: str) -> None:
        sup = make_supervisor()
        target = getattr(sup, method)

        with pytest.raises(UnknownRoleError) as exc:
            if method == "send":
                target("judge", _make_message())
            elif method == "receive":
                target("judge", timeout=0.01)
            else:
                target("judge")

        assert exc.value.role == "judge"

    @pytest.mark.parametrize("role", ["", "PRO", "Pro", "moderator", "  pro"])
    def test_obviously_wrong_role_strings_rejected(self, make_supervisor, role: str) -> None:
        sup = make_supervisor()
        with pytest.raises(UnknownRoleError):
            sup.spawn(role)


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.parametrize("role", ["pro", "con"])
    def test_spawn_creates_child(self, make_supervisor, factory, role: str) -> None:
        sup = make_supervisor()
        cp = sup.spawn(role)

        assert isinstance(cp, ChildProcess)
        assert cp.role == role
        assert cp.is_alive()
        assert cp.restart_count == 0
        assert cp.pid is not None
        assert sup.child(role) is cp
        assert factory.calls[-1]["command"] == _cmd_builder(role)

    @pytest.mark.parametrize(
        ("role", "expected_filename"),
        [("pro", "pro_stderr.log"), ("con", "con_stderr.log")],
    )
    def test_spawn_creates_stderr_file(
        self, make_supervisor, runs_dir, role: str, expected_filename: str
    ) -> None:
        sup = make_supervisor()
        cp = sup.spawn(role)

        expected_path = runs_dir / expected_filename
        assert cp.stderr_path == expected_path
        assert expected_path.exists(), "stderr log file should be created on spawn"

    def test_spawn_records_start_time(self, make_supervisor) -> None:
        clock_values = iter([100.0, 200.0])
        sup = make_supervisor(clock=lambda: next(clock_values))
        cp = sup.spawn("pro")
        assert cp.start_time == 100.0

    def test_spawn_twice_same_role_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        with pytest.raises(ChildAlreadyRunningError):
            sup.spawn("pro")

    def test_spawn_passes_stderr_handle_to_popen(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        kwargs = factory.calls[-1]["kwargs"]
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] is not None
        assert kwargs["stderr"] is not subprocess.PIPE


# ---------------------------------------------------------------------------
# Send / receive
# ---------------------------------------------------------------------------


class TestSendReceive:
    def test_send_writes_jsonl_to_stdin(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        msg = _make_message(payload={"text": "hi pro"})

        sup.send("pro", msg)

        raw = factory.last.read_stdin_line(timeout=2.0)
        assert raw.endswith(b"\n"), "supervisor must always terminate lines with \\n"
        assert raw == serialize_message(msg).encode("utf-8")

    def test_send_uses_ipc_serializer(self, make_supervisor, factory, monkeypatch) -> None:
        calls: list[Message] = []
        real = supervisor_module.serialize_message

        def spy(msg: Message) -> str:
            calls.append(msg)
            return real(msg)

        monkeypatch.setattr(supervisor_module, "serialize_message", spy)
        sup = make_supervisor()
        sup.spawn("pro")
        msg = _make_message(payload={"text": "via serializer"})
        sup.send("pro", msg)

        assert calls == [msg]
        raw = factory.last.read_stdin_line(timeout=2.0)
        assert raw == real(msg).encode("utf-8")

    def test_send_to_unknown_role_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        with pytest.raises(UnknownRoleError):
            sup.send("judge", _make_message())

    def test_send_to_unspawned_role_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        with pytest.raises(ChildNotRunningError):
            sup.send("pro", _make_message())

    def test_receive_deserializes_jsonl_from_stdout(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        msg = _make_message(payload={"text": "from child"})

        factory.last.push_stdout_line(serialize_message(msg).encode("utf-8"))

        received = sup.receive("pro", timeout=2.0)
        assert received == msg

    def test_receive_uses_ipc_deserializer(self, make_supervisor, factory, monkeypatch) -> None:
        calls: list[str] = []
        real = supervisor_module.deserialize_message

        def spy(line: str) -> Message:
            calls.append(line)
            return real(line)

        monkeypatch.setattr(supervisor_module, "deserialize_message", spy)
        sup = make_supervisor()
        sup.spawn("pro")
        msg = _make_message()
        factory.last.push_stdout_line(serialize_message(msg).encode("utf-8"))

        sup.receive("pro", timeout=2.0)
        assert len(calls) == 1
        assert calls[0].rstrip("\n") == serialize_message(msg).rstrip("\n")

    def test_receive_timeout(self, make_supervisor) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        with pytest.raises(ChildReceiveTimeoutError) as exc:
            sup.receive("pro", timeout=0.05)
        assert exc.value.role == "pro"

    def test_receive_eof_raises(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        factory.last._set_exit(0)

        with pytest.raises(ChildStreamClosedError):
            sup.receive("pro", timeout=2.0)

    def test_receive_to_unknown_role_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        with pytest.raises(UnknownRoleError):
            sup.receive("judge", timeout=0.01)


# ---------------------------------------------------------------------------
# Terminate
# ---------------------------------------------------------------------------


class TestTerminate:
    def test_terminate_graceful_path(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        factory.last.terminate_actually_exits = True

        sup.terminate("pro")

        assert factory.last.terminate_called
        assert not factory.last.kill_called
        assert sup.child("pro") is None

    def test_terminate_force_kills_when_graceful_fails(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        factory.last.terminate_actually_exits = False

        sup.terminate("pro")

        assert factory.last.terminate_called
        assert factory.last.kill_called
        assert sup.child("pro") is None

    def test_terminate_unknown_role_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        with pytest.raises(UnknownRoleError):
            sup.terminate("judge")

    def test_terminate_not_running_raises(self, make_supervisor) -> None:
        sup = make_supervisor()
        with pytest.raises(ChildNotRunningError):
            sup.terminate("pro")

    def test_terminate_all_terminates_each_child(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        sup.spawn("con")
        assert sup.child("pro") is not None
        assert sup.child("con") is not None

        sup.terminate_all()

        assert sup.child("pro") is None
        assert sup.child("con") is None
        assert all(fp.terminate_called for fp in factory.instances)

    def test_terminate_all_is_safe_when_no_children(self, make_supervisor) -> None:
        sup = make_supervisor()
        sup.terminate_all()

    def test_context_manager_terminates_all_on_exit(self, make_supervisor, factory) -> None:
        with make_supervisor() as sup:
            sup.spawn("pro")
            sup.spawn("con")
        assert all(fp.terminate_called for fp in factory.instances)


# ---------------------------------------------------------------------------
# Respawn
# ---------------------------------------------------------------------------


class TestRespawn:
    def test_respawn_increments_restart_count(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        original = sup.spawn("pro")
        assert original.restart_count == 0

        new = sup.respawn("pro")
        assert new.restart_count == 1

        newer = sup.respawn("pro")
        assert newer.restart_count == 2

    def test_respawn_replaces_process_object(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        original = sup.spawn("pro")

        new = sup.respawn("pro")

        assert new is not original
        assert new.process is not original.process

    def test_respawn_replaces_pid(self, make_supervisor) -> None:
        sup = make_supervisor()
        original = sup.spawn("pro")
        old_pid = original.pid

        new = sup.respawn("pro")
        assert new.pid != old_pid
        assert new.pid is not None

    def test_respawn_terminates_old_process(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        first = factory.instances[-1]

        sup.respawn("pro")

        assert first.terminate_called or first.kill_called

    def test_respawn_without_prior_spawn_starts_at_one(self, make_supervisor) -> None:
        sup = make_supervisor()
        cp = sup.respawn("pro")
        assert cp.restart_count == 1


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class TestChildEnvironment:
    @pytest.mark.parametrize("role", ["pro", "con"])
    def test_search_api_key_never_leaks(self, make_supervisor, role: str) -> None:
        sup = make_supervisor()
        env = sup.build_child_env(role)
        assert "SEARCH_API_KEY" not in env
        for k, v in env.items():
            assert "MUST-NOT-LEAK" not in v, f"SEARCH_API_KEY value leaked into env[{k!r}]"

    @pytest.mark.parametrize("role", ["pro", "con"])
    def test_openai_api_key_is_passed(self, make_supervisor, role: str) -> None:
        sup = make_supervisor()
        env = sup.build_child_env(role)
        assert env.get("OPENAI_API_KEY") == "sk-test-llm-key"

    @pytest.mark.parametrize("role", ["pro", "con"])
    def test_debate_role_is_set(self, make_supervisor, role: str) -> None:
        sup = make_supervisor()
        env = sup.build_child_env(role)
        assert env["DEBATE_ROLE"] == role

    def test_arbitrary_env_var_is_not_passed(self, make_supervisor) -> None:
        sup = make_supervisor()
        env = sup.build_child_env("pro")
        assert "MY_ARBITRARY_VAR" not in env

    def test_essentials_passed_when_present(self, make_supervisor) -> None:
        sup = make_supervisor()
        env = sup.build_child_env("pro")
        for key in ("PATH", "PYTHONUNBUFFERED", "SYSTEMROOT", "WINDIR"):
            assert env.get(key), f"essential env var {key!r} should be present"

    def test_spawn_receives_filtered_env(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        passed_env = factory.calls[-1]["kwargs"]["env"]
        assert "SEARCH_API_KEY" not in passed_env
        assert "MY_ARBITRARY_VAR" not in passed_env
        assert passed_env["DEBATE_ROLE"] == "pro"

    def test_explicit_denylist_wins_over_allowlist(self, runs_dir, factory) -> None:
        env = {
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-test",
            "SEARCH_API_KEY": "should-not-leak",
        }
        sup = Supervisor(
            runs_dir=runs_dir,
            command_builder=_cmd_builder,
            env=env,
            env_allowlist=frozenset({"PATH", "OPENAI_API_KEY", "SEARCH_API_KEY"}),
            popen=factory,
        )
        built = sup.build_child_env("pro")
        assert "SEARCH_API_KEY" not in built
        assert built["OPENAI_API_KEY"] == "sk-test"


# ---------------------------------------------------------------------------
# IPC purity (Supervisor must not bypass IPC helpers)
# ---------------------------------------------------------------------------


class TestIPCBoundary:
    def test_supervisor_module_does_not_import_json(self) -> None:
        src = Path(supervisor_module.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            assert stripped != "import json", "supervisor must not import json"
            assert not stripped.startswith("from json "), "supervisor must not import from json"
            assert not stripped.startswith("import json "), (
                "supervisor must not import json with alias"
            )

    def test_supervisor_module_does_not_call_json(self) -> None:
        src = Path(supervisor_module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "json.dumps(",
            "json.loads(",
            "json.dump(",
            "json.load(",
            "JSONEncoder",
            "JSONDecoder",
        ):
            assert forbidden not in src, (
                f"supervisor must not bypass IPC helpers (found {forbidden!r})"
            )

    def test_supervisor_module_uses_ipc_helpers(self) -> None:
        src = Path(supervisor_module.__file__).read_text(encoding="utf-8")
        assert "serialize_message" in src
        assert "deserialize_message" in src
        assert "debate.orchestration.ipc" in src

    def test_supervisor_module_does_not_import_agent_modules(self) -> None:
        src = Path(supervisor_module.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            for bad in (
                "debate.agents.pro",
                "debate.agents.con",
                "debate.judge",
                "debate.watchdog",
            ):
                assert bad not in stripped, f"supervisor must not import {bad!r}: {stripped}"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_children_property_returns_snapshot(self, make_supervisor) -> None:
        sup = make_supervisor()
        sup.spawn("pro")
        snap = sup.children
        sup.spawn("con")
        assert set(snap.keys()) == {"pro"}
        assert set(sup.children.keys()) == {"pro", "con"}

    def test_child_returns_none_when_missing(self, make_supervisor) -> None:
        sup = make_supervisor()
        assert sup.child("pro") is None

    def test_child_process_is_alive_after_spawn(self, make_supervisor) -> None:
        sup = make_supervisor()
        cp = sup.spawn("pro")
        assert cp.is_alive() is True

    def test_child_process_not_alive_after_terminate(self, make_supervisor, factory) -> None:
        sup = make_supervisor()
        cp = sup.spawn("pro")
        sup.terminate("pro")
        assert cp.is_alive() is False

    def test_reader_thread_starts(self, make_supervisor) -> None:
        sup = make_supervisor()
        cp = sup.spawn("pro")
        time.sleep(0.05)
        assert cp.reader_thread is not None
        assert cp.reader_thread.is_alive()
