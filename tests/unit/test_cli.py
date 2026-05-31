"""Unit tests for the Stage 10 CLI in :mod:`debate.main`.

These tests exercise the argument parser, replay mode, config /
motion resolution, and the surface-level error paths. The
end-to-end ``run_live`` flow (which spawns Pro/Con subprocesses) is
exercised separately by
``tests/integration/test_e2e_debate.py``.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from debate.main import (
    DEFAULT_MOTION,
    DEFAULT_VERDICT_TEXT,
    _build_child_env,
    _build_gatekeeper,
    _build_judge_llm,
    _load_or_default_debate_config,
    _resolve_motion,
    build_parser,
    main,
    replay,
)
from debate.shared.config import DebateConfig

# ---------------------------------------------------------------------------
# build_parser / argument parsing
# ---------------------------------------------------------------------------


class TestParser:
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        assert args.motion is None
        assert args.rounds is None
        assert args.model == "fake"
        assert args.seed is None
        assert args.fake is True
        assert args.real_search is False
        assert args.real_llm is False
        assert args.replay is None
        assert args.config is None
        assert args.motions_file is None
        assert args.runs_root == "runs"
        assert args.run_id is None
        assert args.quiet is False

    def test_real_search_flag(self) -> None:
        args = build_parser().parse_args(["--real-search"])
        assert args.real_search is True
        assert args.fake is True  # combinable with --fake

    def test_real_llm_flag(self) -> None:
        args = build_parser().parse_args(["--real-llm"])
        assert args.real_llm is True

    def test_no_fake_implies_real_modes(self) -> None:
        """``--no-fake`` is shorthand; the actual mode resolution
        happens in :func:`debate.main._resolve_modes`."""
        from debate.main import _resolve_modes

        args = build_parser().parse_args(["--no-fake"])
        real_llm, real_search = _resolve_modes(args)
        assert real_llm is True
        assert real_search is True

    def test_fake_with_real_search_only(self) -> None:
        from debate.main import _resolve_modes

        args = build_parser().parse_args(["--fake", "--real-search"])
        real_llm, real_search = _resolve_modes(args)
        assert real_llm is False
        assert real_search is True

    def test_all_flags_parse(self) -> None:
        argv = [
            "--motion",
            "M",
            "--rounds",
            "3",
            "--model",
            "fake",
            "--seed",
            "7",
            "--no-fake",
            "--config",
            "x.json",
            "--motions-file",
            "y.json",
            "--runs-root",
            "tmp_runs",
            "--run-id",
            "explicit-id",
            "--quiet",
        ]
        args = build_parser().parse_args(argv)
        assert args.motion == "M"
        assert args.rounds == 3
        assert args.seed == 7
        assert args.fake is False
        assert args.config == "x.json"
        assert args.motions_file == "y.json"
        assert args.runs_root == "tmp_runs"
        assert args.run_id == "explicit-id"
        assert args.quiet is True

    def test_replay_flag(self) -> None:
        args = build_parser().parse_args(["--replay", "runs/old/run.jsonl"])
        assert args.replay == "runs/old/run.jsonl"

    def test_version_flag_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as ei:
            build_parser().parse_args(["--version"])
        assert ei.value.code == 0


# ---------------------------------------------------------------------------
# _resolve_motion / _load_or_default_debate_config
# ---------------------------------------------------------------------------


class TestMotionResolution:
    def test_explicit_motion_wins(self, tmp_path: Path) -> None:
        assert _resolve_motion("explicit", None) == "explicit"

    def test_blank_motion_falls_back_to_motions_file(self, tmp_path: Path) -> None:
        m_path = tmp_path / "motions.json"
        m_path.write_text(
            json.dumps(
                {
                    "motions": [
                        {"id": "abc", "topic": "First topic"},
                        {"id": "def", "topic": "Second topic"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert _resolve_motion(None, str(m_path)) == "First topic"

    def test_missing_motions_file_falls_back_to_default(self, tmp_path: Path) -> None:
        assert _resolve_motion(None, str(tmp_path / "nope.json")) == DEFAULT_MOTION

    def test_malformed_motions_file_falls_back_to_default(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert _resolve_motion(None, str(bad)) == DEFAULT_MOTION

    def test_empty_motion_string_falls_back(self, tmp_path: Path) -> None:
        assert _resolve_motion("   ", str(tmp_path / "missing.json")) == DEFAULT_MOTION


class TestConfigLoad:
    def test_explicit_path_loaded(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "debate.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "rounds": 2,
                    "token_limit_per_turn": 200,
                    "budget_total_tokens": 5000,
                    "heartbeat_seconds": 1.0,
                    "max_message_bytes": 65536,
                    "per_turn_timeout_seconds": 5.0,
                    "total_timeout_seconds": 30.0,
                }
            ),
            encoding="utf-8",
        )
        cfg = _load_or_default_debate_config(str(cfg_path))
        assert isinstance(cfg, DebateConfig)
        assert cfg.rounds == 2

    def test_missing_default_path_returns_safe_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no config/debate.json under tmp_path
        cfg = _load_or_default_debate_config(None)
        assert isinstance(cfg, DebateConfig)
        assert cfg.rounds >= 1


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_build_gatekeeper_uses_config(self) -> None:
        cfg = DebateConfig(
            rounds=2,
            token_limit_per_turn=200,
            budget_total_tokens=5000,
            heartbeat_seconds=1.0,
            max_message_bytes=65536,
            per_turn_timeout_seconds=5.0,
            total_timeout_seconds=30.0,
        )
        gk = _build_gatekeeper(cfg)
        assert gk.policy.max_tokens_per_turn == 200
        assert gk.policy.max_tokens_per_debate >= 200

    def test_build_judge_llm_fake_mode_returns_canned_verdict(self) -> None:
        llm = _build_judge_llm(model="fake", real_llm=False)
        resp = llm.complete(prompt="anything", max_tokens=100)
        # Canned text is shaped as a verdict JSON.
        assert resp.text == DEFAULT_VERDICT_TEXT
        data = json.loads(resp.text)
        assert data["winner"] in ("pro", "con")

    def test_build_judge_llm_real_mode_without_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from debate.sdk.real_llm_client import MissingLLMAPIKeyError

        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(MissingLLMAPIKeyError):
            _build_judge_llm(model="gpt-4o-mini", real_llm=True)

    def test_build_child_env_does_not_leak_search_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEARCH_API_KEY", "should-not-leak")
        monkeypatch.setenv("TAVILY_API_KEY", "should-not-leak")
        env = _build_child_env()
        # The CLI helper does NOT add the search keys itself; the
        # Supervisor's deny-list strips them again as defense-in-depth
        # before the child process is spawned.
        assert "SEARCH_API_KEY" not in env
        assert "TAVILY_API_KEY" not in env

    def test_build_child_env_real_llm_forwards_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
        env = _build_child_env(real_llm=True)
        assert env.get("DEBATE_REAL_LLM") == "1"
        assert env.get("OPENAI_API_KEY") == "sk-fake-test"
        assert env.get("OPENAI_MODEL") == "gpt-4o-mini"

    def test_build_child_env_fake_mode_does_not_set_real_llm_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-test")
        env = _build_child_env(real_llm=False)
        assert "DEBATE_REAL_LLM" not in env
        # The key is *not* forwarded in fake mode (defense in depth -
        # children have no use for it when they're using FakeLLMClient).
        assert "OPENAI_API_KEY" not in env

    def test_build_child_env_adds_pythonpath_for_src_layout(self) -> None:
        env = _build_child_env()
        assert "PYTHONPATH" in env
        assert "src" in env["PYTHONPATH"].lower() or "debate" in env["PYTHONPATH"].lower()


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


class TestReplay:
    def test_replay_missing_file_returns_one(self, tmp_path: Path) -> None:
        out = io.StringIO()
        rc = replay(tmp_path / "nope.jsonl", out)
        assert rc == 1
        assert "not found" in out.getvalue()

    def test_replay_empty_file_returns_one(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        out = io.StringIO()
        rc = replay(p, out)
        assert rc == 1

    def test_replay_well_formed_run_returns_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "good.jsonl"
        _write_transcript(
            p,
            [
                {"ts": 1.0, "role": "judge", "turn_id": 0, "event_type": "debate_started"},
                {"ts": 2.0, "role": "judge", "turn_id": 1, "event_type": "init_sent"},
                {
                    "ts": 3.0,
                    "role": "judge",
                    "turn_id": 5,
                    "event_type": "verdict_recorded",
                    "winner": "pro",
                    "scores": {"pro": 50, "con": 40},
                    "reasons_count": 3,
                    "source": "llm",
                },
                {
                    "ts": 4.0,
                    "role": "judge",
                    "turn_id": 5,
                    "event_type": "debate_done",
                    "winner": "pro",
                },
            ],
        )
        out = io.StringIO()
        rc = replay(p, out)
        assert rc == 0
        text = out.getvalue()
        assert "winner" in text
        assert "pro" in text
        assert "verdict_recorded" in text

    def test_replay_skips_malformed_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "mixed.jsonl"
        p.write_text(
            "\n".join(
                [
                    json.dumps({"ts": 1.0, "role": "judge", "turn_id": 0, "event_type": "x"}),
                    "{not valid json",
                    json.dumps(
                        {"ts": 2.0, "role": "judge", "turn_id": 1, "event_type": "debate_done"}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out = io.StringIO()
        rc = replay(p, out)
        assert rc == 0
        text = out.getvalue()
        assert "WARN" in text
        assert "debate_done" in text

    def test_replay_does_not_call_any_network(self, tmp_path: Path) -> None:
        """The replay path is intentionally narrow. Any attempt to
        instantiate Supervisor / FakeLLMClient / FakeSearchClient
        would balloon imports - just verify the function only
        consumes a file.
        """
        p = tmp_path / "x.jsonl"
        _write_transcript(
            p, [{"ts": 1.0, "role": "judge", "turn_id": 0, "event_type": "debate_done"}]
        )
        out = io.StringIO()
        rc = replay(p, out)
        assert rc == 0


# ---------------------------------------------------------------------------
# main(...) dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_main_dispatches_to_replay(self, tmp_path: Path) -> None:
        p = tmp_path / "r.jsonl"
        _write_transcript(
            p, [{"ts": 1.0, "role": "judge", "turn_id": 0, "event_type": "debate_done"}]
        )
        out = io.StringIO()
        rc = main(["--replay", str(p)], out=out)
        assert rc == 0
        assert "Replay" in out.getvalue()

    def test_main_rejects_invalid_rounds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = io.StringIO()
        rc = main(
            ["--motion", "x", "--rounds", "0", "--runs-root", str(tmp_path / "runs")],
            out=out,
        )
        assert rc == 1
        assert "rounds" in out.getvalue()

    def test_main_no_fake_without_keys_fails_clearly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stage 11: ``--no-fake`` is shorthand for
        ``--real-llm --real-search``. Without API keys it must
        fail cleanly with a typed error pointing the user at the
        env var to set, not raise an unhandled exception.
        """
        monkeypatch.chdir(tmp_path)
        for key in ("LLM_API_KEY", "OPENAI_API_KEY", "SEARCH_API_KEY", "TAVILY_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        out = io.StringIO()
        rc = main(
            [
                "--motion",
                "x",
                "--rounds",
                "1",
                "--no-fake",
                "--runs-root",
                str(tmp_path / "runs"),
            ],
            out=out,
        )
        assert rc == 1
        text = out.getvalue().lower()
        # Either the search OR the LLM key check fires first; both
        # error messages mention the missing env var name.
        assert "missing" in text
        assert "api key" in text

    def test_main_real_search_without_key_fails_clearly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for key in ("SEARCH_API_KEY", "TAVILY_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        out = io.StringIO()
        rc = main(
            [
                "--motion",
                "x",
                "--rounds",
                "1",
                "--fake",
                "--real-search",
                "--runs-root",
                str(tmp_path / "runs"),
            ],
            out=out,
        )
        assert rc == 1
        text = out.getvalue().lower()
        assert "missing" in text
        assert "search" in text

    def test_main_real_llm_without_key_fails_clearly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for key in ("LLM_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        out = io.StringIO()
        rc = main(
            [
                "--motion",
                "x",
                "--rounds",
                "1",
                "--real-llm",
                "--runs-root",
                str(tmp_path / "runs"),
            ],
            out=out,
        )
        assert rc == 1
        text = out.getvalue().lower()
        assert "missing" in text
        assert "llm" in text


# ---------------------------------------------------------------------------
# Argv / Namespace shapes
# ---------------------------------------------------------------------------


class TestNamespaceShape:
    def test_namespace_has_expected_attrs(self) -> None:
        ns = build_parser().parse_args([])
        assert isinstance(ns, argparse.Namespace)
        for attr in (
            "motion",
            "rounds",
            "model",
            "seed",
            "fake",
            "real_search",
            "real_llm",
            "config",
            "motions_file",
            "runs_root",
            "run_id",
            "replay",
            "quiet",
        ):
            assert hasattr(ns, attr), f"namespace is missing {attr!r}"
