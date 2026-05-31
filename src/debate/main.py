"""Entry point for the AI Agent Debate application.

Run with:

    uv run python -m debate.main

Stage 1: this file is only a placeholder. It prints a banner and exits
cleanly. Real orchestration (agents, IPC, Gatekeeper, Watchdog, Judge)
will be wired up in later stages.
"""

from __future__ import annotations

import sys

from debate import __version__

BANNER = r"""
======================================================================
  HW2 - AI Agent Debate
  Stage 1 skeleton  -  no debate logic implemented yet.
  Version: {version}
======================================================================
""".strip()


def main(argv: list[str] | None = None) -> int:
    """Placeholder entry point.

    Returns the process exit code. Always 0 in Stage 1.
    """
    _ = argv if argv is not None else sys.argv[1:]
    print(BANNER.format(version=__version__))
    print("Stage 1 OK: project skeleton is in place.")
    print("Next stages will add: Pro/Con agents, Gatekeeper, Watchdog, Judge, IPC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
