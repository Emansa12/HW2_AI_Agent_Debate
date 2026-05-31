"""Stage 10 housekeeping tests.

These tests pin the on-disk layout we decided to ship so future
changes don't regress it:

- ``runs/`` is tracked via ``runs/.gitkeep``;
- ``.gitignore`` excludes ``runs/*`` but keeps ``runs/.gitkeep``;
- the Stage 1 placeholder folders under ``src/debate/`` (which the
  Stage 4 audit flagged as dead code) have been removed; the real
  homes are still present;
- ``config/prompts/verdict.schema.json`` exists and is valid JSON;
- ``.env-example`` does not contain real-looking secrets.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# runs/ tracking
# ---------------------------------------------------------------------------


class TestRunsDir:
    def test_runs_gitkeep_exists(self) -> None:
        assert (REPO_ROOT / "runs" / ".gitkeep").is_file(), (
            "runs/.gitkeep must be tracked so a fresh clone has the directory"
        )

    def test_gitignore_excludes_run_artifacts_but_keeps_gitkeep(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "runs/*" in gitignore, ".gitignore must exclude runs/* contents"
        assert "!runs/.gitkeep" in gitignore, (
            ".gitignore must whitelist runs/.gitkeep so the directory stays tracked"
        )


# ---------------------------------------------------------------------------
# Placeholder cleanup (Stage 1 layout sketch)
# ---------------------------------------------------------------------------

_DEAD_PLACEHOLDER_DIRS: tuple[str, ...] = (
    "config",
    "gatekeeper",
    "ipc",
    "judge",
    "prompts",
    "utils",
    "watchdog",
)
"""Stage 1 layout sketch directories that were never the real home
of any code. Their real homes live under
``src/debate/{sdk,shared,orchestration,agents}/``."""

_REAL_DIRS: tuple[str, ...] = ("agents", "orchestration", "sdk", "shared")
"""Production code lives under exactly these subpackages of ``src/debate``."""


class TestPlaceholderCleanup:
    def test_dead_placeholder_dirs_are_gone(self) -> None:
        debate_pkg = REPO_ROOT / "src" / "debate"
        for name in _DEAD_PLACEHOLDER_DIRS:
            assert not (debate_pkg / name).exists(), (
                f"src/debate/{name}/ is a Stage 1 placeholder; it must be "
                "removed (real home lives elsewhere). See open finding #4."
            )

    def test_real_subpackages_still_present(self) -> None:
        debate_pkg = REPO_ROOT / "src" / "debate"
        for name in _REAL_DIRS:
            assert (debate_pkg / name).is_dir(), f"src/debate/{name}/ is required"


# ---------------------------------------------------------------------------
# verdict.schema.json
# ---------------------------------------------------------------------------


class TestVerdictSchemaPresence:
    def test_schema_file_exists_and_parses(self) -> None:
        path = REPO_ROOT / "config" / "prompts" / "verdict.schema.json"
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "winner" in data["properties"]


# ---------------------------------------------------------------------------
# Secrets hygiene
# ---------------------------------------------------------------------------


_OBVIOUS_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
)


class TestSecretsHygiene:
    def test_env_example_has_no_obvious_real_keys(self) -> None:
        path = REPO_ROOT / ".env-example"
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        for pattern in _OBVIOUS_SECRET_PATTERNS:
            assert pattern.search(text) is None, (
                f".env-example must not contain real-looking secrets matching {pattern.pattern}"
            )

    def test_env_file_is_gitignored(self) -> None:
        gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gi
