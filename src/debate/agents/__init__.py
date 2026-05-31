"""Debate agents package.

- :class:`BaseAgent` - shared child-process behavior (read/dispatch
  loop, heartbeat, shutdown, error handling).
- :class:`DebaterAgent` - shared Pro/Con debater logic (stance,
  prompt building, reply generation, search tool-call emission).
- :class:`ProAgent` / :class:`ConAgent` - minimal stance-only
  subclasses of DebaterAgent.

Filenames use ``*_agent.py`` so that Con never lands on a basename
equal to the reserved Windows DOS device name ``CON``.
"""

from debate.agents.base_agent import BaseAgent
from debate.agents.con_agent import ConAgent
from debate.agents.debater_agent import (
    DEFAULT_MAX_TOKENS,
    SEARCH_TOOL_NAME,
    DebaterAgent,
)
from debate.agents.pro_agent import ProAgent

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "SEARCH_TOOL_NAME",
    "BaseAgent",
    "ConAgent",
    "DebaterAgent",
    "ProAgent",
]
