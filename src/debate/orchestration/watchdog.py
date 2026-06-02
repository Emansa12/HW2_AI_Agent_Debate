"""Watchdog / health monitor for child agents (Stage 8)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from debate.orchestration.supervisor import Supervisor
from debate.orchestration.watchdog_check import _check_role
from debate.orchestration.watchdog_types import (
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_HEARTBEAT_TIMEOUT_SEC,
    DEFAULT_ROLES,
    MissReason,
    OnMissCallback,
)

logger = logging.getLogger(__name__)


class Watchdog:
    """Liveness monitor for Pro / Con children, sitting on the Judge side."""

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

    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop_event.is_set()

    @property
    def roles(self) -> tuple[str, ...]:
        return self._roles

    def start(self) -> None:
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
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def check_once(self) -> None:
        for role in self._roles:
            _check_role(self, role)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:  # noqa: BLE001
                logger.exception("watchdog check_once raised; continuing")
            if self._stop_event.wait(self._heartbeat_interval_sec):
                break

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
