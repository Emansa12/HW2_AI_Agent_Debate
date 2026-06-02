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
    # Stage 11: child swaps in :class:`RealLLMClient` only when the
    # parent CLI explicitly opted in via ``DEBATE_REAL_LLM=1`` (set
    # by ``--real-llm``). Default stays the offline FakeLLMClient
    # so the rest of the suite never needs an API key.
    import os

    if os.environ.get("DEBATE_REAL_LLM") == "1":
        from debate.sdk.real_llm_client import RealLLMClient

        client = RealLLMClient.from_env()
    else:
        from debate.sdk.llm_client import FakeLLMClient

        client = FakeLLMClient()

    search_enabled = os.environ.get("DEBATE_REAL_SEARCH") == "1"

    raise SystemExit(ConAgent(llm_client=client, search_enabled=search_enabled).run())
