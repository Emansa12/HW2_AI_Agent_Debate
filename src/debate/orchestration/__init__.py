"""Orchestration layer.

- Stage 2: JSONL IPC helpers.
- Stage 5: pure, deterministic debate state machine.
- Stage 6: Supervisor / child process manager.
- Stage 8: Watchdog / liveness monitor.
- Stage 9: Judge debate flow + verdict pipeline.

Stage 10 will add the end-to-end CLI driver and transcript polish.
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
from debate.orchestration.judge import (
    ALLOWED_TOOL_RESULT_ERRORS,
    DEFAULT_PER_TURN_TIMEOUT_SEC,
    DEFAULT_RECEIVE_MAX_ITERS,
    DEFAULT_VERDICT_MAX_TOKENS,
    MIN_VERDICT_REASONS,
    DebateHistory,
    InvalidReplyError,
    InvalidVerdictError,
    Judge,
    JudgeError,
    TurnRecord,
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
    "ALLOWED_TOOL_RESULT_ERRORS",
    "CON_STDERR_FILENAME",
    "DEFAULT_HEARTBEAT_INTERVAL_SEC",
    "DEFAULT_HEARTBEAT_TIMEOUT_SEC",
    "DEFAULT_PER_TURN_TIMEOUT_SEC",
    "DEFAULT_RECEIVE_MAX_ITERS",
    "DEFAULT_ROLES",
    "DEFAULT_TERMINATE_TIMEOUT_S",
    "DEFAULT_VERDICT_MAX_TOKENS",
    "MAX_MESSAGE_BYTES",
    "MIN_VERDICT_REASONS",
    "PRO_STDERR_FILENAME",
    "VALID_ROLES",
    "ChildAlreadyRunningError",
    "ChildNotRunningError",
    "ChildProcess",
    "ChildReceiveTimeoutError",
    "ChildStreamClosedError",
    "DebateHistory",
    "DebateStateMachine",
    "Event",
    "IPCError",
    "IllegalTransitionError",
    "InvalidReplyError",
    "InvalidVerdictError",
    "Judge",
    "JudgeError",
    "MalformedMessageError",
    "MissReason",
    "MultilineError",
    "OnMissCallback",
    "OversizeError",
    "SchemaVersionError",
    "State",
    "Supervisor",
    "SupervisorError",
    "TurnRecord",
    "UnknownRoleError",
    "Watchdog",
    "default_command_builder",
    "deserialize_message",
    "serialize_message",
]
