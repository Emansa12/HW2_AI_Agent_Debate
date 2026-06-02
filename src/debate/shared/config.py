"""Typed config loaders for `config/debate.json` and `config/motions.json`.

Both files are validated through Pydantic so every field has a
well-defined type and reasonable bounds. The loaders raise on:

- missing file        -> `FileNotFoundError`
- malformed JSON      -> `json.JSONDecodeError`
- failing validation  -> `pydantic.ValidationError`

Defaults are documented in `config/debate.json` shipped with the
repo (10 rounds per side, 64 KiB max message size, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DebateConfig(BaseModel):
    """Tunables for a single debate run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds: Annotated[int, Field(ge=1, le=100)]
    """Turns per side (Pro and Con each get `rounds` turns)."""

    token_limit_per_turn: Annotated[int, Field(ge=1, le=8192)]
    """Hard cap on output tokens for any single agent reply."""

    budget_total_tokens: Annotated[int, Field(ge=1)]
    """Soft cap on total LLM tokens used by the whole debate."""

    heartbeat_seconds: Annotated[float, Field(gt=0.0, le=600.0)]
    """Watchdog `ping` cadence."""

    max_message_bytes: Annotated[int, Field(ge=1024, le=10 * 1024 * 1024)]
    """Hard cap per JSONL line. Should match
    `debate.orchestration.ipc.MAX_MESSAGE_BYTES`."""

    per_turn_timeout_seconds: Annotated[float, Field(gt=0.0)]
    """Watchdog timeout for a single agent turn."""

    total_timeout_seconds: Annotated[float, Field(gt=0.0)]
    """Watchdog wall-clock budget for the whole debate."""

    max_logged_text_chars: Annotated[int, Field(ge=256, le=1_000_000)] = 65_536
    """Maximum characters per string field written into ``run.jsonl``.

    Large enough for graders to read full debate turns; truncates
    with a ``…[truncated]`` suffix when exceeded."""


class Motion(BaseModel):
    """A single debate motion (topic)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(min_length=1, max_length=64)]
    topic: Annotated[str, Field(min_length=1, max_length=512)]


class Motions(BaseModel):
    """Wrapper around the list of available motions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    motions: Annotated[list[Motion], Field(min_length=1)]


def default_debate_config_path() -> Path:
    return Path("config") / "debate.json"


def default_motions_path() -> Path:
    return Path("config") / "motions.json"


def _read_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("expected a JSON object at the root", text, 0)
    return data


def load_debate_config(path: str | Path | None = None) -> DebateConfig:
    """Read and validate `config/debate.json` (or a custom path)."""
    p = Path(path) if path is not None else default_debate_config_path()
    data = _read_json(p)
    return DebateConfig.model_validate(data)


def load_motions(path: str | Path | None = None) -> Motions:
    """Read and validate `config/motions.json` (or a custom path)."""
    p = Path(path) if path is not None else default_motions_path()
    data = _read_json(p)
    return Motions.model_validate(data)
