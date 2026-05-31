"""Orchestration layer.

- Stage 2: JSONL IPC helpers.
- Stage 5: pure, deterministic debate state machine.
- Stage 6: Supervisor / child process manager.
- Stage 8: Watchdog / liveness monitor.

Future stages will wire in the full debate loop and the Judge's
debate flow.
"""

from debate.orchestration.ipc import (
    MAX_MESSAGE_BYTES,
    IPCError,
    MalformedMessageError,
    MultilineError,
    OversizeError,
    SchemaVersionError,
    deserialize_message,
    serialize_message,
)
from debate.orchestration.state_machine import (
    DebateStateMachine,
    Event,
    IllegalTransitionError,
    State,
)
from debate.orchestration.supervisor import (
    CON_STDERR_FILENAME,
    DEFAULT_TERMINATE_TIMEOUT_S,
    PRO_STDERR_FILENAME,
    VALID_ROLES,
    ChildAlreadyRunningError,
    ChildNotRunningError,
    ChildProcess,
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    Supervisor,
    SupervisorError,
    UnknownRoleError,
    default_command_builder,
)
from debate.orchestration.watchdog import (
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    DEFAULT_HEARTBEAT_TIMEOUT_SEC,
    DEFAULT_ROLES,
    MissReason,
    OnMissCallback,
    Watchdog,
)

__all__ = [
    "CON_STDERR_FILENAME",
    "DEFAULT_HEARTBEAT_INTERVAL_SEC",
    "DEFAULT_HEARTBEAT_TIMEOUT_SEC",
    "DEFAULT_ROLES",
    "DEFAULT_TERMINATE_TIMEOUT_S",
    "MAX_MESSAGE_BYTES",
    "PRO_STDERR_FILENAME",
    "VALID_ROLES",
    "ChildAlreadyRunningError",
    "ChildNotRunningError",
    "ChildProcess",
    "ChildReceiveTimeoutError",
    "ChildStreamClosedError",
    "DebateStateMachine",
    "Event",
    "IPCError",
    "IllegalTransitionError",
    "MalformedMessageError",
    "MissReason",
    "MultilineError",
    "OnMissCallback",
    "OversizeError",
    "SchemaVersionError",
    "State",
    "Supervisor",
    "SupervisorError",
    "UnknownRoleError",
    "Watchdog",
    "default_command_builder",
    "deserialize_message",
    "serialize_message",
]
