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

    raise SystemExit(ProAgent(llm_client=client, search_enabled=search_enabled).run())
