"""CLI banner, summary, and replay output helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from debate import __version__
from debate.sdk.schemas import Verdict


def _log_cli_failed(
    logger: Any,
    out: Any,
    exc: BaseException,
    *,
    label: str | None = None,
) -> int:
    logger.log(
        role="cli",
        turn_id=0,
        event_type="cli_failed",
        error=type(exc).__name__,
        message=str(exc),
    )
    if label is None:
        out.write(f"error: {type(exc).__name__}: {exc}\n")
    else:
        out.write(f"error: {label}: {type(exc).__name__}: {exc}\n")
    return 1


def _print_banner(
    out: Any,
    *,
    motion: str,
    rounds: int,
    run_dir: Path,
    fake: bool,
    real_llm: bool,
    real_search: bool,
) -> None:
    if real_llm and real_search:
        mode = "real LLM + real search"
    elif real_llm:
        mode = "real LLM + fake search"
    elif real_search:
        mode = "fake LLM + real search"
    else:
        mode = "fake / offline" if fake else "real provider"
    out.write(
        "\n".join(
            [
                "======================================================================",
                f"  HW2 - AI Agent Debate (v{__version__})",
                f"  motion : {motion}",
                f"  rounds : {rounds} (per side)",
                f"  mode   : {mode}",
                f"  run dir: {run_dir}",
                "======================================================================",
                "",
            ]
        )
    )
    out.flush()


def _print_summary(out: Any, *, verdict: Verdict, run_file: Path) -> None:
    extra = verdict.model_extra or {}
    scores = extra.get("scores") or {}
    reasons = extra.get("reasons") or []
    out.write("\n--- Final verdict ---\n")
    out.write(f"  winner   : {verdict.winner}\n")
    out.write(f"  rationale: {verdict.rationale or '(none)'}\n")
    out.write(f"  scores   : pro={scores.get('pro', '?')} con={scores.get('con', '?')}\n")
    out.write(f"  reasons  : {len(reasons)}\n")
    for r in reasons:
        out.write(f"    - {r}\n")
    out.write(f"\nTranscript: {run_file}\n")
    out.flush()


def replay(path: Path, out: Any) -> int:
    """Pretty-print a previously written ``run.jsonl``.

    Replay is read-only: it never instantiates an LLM, a Supervisor,
    or anything that touches the network.

    Returns 0 if the transcript looks well-formed (each line parses,
    a final ``debate_done`` or ``verdict_recorded`` record exists),
    and 1 otherwise.
    """
    if not path.is_file():
        out.write(f"replay: file not found: {path}\n")
        return 1
    out.write(f"=== Replaying {path} ===\n")
    last_verdict: dict[str, Any] | None = None
    line_count = 0
    debate_done_seen = False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                line_count += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    out.write(f"  [WARN] line {line_count} not valid JSON: {exc.msg}\n")
                    continue
                if not isinstance(record, dict):
                    out.write(f"  [WARN] line {line_count} JSON root is not an object\n")
                    continue
                event = record.get("event_type", "?")
                role = record.get("role", "?")
                turn = record.get("turn_id", "?")
                ts = record.get("ts", "?")
                out.write(f"  [{ts}] role={role} turn={turn} event={event}\n")
                if event == "verdict_recorded":
                    last_verdict = record
                if event == "debate_done":
                    debate_done_seen = True
                    if last_verdict is None:
                        last_verdict = record
    except OSError as exc:
        out.write(f"replay: failed to read {path}: {exc}\n")
        return 1

    out.write(f"\n--- Replay summary ({line_count} record(s)) ---\n")
    if last_verdict is not None:
        out.write(f"  winner   : {last_verdict.get('winner', '?')}\n")
        out.write(f"  scores   : {last_verdict.get('scores', '?')}\n")
        out.write(f"  reasons  : {last_verdict.get('reasons_count', '?')}\n")
        out.write(f"  source   : {last_verdict.get('source', 'llm')}\n")
    else:
        out.write("  no verdict_recorded / debate_done event found in transcript\n")
    out.flush()
    if line_count == 0:
        return 1
    if not debate_done_seen and last_verdict is None:
        return 1
    return 0
