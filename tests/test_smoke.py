"""Smoke tests for the package's public surface.

Verifies the package is importable, exposes a version, and the CLI
``--version`` / ``--help`` paths work without spawning subprocesses.
The full-debate end-to-end run is exercised in
``tests/integration/test_e2e_debate.py``.
"""

from __future__ import annotations

import io

import pytest

import debate
from debate.main import build_parser, main


def test_package_has_version() -> None:
    assert isinstance(debate.__version__, str)
    assert debate.__version__ != ""


def test_cli_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args(["--version"])
    assert ei.value.code == 0


def test_cli_help_does_not_crash() -> None:
    with pytest.raises(SystemExit) as ei:
        build_parser().parse_args(["--help"])
    assert ei.value.code == 0


def test_cli_replay_missing_file_returns_one(tmp_path) -> None:
    """A fast end-to-end smoke check that ``main`` dispatches to
    replay, which never spawns a subprocess."""
    out = io.StringIO()
    rc = main(["--replay", str(tmp_path / "does_not_exist.jsonl")], out=out)
    assert rc == 1
    assert "not found" in out.getvalue()
