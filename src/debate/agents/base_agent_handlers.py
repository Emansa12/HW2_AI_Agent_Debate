"""IPC handler helpers for :class:`BaseAgent`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from debate.orchestration.ipc import IPCError
from debate.sdk.schemas import Message, MessageType

if TYPE_CHECKING:
    from debate.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)


def _on_ipc_error(agent: BaseAgent, exc: IPCError, raw: bytes) -> None:
    """Called when a single line cannot be parsed. Loop continues."""
    logger.warning(
        "ipc error on %s: %s (%d bytes)",
        agent.role.value,
        exc,
        len(raw),
    )


def _on_handler_error(agent: BaseAgent, exc: Exception, msg: Message) -> None:
    """Called when handle()/heartbeat raises. Loop continues."""
    logger.exception(
        "handler error on %s for type=%s: %s",
        agent.role.value,
        msg.type.value,
        exc,
    )


def _on_unknown_type(agent: BaseAgent, msg: Message) -> None:
    """Called for message types BaseAgent does not route. Loop continues."""
    logger.debug(
        "unhandled message type on %s: %s",
        agent.role.value,
        msg.type.value,
    )


def _handle_ping(agent: BaseAgent, ping: Message) -> None:
    pong = agent.make_message(
        MessageType.PONG,
        {"in_reply_to": ping.turn_id},
    )
    agent.send(pong)


def _handle_shutdown(agent: BaseAgent, _shutdown: Message) -> None:
    agent._running = False
