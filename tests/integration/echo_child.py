"""Tiny line-echo child used by the Stage 6 Supervisor smoke test.

Reads bytes from stdin one line at a time and writes the same bytes
back to stdout. Exits cleanly on EOF.

The script is intentionally minimal: it does not parse JSON, so the
supervisor's IPC helpers are exercised on a clean round trip with no
re-serialization in the child.

It also writes one diagnostic line to stderr on startup so the smoke
test can verify that the per-role stderr capture file actually
receives output.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    role = os.environ.get("DEBATE_ROLE", "?")
    sys.stderr.write(f"echo_child[{role}] starting\n")
    sys.stderr.flush()

    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    while True:
        try:
            raw = inp.readline()
        except (OSError, ValueError):
            break
        if not raw:
            break
        try:
            out.write(raw)
            out.flush()
        except (OSError, ValueError):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
