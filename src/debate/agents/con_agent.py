"""Con-side debater agent.

Minimal subclass of :class:`debate.agents.debater_agent.DebaterAgent`
that only sets the stance. All real behavior lives in DebaterAgent.

Filename is ``con_agent.py`` (not ``con.py``) because ``CON`` is a
reserved DOS device name on Windows. A bare ``con.py`` cannot be
opened by most tools (ruff, ripgrep, etc.) and would break imports
as ``debate.agents.con``.
"""

from __future__ import annotations

from debate.agents.debater_agent import DebaterAgent


class ConAgent(DebaterAgent):
    """Con-side debater. The *only* thing this class declares is its stance."""

    STANCE = "con"


__all__ = ["ConAgent"]


if __name__ == "__main__":
    from debate.sdk.llm_client import FakeLLMClient

    raise SystemExit(ConAgent(llm_client=FakeLLMClient()).run())
