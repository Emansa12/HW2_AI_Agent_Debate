"""Helpers for grading-friendly transcript fields in ``run.jsonl``.

Values passed to :class:`debate.shared.logger.RunLogger` are already
scrubbed by :func:`debate.shared.redaction.redact` at write time.
The Judge additionally runs every log field through
:func:`prepare_transcript_field` first so very long strings are
truncated before they hit disk.

:func:`print_readable_transcript` reads a finished ``run.jsonl`` and
prints a human-readable terminal summary (with redaction applied
again before display).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from debate.shared.redaction import redact

DEFAULT_MAX_LOGGED_TEXT_CHARS: int = 65_536
"""Default cap per string field written into the transcript.

Large enough for a full HW2 debate transcript while preventing
accidental multi-megabyte log lines."""

DEFAULT_MAX_PRINTED_TEXT_CHARS: int = 3000
"""Default cap per answer / long text field in terminal summaries."""

_TRUNCATION_SUFFIX: str = "…[truncated]"


def format_transcript_dict(payload: dict[str, Any]) -> str:
    """Stable, human-readable JSON text for transcript log fields only.

    Not used for IPC wire serialization — only for ``run.jsonl``
    grading readability.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def prepare_transcript_field(value: Any, *, max_chars: int) -> Any:
    """Return a copy of ``value`` safe for transcript logging.

    - ``str`` values are truncated to ``max_chars`` (with a suffix
      when clipped).
    - ``dict`` / ``list`` / ``tuple`` are walked recursively.
    - Scalars are returned unchanged.

    Callers should still pass the result through
    :func:`debate.shared.redaction.redact` before writing.
    """
    if max_chars < 1:
        max_chars = 1
    return _prepare(value, max_chars=max_chars)


def _prepare(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_str(value, max_chars)
    if isinstance(value, dict):
        return {k: _prepare(v, max_chars=max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_prepare(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(_prepare(item, max_chars=max_chars) for item in value)
    return value


def _truncate_str(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = _TRUNCATION_SUFFIX
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


def _load_transcript_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def _safe_print_text(value: Any, *, max_chars: int) -> str:
    """Redact sensitive keys, then truncate for terminal display."""
    cleaned = redact(value)
    if cleaned is None:
        return ""
    text = cleaned if isinstance(cleaned, str) else str(cleaned)
    return _truncate_str(text, max_chars)


def _extract_search_hits(payload: dict[str, Any], *, limit: int = 3) -> list[tuple[str, str]]:
    """Return up to ``limit`` ``(title, url)`` pairs from a tool result."""
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    hits: list[tuple[str, str]] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("url", "")
        hits.append(
            (
                _safe_print_text(title, max_chars=500),
                _safe_print_text(url, max_chars=500),
            )
        )
    return hits


def print_readable_transcript(
    path: Path,
    *,
    out: TextIO | None = None,
    max_chars: int = DEFAULT_MAX_PRINTED_TEXT_CHARS,
) -> None:
    """Print a readable summary of ``run.jsonl`` to ``out`` (stdout default).

    Applies :func:`redact` to every field before printing. Long reply
    bodies are capped at ``max_chars`` characters each.
    """
    if out is None:
        out = sys.stdout
    if not path.is_file():
        out.write(f"transcript: file not found: {path}\n")
        out.flush()
        return

    records = _load_transcript_records(path)
    motion = "?"
    for rec in records:
        if rec.get("event_type") == "cli_invoked":
            m = rec.get("motion")
            if isinstance(m, str) and m.strip():
                motion = _safe_print_text(m, max_chars=max_chars)
            break

    out.write("\n=== Debate transcript summary ===\n\n")
    out.write(f"Motion: {motion}\n\n")

    for rec in records:
        event = rec.get("event_type")
        role = rec.get("target_role", rec.get("role", "?"))

        if event == "tool_call_received":
            payload = rec.get("tool_call_payload")
            if not isinstance(payload, dict):
                payload = {}
            safe_payload = redact(payload)
            query = safe_payload.get("query", "")
            out.write(f"SEARCH CALL FROM {role}\n")
            out.write(f"query: {_safe_print_text(query, max_chars=max_chars)}\n\n")

        elif event == "tool_result_sent":
            payload = rec.get("tool_result_payload")
            if not isinstance(payload, dict):
                payload = {}
            hits = _extract_search_hits(redact(payload), limit=3)
            if hits:
                out.write(f"Search results ({role}):\n")
                for i, (title, url) in enumerate(hits, start=1):
                    out.write(f"  {i}. {title} ({url})\n")
                out.write("\n")

        elif event == "reply_received":
            phase = rec.get("phase", "?")
            rnd = rec.get("round", "?")
            content = rec.get("content", "")
            out.write(f"ANSWER FROM {role}\n")
            out.write(f"phase: {phase}\n")
            out.write(f"round: {rnd}\n")
            out.write(f"content: {_safe_print_text(content, max_chars=max_chars)}\n\n")

        elif event == "verdict_recorded":
            out.write("--- Judge verdict ---\n")
            out.write(f"winner: {_safe_print_text(rec.get('winner', '?'), max_chars=100)}\n")
            scores = rec.get("scores")
            if isinstance(scores, dict):
                safe_scores = redact(scores)
                pro = safe_scores.get("pro", "?")
                con = safe_scores.get("con", "?")
                out.write(f"scores: pro={pro} con={con}\n")
            else:
                out.write(f"scores: {_safe_print_text(scores, max_chars=max_chars)}\n")
            reasons = rec.get("reasons")
            if isinstance(reasons, list) and reasons:
                out.write("reasons:\n")
                for reason in reasons:
                    out.write(f"  - {_safe_print_text(reason, max_chars=max_chars)}\n")
            rationale = rec.get("rationale")
            if rationale:
                out.write(f"rationale: {_safe_print_text(rationale, max_chars=max_chars)}\n")
            out.write("\n")

        elif event == "cli_finished":
            ledger = rec.get("ledger")
            if isinstance(ledger, dict):
                safe_ledger = redact(ledger)
                out.write("--- Gatekeeper ledger ---\n")
                for key in (
                    "requests",
                    "llm_input_count",
                    "llm_output_count",
                    "llm_total_count",
                    "usd_spent",
                ):
                    if key in safe_ledger:
                        out.write(f"{key}: {safe_ledger[key]}\n")
                out.write("\n")

    out.flush()
