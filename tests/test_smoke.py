"""Smoke tests for the Stage 1 skeleton.

These tests do not exercise any debate logic - they only verify that
the package layout is importable and the placeholder entry point
returns 0.
"""

from __future__ import annotations

import debate
from debate.main import main


def test_package_has_version() -> None:
    assert isinstance(debate.__version__, str)
    assert debate.__version__ != ""


def test_main_returns_zero(capsys) -> None:
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "HW2 - AI Agent Debate" in captured.out
    assert "Stage 1 OK" in captured.out
