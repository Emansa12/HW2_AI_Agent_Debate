"""Pro-side debater agent.

Minimal subclass of :class:`debate.agents.debater_agent.DebaterAgent`
that only sets the stance. All real behavior lives in DebaterAgent.

Filename is ``pro_agent.py`` (not ``pro.py``) for parity with
``con_agent.py``, which avoids the Windows reserved DOS device name
``CON``.
"""

from __future__ import annotations

from debate.agents.debater_agent import DebaterAgent


class ProAgent(DebaterAgent):
    """Pro-side debater. The *only* thing this class declares is its stance."""

    STANCE = "pro"


__all__ = ["ProAgent"]


if __name__ == "__main__":
    from debate.sdk.llm_client import FakeLLMClient

    raise SystemExit(ProAgent(llm_client=FakeLLMClient()).run())
