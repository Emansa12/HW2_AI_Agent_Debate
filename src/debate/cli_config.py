"""Debate CLI configuration defaults and loading helpers."""

from __future__ import annotations

import json
from pathlib import Path

from debate.shared.config import DebateConfig, load_debate_config, load_motions

DEFAULT_MOTION: str = "AI-generated content should require mandatory labeling."
"""Used when neither ``--motion`` nor a non-empty ``config/motions.json``
is available. Mirrors the first entry in the shipped motions file."""

DEFAULT_RUNS_ROOT: Path = Path("runs")
"""Where ``RunLogger`` materializes the per-run subdirectory."""

DEFAULT_VERDICT_TEXT: str = (
    '{"winner":"pro","scores":{"pro":50,"con":40},'
    '"reasons":["Pro framed the motion clearly.",'
    '"Con conceded one structural point.",'
    '"Pro tied evidence to the motion."],'
    '"rationale":"Demo verdict from FakeLLMClient."}'
)
"""Canned verdict JSON used when running with the offline fake LLM.

The Stage 9 :class:`Judge` runs the LLM through the Gatekeeper to
produce the verdict, parses the response as JSON, and validates it.
:class:`debate.sdk.llm_client.FakeLLMClient` returns whatever string
it was constructed with, so feeding it pre-shaped JSON gives a
clean, deterministic, schema-valid demo verdict.
"""


def _load_or_default_debate_config(path_arg: str | None) -> DebateConfig:
    if path_arg is not None:
        return load_debate_config(path_arg)
    default_path = Path("config") / "debate.json"
    if default_path.exists():
        return load_debate_config(default_path)
    return DebateConfig(
        rounds=10,
        token_limit_per_turn=400,
        budget_total_tokens=50_000,
        heartbeat_seconds=5.0,
        max_message_bytes=64 * 1024,
        per_turn_timeout_seconds=30.0,
        total_timeout_seconds=300.0,
    )


def _resolve_motion(motion_arg: str | None, motions_path_arg: str | None) -> str:
    if motion_arg is not None and motion_arg.strip():
        return motion_arg
    candidate = Path(motions_path_arg) if motions_path_arg else Path("config") / "motions.json"
    if candidate.exists():
        try:
            motions = load_motions(candidate)
            if motions.motions:
                return motions.motions[0].topic
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return DEFAULT_MOTION
