"""Factory helpers for Gatekeeper, Router, LLM, and runs directory."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from debate.cli_config import DEFAULT_VERDICT_TEXT
from debate.sdk.llm_client import FakeLLMClient, LLMClient
from debate.sdk.search_client import FakeSearchClient, SearchClient
from debate.shared.config import DebateConfig
from debate.shared.gatekeeper import Gatekeeper, GatekeeperPolicy
from debate.shared.logger import RunLogger
from debate.shared.router import ToolRouter


def _build_gatekeeper(cfg: DebateConfig) -> Gatekeeper:
    policy = GatekeeperPolicy(
        max_tokens_per_turn=cfg.token_limit_per_turn,
        max_tokens_per_debate=max(cfg.budget_total_tokens, cfg.token_limit_per_turn),
        max_usd_per_debate=10.0,
        max_requests_per_minute=600,
    )
    return Gatekeeper(policy)


def _build_router(gk: Gatekeeper, *, real_search: bool) -> ToolRouter:
    """Build the Stage 4 :class:`ToolRouter`.

    ``real_search=True`` swaps in the Stage 11
    :class:`RealSearchClient` (Tavily), which raises
    :class:`MissingSearchAPIKeyError` if neither
    ``SEARCH_API_KEY`` nor ``TAVILY_API_KEY`` is set. Either way
    the Gatekeeper + LRU cache still wrap every call.
    """
    client: SearchClient
    if real_search:
        from debate.sdk.real_search_client import RealSearchClient

        client = RealSearchClient.from_env()
    else:
        client = FakeSearchClient(results_per_query=2)
    return ToolRouter(gatekeeper=gk, search_client=client)


def _build_judge_llm(*, model: str, real_llm: bool) -> LLMClient:
    """Build the Judge-side :class:`LLMClient`.

    ``real_llm=True`` swaps in the Stage 11
    :class:`RealLLMClient`, which raises
    :class:`MissingLLMAPIKeyError` if neither ``LLM_API_KEY`` nor
    ``OPENAI_API_KEY`` is set. Otherwise the offline
    :class:`FakeLLMClient` is used (pre-loaded with the canned
    Stage 10 verdict JSON for a clean demo).
    """
    if real_llm:
        from debate.sdk.real_llm_client import RealLLMClient

        kwargs: dict[str, Any] = {}
        if model and model != "fake":
            kwargs["model"] = model
        return RealLLMClient.from_env(**kwargs)

    # ``model`` is recorded in the transcript so a real-mode swap
    # can reuse the same CLI flag.
    del model
    return FakeLLMClient(response_text=DEFAULT_VERDICT_TEXT)


def _build_runs_dir(runs_root: Path, run_id: str | None) -> tuple[Path, RunLogger]:
    runs_root.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(runs_root=runs_root, run_id=run_id)
    return logger.run_dir, logger


def _resolve_modes(args: argparse.Namespace) -> tuple[bool, bool]:
    """Return ``(use_real_llm, use_real_search)`` after collapsing
    ``--fake`` / ``--no-fake`` / ``--real-llm`` / ``--real-search``.

    Rules:

    - Default: both real flags are off (full fake mode).
    - ``--no-fake`` is shorthand for both real flags on, but a
      caller-specified ``--real-llm`` / ``--real-search`` wins if
      present.
    - ``--real-llm`` and ``--real-search`` can be combined with
      ``--fake`` (the default) so e.g. fake LLM + real search is a
      legitimate hybrid demo mode.
    """
    real_llm = args.real_llm or (not args.fake)
    real_search = args.real_search or (not args.fake)
    return real_llm, real_search


def _safe_ledger_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a ledger snapshot whose keys won't trip the
    :mod:`debate.shared.redaction` substring filter.

    The Stage 3 redactor scrubs any key whose lower-cased name
    *contains* ``"token"`` (so e.g. an ``OPENAI_API_TOKEN`` field
    becomes ``<redacted>``). The :class:`Ledger.snapshot` keys
    (``tokens_in`` / ``tokens_out`` / ``total_tokens``) are
    counters, not secrets, but they trip the same filter. We
    rename them to ``llm_*_count`` for the on-disk record - the
    counts themselves stay verbatim.
    """
    return {
        "requests": snapshot.get("requests"),
        "llm_input_count": snapshot.get("tokens_in"),
        "llm_output_count": snapshot.get("tokens_out"),
        "llm_total_count": snapshot.get("total_tokens"),
        "usd_spent": snapshot.get("usd_spent"),
    }
