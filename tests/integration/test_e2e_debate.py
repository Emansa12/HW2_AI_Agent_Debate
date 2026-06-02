"""End-to-end integration test for the Stage 10 CLI.

Runs the real :func:`debate.main.main` entry point. Pro and Con are
spawned as **real Python subprocesses** (``python -m
debate.agents.pro_agent`` / ``con_agent``) which themselves use
:class:`debate.sdk.llm_client.FakeLLMClient`, so the test exercises:

- argparse + CLI dispatch,
- :class:`debate.shared.config.DebateConfig` /
  :class:`debate.shared.config.Motion` loading,
- :class:`debate.shared.logger.RunLogger` writing
  ``runs/<id>/run.jsonl``,
- :class:`debate.orchestration.supervisor.Supervisor` spawning real
  child processes,
- :class:`debate.orchestration.judge.Judge` driving the FSM through
  every transition,
- :class:`debate.orchestration.ipc` JSONL serialization on real OS
  pipes,
- :class:`debate.shared.gatekeeper.Gatekeeper` /
  :class:`debate.shared.router.ToolRouter` budget bookkeeping,
- the Stage 10 verdict generation via FakeLLMClient (canned JSON in
  :data:`debate.main.DEFAULT_VERDICT_TEXT`).

No real API keys, no real network. Each test runs a fresh debate
into a per-test ``tmp_path`` so we never touch the repo's
``runs/`` directory.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from debate.main import main


def _run(tmp_path: Path, *extra: str) -> tuple[int, str, Path]:
    runs_root = tmp_path / "runs"
    out = io.StringIO()
    argv = [
        "--motion",
        "Is AI good for education?",
        "--rounds",
        "2",
        "--fake",
        "--runs-root",
        str(runs_root),
        "--quiet",
        *extra,
    ]
    rc = main(argv, out=out)
    return rc, out.getvalue(), runs_root


def _find_run_dir(runs_root: Path) -> Path:
    """Find the single ``runs/<id>/`` directory written by the run."""
    candidates = [p for p in runs_root.iterdir() if p.is_dir()]
    assert len(candidates) == 1, f"expected exactly one run dir, got {candidates!r}"
    return candidates[0]


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_two_round_fake_debate_produces_run_jsonl(self, tmp_path: Path) -> None:
        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0

        run_dir = _find_run_dir(runs_root)
        run_file = run_dir / "run.jsonl"
        assert run_file.exists(), "RunLogger must produce run.jsonl"

        records = _read_jsonl(run_file)
        assert len(records) > 0, "transcript must contain at least one record"

        # Required event types appear at least once each.
        event_types = {rec["event_type"] for rec in records}
        for required in (
            "cli_invoked",
            "debate_started",
            "children_spawned",
            "init_sent",
            "prompt_sent",
            "reply_received",
            "score_recorded",
            "verdict_recorded",
            "debate_done",
            "cli_finished",
        ):
            assert required in event_types, (
                f"missing required event {required!r}; got {sorted(event_types)}"
            )

        # Every record has the four mandatory shared fields.
        for rec in records:
            assert "ts" in rec
            assert "role" in rec
            assert "turn_id" in rec
            assert "event_type" in rec

        # Final verdict is pro or con (never tie). Schema-forbidden, but
        # we double-check on the wire too.
        cli_finished = [r for r in records if r["event_type"] == "cli_finished"]
        assert len(cli_finished) == 1
        assert cli_finished[0]["winner"] in ("pro", "con")
        # Ledger snapshot is included. Counter keys are renamed at
        # log-time (`tokens_in` -> `llm_input_count` etc.) so the
        # Stage 3 redaction filter doesn't scrub them - they're
        # counters, not secrets.
        assert "ledger" in cli_finished[0]
        ledger = cli_finished[0]["ledger"]
        assert "requests" in ledger
        assert "llm_input_count" in ledger
        assert "llm_output_count" in ledger
        assert "llm_total_count" in ledger
        assert "usd_spent" in ledger
        assert isinstance(ledger["requests"], int)
        assert isinstance(ledger["llm_input_count"], int)
        assert ledger["requests"] >= 1, (
            "verdict generation alone must consume at least one LLM request"
        )

    def test_transcript_includes_readable_debate_text(self, tmp_path: Path) -> None:
        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0
        records = _read_jsonl(_find_run_dir(runs_root) / "run.jsonl")

        prompts = [r for r in records if r["event_type"] == "prompt_sent"]
        replies = [r for r in records if r["event_type"] == "reply_received"]
        assert prompts, "expected at least one prompt_sent record"
        assert replies, "expected at least one reply_received record"

        for rec in prompts:
            assert isinstance(rec.get("prompt_text"), str) and rec["prompt_text"].strip()
            assert isinstance(rec.get("prompt_payload"), dict)
            assert rec.get("prompt_length", 0) >= len(rec["prompt_text"])

        for rec in replies:
            content = rec.get("content")
            assert isinstance(content, str) and content.strip()
            assert rec.get("content_length") == len(content)

        verdicts = [r for r in records if r["event_type"] == "verdict_recorded"]
        assert verdicts, "expected verdict_recorded"
        last = verdicts[-1]
        assert last["winner"] in ("pro", "con")
        assert isinstance(last.get("scores"), dict)
        scores = last["scores"]
        assert scores.get("pro") != scores.get("con"), (
            "verdict_recorded must not contain tied pro/con scores"
        )
        assert isinstance(last.get("reasons"), list) and len(last["reasons"]) >= 3
        assert isinstance(last.get("verdict_text"), str) and last["verdict_text"].strip()

        llm_verdict = [r for r in records if r["event_type"] == "verdict_llm_response"]
        assert llm_verdict, "expected verdict_llm_response"
        assert isinstance(llm_verdict[-1].get("verdict_text"), str)
        assert llm_verdict[-1].get("text_length") == len(llm_verdict[-1]["verdict_text"])

    def test_brokered_search_tool_calls_in_fake_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Children emit tool_call when DEBATE_REAL_SEARCH=1; parent still
        uses FakeSearchClient (no network, no API keys)."""
        import debate.main as main_module

        original_build = main_module._build_child_env

        def _force_search_env(*, real_llm: bool = False, real_search: bool = False):
            env = original_build(real_llm=real_llm, real_search=real_search)
            env["DEBATE_REAL_SEARCH"] = "1"
            return env

        monkeypatch.setattr(main_module, "_build_child_env", _force_search_env)

        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0
        records = _read_jsonl(_find_run_dir(runs_root) / "run.jsonl")
        event_types = {r["event_type"] for r in records}
        assert "tool_call_received" in event_types
        assert "tool_result_sent" in event_types

        tool_results = [r for r in records if r["event_type"] == "tool_result_sent"]
        assert tool_results, "expected tool_result_sent records"
        payload_text = str(tool_results[0].get("tool_result_payload", {}))
        assert "https://example.com" in payload_text or "url" in payload_text.lower()

    def test_verdict_event_has_pro_or_con_winner(self, tmp_path: Path) -> None:
        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0
        records = _read_jsonl(_find_run_dir(runs_root) / "run.jsonl")
        verdicts = [r for r in records if r["event_type"] == "verdict_recorded"]
        assert verdicts, "transcript must contain a verdict_recorded record"
        winner = verdicts[-1]["winner"]
        assert winner in ("pro", "con"), f"tie verdict is forbidden; got winner={winner!r}"

    def test_no_secret_values_in_transcript(self, tmp_path: Path) -> None:
        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0
        text = (_find_run_dir(runs_root) / "run.jsonl").read_text(encoding="utf-8")
        # Sensitive substrings would have been redacted to "<redacted>"
        # by the Stage 3 redactor; if any of these strings appear as
        # a *value*, that's a bug.
        for forbidden in ("sk-", "AKIA", "AIza"):
            assert forbidden not in text, (
                f"transcript contains a value matching {forbidden!r}; redaction may have leaked"
            )

    def test_replay_after_run_returns_zero(self, tmp_path: Path) -> None:
        rc, _stdout, runs_root = _run(tmp_path)
        assert rc == 0
        run_file = _find_run_dir(runs_root) / "run.jsonl"

        out = io.StringIO()
        rc2 = main(["--replay", str(run_file)], out=out)
        assert rc2 == 0
        text = out.getvalue()
        assert "Replay" in text
        assert "winner" in text


class TestQuietMode:
    def test_quiet_mode_produces_minimal_stdout(self, tmp_path: Path) -> None:
        rc, stdout, _ = _run(tmp_path)
        assert rc == 0
        # --quiet suppresses banner and summary; only argparse / fatal
        # error paths print anything else, and we hit none of them.
        # (We accept up to a couple of stray newlines from any future
        # error guards as long as no banner / summary leaked.)
        assert "Final verdict" not in stdout
        assert "HW2 - AI Agent Debate" not in stdout


class TestSeedFlag:
    def test_seed_does_not_break_run(self, tmp_path: Path) -> None:
        rc, _stdout, _ = _run(tmp_path, "--seed", "12345")
        assert rc == 0


# ---------------------------------------------------------------------------
# Demo CLI command from the spec
# ---------------------------------------------------------------------------


class TestSpecDemoCommand:
    def test_spec_demo_command_succeeds(self, tmp_path: Path) -> None:
        """Stage 10 spec demo command:

            uv run python -m debate.main \
                --motion "Is AI good for education?" --rounds 2 --fake

        Must run cleanly and produce a parseable transcript."""
        runs_root = tmp_path / "spec_runs"
        out = io.StringIO()
        rc = main(
            [
                "--motion",
                "Is AI good for education?",
                "--rounds",
                "2",
                "--fake",
                "--runs-root",
                str(runs_root),
                "--quiet",
            ],
            out=out,
        )
        assert rc == 0
        run_dir = _find_run_dir(runs_root)
        run_file = run_dir / "run.jsonl"
        assert run_file.exists()
        records = _read_jsonl(run_file)
        assert any(r["event_type"] == "debate_done" for r in records)


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: scrub any real API keys from the test process
    env so a misconfigured dev box can't accidentally hit a provider."""
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SEARCH_API_KEY"):
        monkeypatch.delenv(key, raising=False)
