"""Tiny ping/pong child for the Stage 8 Watchdog chaos test.

Reads JSONL from stdin. For each ``ping`` line it emits a matching
``pong`` line to stdout. Anything else is ignored. Exits on EOF.

It deliberately avoids importing ``debate.*`` so the test doesn't
need PYTHONPATH plumbing for the subprocess (the supervisor passes
an allow-list env that doesn't include PYTHONPATH by default).

The wire format matches the schema defined in
:mod:`debate.sdk.schemas` for the Stage 8 test scope. We mirror only
the fields the schema requires plus ``in_reply_to`` in the payload.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    role = os.environ.get("DEBATE_ROLE", "pro")
    sys.stderr.write(f"heartbeat_child[{role}] starting\n")
    sys.stderr.flush()

    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    counter = 0
    while True:
        try:
            raw = inp.readline()
        except (OSError, ValueError):
            break
        if not raw:
            break
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            continue
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") != "ping":
            continue

        counter += 1
        pong = {
            "v": 1,
            "ts": time.time(),
            "turn_id": counter,
            "role": role,
            "type": "pong",
            "payload": {"in_reply_to": int(data.get("turn_id", 0))},
        }
        body = json.dumps(pong, separators=(",", ":"), ensure_ascii=False)
        try:
            out.write((body + "\n").encode("utf-8"))
            out.flush()
        except (OSError, ValueError):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
