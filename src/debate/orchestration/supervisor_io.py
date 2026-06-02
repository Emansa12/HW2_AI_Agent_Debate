"""Child process I/O: reader thread, streams, and teardown helpers."""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from debate.orchestration.supervisor_errors import (
    ChildNotRunningError,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
)
from debate.sdk.schemas import Message


class _ReaderSentinel:
    """Marker placed on the reader queue when the child closes stdout."""

    __slots__ = ()


EOF = _ReaderSentinel()

PRO_STDERR_FILENAME: str = "pro_stderr.log"
CON_STDERR_FILENAME: str = "con_stderr.log"
STDERR_FILENAMES: dict[str, str] = {
    "pro": PRO_STDERR_FILENAME,
    "con": CON_STDERR_FILENAME,
}


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


def reader_loop(child: ChildProcess) -> None:
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
        child.read_queue.put(EOF)


def safe_poll(proc: subprocess.Popen) -> int | None:
    try:
        return proc.poll()
    except Exception:
        return -1


def safe_close(stream: IO[Any] | None) -> None:
    if stream is None:
        return
    try:
        if not getattr(stream, "closed", False):
            stream.close()
    except Exception:
        pass


def teardown_child(child: ChildProcess, *, terminate_timeout_s: float) -> None:
    """Graceful-then-forced termination and stream cleanup for one child."""
    proc = child.process

    if safe_poll(proc) is None:
        safe_close(child.stdin)
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=terminate_timeout_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=terminate_timeout_s)
        except Exception:
            pass

    safe_close(child.stdin)
    safe_close(child.stdout)
    safe_close(child.stderr_fh)

    if child.reader_thread is not None and child.reader_thread.is_alive():
        child.reader_thread.join(timeout=terminate_timeout_s)


def child_send(child: ChildProcess, role: str, message: Message) -> None:
    from debate.orchestration import supervisor as _supervisor

    if not child.is_alive():
        raise ChildNotRunningError(role)
    try:
        child.stdin.write(_supervisor.serialize_message(message).encode("utf-8"))
        child.stdin.flush()
    except (OSError, ValueError) as exc:
        raise ChildNotRunningError(role) from exc


def child_receive(child: ChildProcess, role: str, timeout: float | None) -> Message:
    try:
        item = child.read_queue.get() if timeout is None else child.read_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise ChildReceiveTimeoutError(role, timeout or 0.0) from exc
    if item is EOF:
        raise ChildStreamClosedError(role)
    from debate.orchestration import supervisor as _supervisor

    return _supervisor.deserialize_message(item)
