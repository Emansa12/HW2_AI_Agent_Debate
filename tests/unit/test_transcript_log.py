"""Tests for grading-friendly transcript field preparation."""

from __future__ import annotations

import io
import json
from pathlib import Path

from debate.shared.redaction import REDACTION_PLACEHOLDER, redact
from debate.shared.transcript_log import (
    DEFAULT_MAX_PRINTED_TEXT_CHARS,
    prepare_transcript_field,
    print_readable_transcript,
)


class TestPrepareTranscriptField:
    def test_truncates_long_strings(self) -> None:
        text = "x" * 100
        out = prepare_transcript_field(text, max_chars=20)
        assert len(out) <= 20
        assert out.endswith("[truncated]")

    def test_recurses_through_dicts_and_lists(self) -> None:
        payload = {"items": [{"snippet": "a" * 50}]}
        out = prepare_transcript_field(payload, max_chars=10)
        assert len(out["items"][0]["snippet"]) <= 10

    def test_redact_scrubs_sensitive_keys_in_nested_payload(self) -> None:
        raw = prepare_transcript_field(
            {"tool_result_payload": {"api_key": "sk-should-not-appear", "results": []}},
            max_chars=1000,
        )
        safe = redact(raw)
        assert safe["tool_result_payload"]["api_key"] == REDACTION_PLACEHOLDER
        assert "sk-should-not-appear" not in str(safe)


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


_SAMPLE_RECORDS = [
    {
        "ts": 1.0,
        "role": "cli",
        "turn_id": 0,
        "event_type": "cli_invoked",
        "motion": "Should schools ban smartphones?",
    },
    {
        "ts": 2.0,
        "role": "judge",
        "turn_id": 1,
        "event_type": "tool_call_received",
        "target_role": "pro",
        "tool_call_payload": {"tool": "search", "query": "smartphone ban schools evidence"},
    },
    {
        "ts": 3.0,
        "role": "judge",
        "turn_id": 1,
        "event_type": "tool_result_sent",
        "target_role": "pro",
        "tool_result_payload": {
            "tool": "search",
            "results": [
                {
                    "title": "Study on phones in class",
                    "url": "https://example.com/study",
                    "snippet": "ignored in summary",
                },
                {
                    "title": "Policy brief",
                    "url": "https://example.com/policy",
                    "snippet": "also ignored",
                },
            ],
        },
    },
    {
        "ts": 4.0,
        "role": "judge",
        "turn_id": 1,
        "event_type": "reply_received",
        "target_role": "pro",
        "phase": "opening",
        "round": 0,
        "content": "Phones distract students and harm learning outcomes.",
    },
    {
        "ts": 5.0,
        "role": "judge",
        "turn_id": 2,
        "event_type": "reply_received",
        "target_role": "con",
        "phase": "opening",
        "round": 0,
        "content": "Smartphones enable access to educational resources.",
    },
    {
        "ts": 6.0,
        "role": "judge",
        "turn_id": 10,
        "event_type": "verdict_recorded",
        "winner": "pro",
        "scores": {"pro": 55, "con": 45},
        "reasons": ["Pro cited distraction evidence.", "Con lacked counter-data."],
        "rationale": "Pro made the stronger case overall.",
    },
    {
        "ts": 7.0,
        "role": "cli",
        "turn_id": 0,
        "event_type": "cli_finished",
        "ledger": {
            "requests": 12,
            "llm_input_count": 800,
            "llm_output_count": 400,
            "llm_total_count": 1200,
            "usd_spent": 0.05,
        },
    },
]


class TestPrintReadableTranscript:
    def test_prints_motion(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        _write_transcript(p, _SAMPLE_RECORDS)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "Should schools ban smartphones?" in text
        assert "Motion:" in text

    def test_prints_search_call_and_results(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        _write_transcript(p, _SAMPLE_RECORDS)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "SEARCH CALL FROM pro" in text
        assert "smartphone ban schools evidence" in text
        assert "Search results (pro):" in text
        assert "Study on phones in class" in text
        assert "https://example.com/study" in text

    def test_prints_pro_and_con_replies(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        _write_transcript(p, _SAMPLE_RECORDS)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "ANSWER FROM pro" in text
        assert "ANSWER FROM con" in text
        assert "phase: opening" in text
        assert "round: 0" in text
        assert "Phones distract students" in text
        assert "Smartphones enable access" in text

    def test_prints_verdict(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        _write_transcript(p, _SAMPLE_RECORDS)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "--- Judge verdict ---" in text
        assert "winner: pro" in text
        assert "scores: pro=55 con=45" in text
        assert "Pro cited distraction evidence." in text
        assert "Pro made the stronger case overall." in text

    def test_prints_ledger_when_present(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        _write_transcript(p, _SAMPLE_RECORDS)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "--- Gatekeeper ledger ---" in text
        assert "requests: 12" in text
        assert "llm_input_count: 800" in text
        assert "llm_output_count: 400" in text
        assert "llm_total_count: 1200" in text
        assert "usd_spent: 0.05" in text

    def test_redacts_sensitive_keys_before_printing(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        records = list(_SAMPLE_RECORDS)
        records.insert(
            2,
            {
                "ts": 2.5,
                "role": "judge",
                "turn_id": 1,
                "event_type": "tool_call_received",
                "target_role": "con",
                "tool_call_payload": {
                    "tool": "search",
                    "query": "phones in schools",
                    "client_api_key": "sk-leak-test-key",
                },
            },
        )
        _write_transcript(p, records)
        out = io.StringIO()
        print_readable_transcript(p, out=out)
        text = out.getvalue()
        assert "sk-leak-test-key" not in text
        assert "phones in schools" in text

    def test_truncates_long_reply_content(self, tmp_path: Path) -> None:
        p = tmp_path / "run.jsonl"
        records = [
            _SAMPLE_RECORDS[0],
            {
                "ts": 2.0,
                "role": "judge",
                "turn_id": 1,
                "event_type": "reply_received",
                "target_role": "pro",
                "phase": "opening",
                "round": 0,
                "content": "x" * 5000,
            },
        ]
        _write_transcript(p, records)
        out = io.StringIO()
        print_readable_transcript(p, out=out, max_chars=100)
        text = out.getvalue()
        assert len(text) < 5000
        assert "[truncated]" in text

    def test_default_max_chars_constant(self) -> None:
        assert DEFAULT_MAX_PRINTED_TEXT_CHARS == 3000
