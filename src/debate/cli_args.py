"""Argument parser for the debate CLI."""

from __future__ import annotations

import argparse

from debate import __version__
from debate.cli_config import DEFAULT_RUNS_ROOT


def build_parser() -> argparse.ArgumentParser:
    """Build the ``argparse`` parser used by :func:`debate.main.main`."""
    parser = argparse.ArgumentParser(
        prog="debate",
        description=(
            "Run a Pro/Con AI debate end-to-end. Default mode uses offline "
            "FakeLLMClient + FakeSearchClient and writes a JSONL transcript "
            "under runs/<timestamp>/run.jsonl."
        ),
    )
    parser.add_argument(
        "--motion",
        type=str,
        default=None,
        help="Debate motion. Defaults to the first entry in config/motions.json.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Number of argument rounds per side (1..100). Defaults to "
        "DebateConfig.rounds (config/debate.json: 10).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="fake",
        help="LLM model identifier. Used when `--real-llm` is set (passed "
        "through to RealLLMClient). 'fake' (default) selects FakeLLMClient.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed for the standard library RNG (logged for "
        "reproducibility; FakeLLMClient is already deterministic).",
    )
    parser.add_argument(
        "--fake",
        dest="fake",
        action="store_true",
        default=True,
        help="Use offline FakeLLMClient/FakeSearchClient (default).",
    )
    parser.add_argument(
        "--no-fake",
        dest="fake",
        action="store_false",
        help="Shorthand for `--real-llm --real-search`. Both real "
        "clients require API keys in the environment "
        "(LLM_API_KEY/OPENAI_API_KEY, SEARCH_API_KEY/TAVILY_API_KEY).",
    )
    parser.add_argument(
        "--real-search",
        dest="real_search",
        action="store_true",
        default=False,
        help="Stage 11: use the real Tavily-backed SearchClient for "
        "tool calls. Requires SEARCH_API_KEY (or TAVILY_API_KEY) in "
        "the environment. The Gatekeeper + ToolRouter still wrap "
        "every call.",
    )
    parser.add_argument(
        "--real-llm",
        dest="real_llm",
        action="store_true",
        default=False,
        help="Stage 11: use the real OpenAI-compatible LLMClient for "
        "the Judge's verdict generation AND for both Pro/Con "
        "subprocesses. Requires LLM_API_KEY (or OPENAI_API_KEY) in "
        "the environment.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to debate config JSON (defaults to config/debate.json).",
    )
    parser.add_argument(
        "--motions-file",
        type=str,
        default=None,
        help="Path to motions JSON (defaults to config/motions.json).",
    )
    parser.add_argument(
        "--runs-root",
        type=str,
        default=str(DEFAULT_RUNS_ROOT),
        help="Root directory for run transcripts (default: runs/).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional explicit run id (otherwise a UTC timestamp is used).",
    )
    parser.add_argument(
        "--replay",
        type=str,
        default=None,
        help="Path to a previously written run.jsonl. In replay mode "
        "no LLM / search / subprocess calls happen; the transcript is "
        "just pretty-printed.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress per-event progress output (the JSONL transcript is still written).",
    )
    parser.add_argument(
        "--print-transcript",
        action="store_true",
        default=False,
        help="After a live run, print a readable transcript summary from run.jsonl.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser
