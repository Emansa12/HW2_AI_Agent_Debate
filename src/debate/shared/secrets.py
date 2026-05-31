"""Secret loading.

Secrets (LLM API keys, etc.) are read **only from environment
variables**. The `.env-example` file at the repo root documents the
recognized variable names and only contains placeholders. The real
`.env` is gitignored.

`maybe_load_dotenv` is a small convenience that populates
`os.environ` from a `.env` file *if* one exists - useful for local
development. Production deployments should set environment variables
directly and never rely on a `.env` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Secrets:
    """Container for known secret values, all optional."""

    openai_api_key: str | None
    anthropic_api_key: str | None
    google_api_key: str | None

    @property
    def has_any_llm_key(self) -> bool:
        return any(
            (
                self.openai_api_key,
                self.anthropic_api_key,
                self.google_api_key,
            )
        )


def _env_or_none(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def load_secrets() -> Secrets:
    """Read recognized secrets from `os.environ`.

    Empty strings are normalized to `None`.

    This function does **not** read any file. If you want `.env`
    support, call `maybe_load_dotenv` first.
    """
    return Secrets(
        openai_api_key=_env_or_none("OPENAI_API_KEY"),
        anthropic_api_key=_env_or_none("ANTHROPIC_API_KEY"),
        google_api_key=_env_or_none("GOOGLE_API_KEY"),
    )


def maybe_load_dotenv(env_path: str | Path = ".env") -> bool:
    """Populate `os.environ` from a `.env` file if present.

    Returns True if a file was found and loaded, False otherwise.
    Never raises if the file is absent.

    By default this respects pre-existing environment variables
    (it will not override them).
    """
    p = Path(env_path)
    if not p.exists():
        return False
    load_dotenv(p)
    return True
