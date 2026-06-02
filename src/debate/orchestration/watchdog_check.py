"""Per-role heartbeat checks for :class:`Watchdog`."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from debate.orchestration.ipc import IPCError
from debate.orchestration.supervisor import (
    ChildNotRunningError,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    SupervisorError,
)
from debate.orchestration.watchdog_types import MissReason
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role

if TYPE_CHECKING:
    from debate.orchestration.watchdog import Watchdog

logger = logging.getLogger(__name__)


def _check_role(watchdog: Watchdog, role: str) -> None:
    child = watchdog._supervisor.child(role)
    if child is None:
        _record_miss(watchdog, role, MissReason.NO_CHILD, ping_turn_id=None)
        return
    if not child.is_alive():
        _record_miss(watchdog, role, MissReason.CHILD_NOT_ALIVE, ping_turn_id=None)
        return

    ping = _build_ping(watchdog)
    try:
        watchdog._supervisor.send(role, ping)
    except ChildNotRunningError:
        _record_miss(watchdog, role, MissReason.SEND_FAILED, ping_turn_id=ping.turn_id)
        return
    except SupervisorError:
        _record_miss(watchdog, role, MissReason.SUPERVISOR_ERROR, ping_turn_id=ping.turn_id)
        return
    except Exception:  # noqa: BLE001
        _record_miss(watchdog, role, MissReason.UNEXPECTED_ERROR, ping_turn_id=ping.turn_id)
        return

    _log_event(
        watchdog,
        event_type="watchdog_ping",
        turn_id=ping.turn_id,
        target_role=role,
    )

    try:
        reply = watchdog._supervisor.receive(role, timeout=watchdog._heartbeat_timeout_sec)
    except ChildReceiveTimeoutError:
        _record_miss(watchdog, role, MissReason.TIMEOUT, ping_turn_id=ping.turn_id)
        return
    except ChildStreamClosedError:
        _record_miss(watchdog, role, MissReason.STREAM_CLOSED, ping_turn_id=ping.turn_id)
        return
    except IPCError:
        _record_miss(watchdog, role, MissReason.IPC_ERROR, ping_turn_id=ping.turn_id)
        return
    except SupervisorError:
        _record_miss(watchdog, role, MissReason.SUPERVISOR_ERROR, ping_turn_id=ping.turn_id)
        return
    except Exception:  # noqa: BLE001
        _record_miss(watchdog, role, MissReason.UNEXPECTED_ERROR, ping_turn_id=ping.turn_id)
        return

    if reply.type is not MessageType.PONG:
        _record_miss(
            watchdog,
            role,
            MissReason.NOT_A_PONG,
            ping_turn_id=ping.turn_id,
            actual_type=reply.type.value,
        )
        return

    _log_event(
        watchdog,
        event_type="watchdog_pong",
        turn_id=ping.turn_id,
        target_role=role,
        pong_turn_id=reply.turn_id,
    )


def _build_ping(watchdog: Watchdog) -> Message:
    watchdog._ping_counter += 1
    turn_id = watchdog._ping_counter
    ts = float(watchdog._clock())
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
    watchdog: Watchdog,
    role: str,
    reason: str,
    *,
    ping_turn_id: int | None,
    **extra: Any,
) -> None:
    _log_event(
        watchdog,
        event_type="watchdog_miss",
        turn_id=ping_turn_id if ping_turn_id is not None else 0,
        target_role=role,
        reason=reason,
        **extra,
    )
    try:
        watchdog._on_miss(role)
    except Exception:  # noqa: BLE001
        logger.exception("watchdog on_miss callback raised for role=%s", role)


def _log_event(watchdog: Watchdog, *, event_type: str, turn_id: int, **fields: Any) -> None:
    if watchdog._run_logger is None:
        return
    with contextlib.suppress(Exception):
        watchdog._run_logger.log(
            role="watchdog",
            turn_id=turn_id,
            event_type=event_type,
            **fields,
        )
