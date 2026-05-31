"""Per-run JSONL transcript writer.

Layout for each run:

    runs/<run_id>/
        run.jsonl         -- structured event log (one JSON object per line)
        pro_stderr.log    -- captured stderr from the Pro child process
        con_stderr.log    -- captured stderr from the Con child process

The stderr files use the `pro_stderr` / `con_stderr` naming (not
`pro.stderr` / `con.stderr`) so that `con` is never the basename of
any file - on Windows, `CON` is a reserved DOS device name and
cannot be used as the start of a basename even with extensions.

`run_id` defaults to a UTC timestamp like `20260101T123045_123456Z`.
Tests can pass an explicit `run_id` and `runs_root` for predictability.

Every line in `run.jsonl` is a single valid JSON object with at
least these fields:

    ts          - epoch seconds (float)   - when the event was logged
    role        - judge | pro | con | system | watchdog | gatekeeper | tools
    turn_id     - monotonic turn counter (int, >= 0)
    event_type  - short string describing the record

Sensitive fields (matching `debate.shared.redaction`) are scrubbed
before the line is written to disk.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from debate.shared.redaction import redact


class RunLogger:
    """Writes the per-run JSONL transcript and exposes stderr paths."""

    DEFAULT_RUNS_ROOT: Path = Path("runs")
    RUN_FILENAME: str = "run.jsonl"
    PRO_STDERR_FILENAME: str = "pro_stderr.log"
    CON_STDERR_FILENAME: str = "con_stderr.log"

    def __init__(
        self,
        runs_root: str | Path | None = None,
        *,
        run_id: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        root = Path(runs_root) if runs_root is not None else self.DEFAULT_RUNS_ROOT
        self.run_id: str = run_id if run_id is not None else self._make_run_id()
        self.run_dir: Path = root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)

        self.run_file: Path = self.run_dir / self.RUN_FILENAME
        self.pro_stderr_path: Path = self.run_dir / self.PRO_STDERR_FILENAME
        self.con_stderr_path: Path = self.run_dir / self.CON_STDERR_FILENAME

        self.pro_stderr_path.touch()
        self.con_stderr_path.touch()

    @staticmethod
    def _make_run_id() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")

    def log(
        self,
        *,
        role: str,
        turn_id: int,
        event_type: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append one structured record to `run.jsonl`.

        Returns the (already-redacted) record that was written so
        callers can inspect what hit disk.
        """
        record: dict[str, Any] = {
            "ts": self._clock(),
            "role": role,
            "turn_id": turn_id,
            "event_type": event_type,
        }
        record.update(fields)
        record = redact(record)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self.run_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record
