"""Debate CLI entry point (Stage 10).

Run with::

    uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake

The CLI is the user-facing wrapper around the Stage 9
:class:`debate.orchestration.judge.Judge` controller. It wires:

- :class:`debate.shared.config.DebateConfig` -> token / round /
  timeout limits;
- :class:`debate.shared.logger.RunLogger` ->
  ``runs/<run_id>/run.jsonl`` JSONL transcript;
- :class:`debate.orchestration.state_machine.DebateStateMachine` ->
  the Stage 5 pure FSM;
- :class:`debate.orchestration.supervisor.Supervisor` -> spawns Pro
  and Con as real ``python -m debate.agents.pro_agent /
  con_agent`` subprocesses;
- :class:`debate.shared.gatekeeper.Gatekeeper` /
  :class:`debate.shared.router.ToolRouter` /
  :class:`debate.sdk.search_client.FakeSearchClient` /
  :class:`debate.sdk.llm_client.FakeLLMClient`.

All defaults are **fake / offline**: no real API key is required to
run the demo or any test. The ``--fake`` flag is therefore a
no-op today (kept for forward compatibility - real-LLM mode will
land later via ``--no-fake`` + ``OPENAI_API_KEY``).

Two modes are supported:

* default: run a fresh debate end to end and write
  ``runs/<run_id>/run.jsonl``.
* ``--replay <path/to/run.jsonl>``: read a previously written
  transcript and pretty-print it. **No** LLM / search / subprocess
  calls happen in replay mode.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from debate import __version__
from debate.orchestration.judge import Judge
from debate.orchestration.state_machine import DebateStateMachine
from debate.orchestration.supervisor import Supervisor
from debate.sdk.llm_client import FakeLLMClient, LLMClient
from debate.sdk.schemas import Verdict
from debate.sdk.search_client import FakeSearchClient
from debate.shared.config import DebateConfig, load_debate_config, load_motions
from debate.shared.gatekeeper import Gatekeeper, GatekeeperPolicy
from debate.shared.logger import RunLogger
from debate.shared.router import ToolRouter

DEFAULT_MOTION: str = "AI-generated content should require mandatory labeling."
"""Used when neither ``--motion`` nor a non-empty ``config/motions.json``
is available. Mirrors the first entry in the shipped motions file."""

DEFAULT_RUNS_ROOT: Path = Path("runs")
"""Where ``RunLogger`` materializes the per-run subdirectory."""

DEFAULT_VERDICT_TEXT: str = (
    '{"winner":"pro","scores":{"pro":50,"con":40},'
    '"reasons":["Pro framed the motion clearly.",'
    '"Con conceded one structural point.",'
    '"Pro tied evidence to the motion."],'
    '"rationale":"Demo verdict from FakeLLMClient."}'
)
"""Canned verdict JSON used when running with the offline fake LLM.

The Stage 9 :class:`Judge` runs the LLM through the Gatekeeper to
produce the verdict, parses the response as JSON, and validates it.
:class:`debate.sdk.llm_client.FakeLLMClient` returns whatever string
it was constructed with, so feeding it pre-shaped JSON gives a
clean, deterministic, schema-valid demo verdict.
"""


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``argparse`` parser used by :func:`main`."""
    parser = argparse.ArgumentParser(
        prog="debate",
        description=(
            "Run a Pro/Con AI debate end-to-end. Default mode uses offline "
            "FakeLLMClient + FakeSearchClient and writes a JSONL transcript "
            "under runs/<timestamp>/run.jsonl."
        ),
    )
    parser.add_argument(
        "--motion",
        type=str,
        default=None,
        help="Debate motion. Defaults to the first entry in config/motions.json.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Number of argument rounds per side (1..100). Defaults to "
        "DebateConfig.rounds (config/debate.json: 10).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="fake",
        help="LLM model identifier. Currently only 'fake' is wired in; "
        "real provider support lands in a future stage.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed for the standard library RNG (logged for "
        "reproducibility; FakeLLMClient is already deterministic).",
    )
    parser.add_argument(
        "--fake",
        dest="fake",
        action="store_true",
        default=True,
        help="Use offline FakeLLMClient/FakeSearchClient (default).",
    )
    parser.add_argument(
        "--no-fake",
        dest="fake",
        action="store_false",
        help="Reserved: switch to real provider once one is wired in. "
        "Today this raises NotImplementedError.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to debate config JSON (defaults to config/debate.json).",
    )
    parser.add_argument(
        "--motions-file",
        type=str,
        default=None,
        help="Path to motions JSON (defaults to config/motions.json).",
    )
    parser.add_argument(
        "--runs-root",
        type=str,
        default=str(DEFAULT_RUNS_ROOT),
        help="Root directory for run transcripts (default: runs/).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional explicit run id (otherwise a UTC timestamp is used).",
    )
    parser.add_argument(
        "--replay",
        type=str,
        default=None,
        help="Path to a previously written run.jsonl. In replay mode "
        "no LLM / search / subprocess calls happen; the transcript is "
        "just pretty-printed.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress per-event progress output (the JSONL transcript is still written).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


# ---------------------------------------------------------------------------
# Banner / progress printing
# ---------------------------------------------------------------------------


def _print_banner(out: Any, *, motion: str, rounds: int, run_dir: Path, fake: bool) -> None:
    out.write(
        "\n".join(
            [
                "======================================================================",
                f"  HW2 - AI Agent Debate (v{__version__})",
                f"  motion : {motion}",
                f"  rounds : {rounds} (per side)",
                f"  mode   : {'fake / offline' if fake else 'real provider'}",
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


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def _load_or_default_debate_config(path_arg: str | None) -> DebateConfig:
    if path_arg is not None:
        return load_debate_config(path_arg)
    default_path = Path("config") / "debate.json"
    if default_path.exists():
        return load_debate_config(default_path)
    return DebateConfig(
        rounds=10,
        token_limit_per_turn=400,
        budget_total_tokens=50_000,
        heartbeat_seconds=5.0,
        max_message_bytes=64 * 1024,
        per_turn_timeout_seconds=30.0,
        total_timeout_seconds=300.0,
    )


def _resolve_motion(motion_arg: str | None, motions_path_arg: str | None) -> str:
    if motion_arg is not None and motion_arg.strip():
        return motion_arg
    candidate = Path(motions_path_arg) if motions_path_arg else Path("config") / "motions.json"
    if candidate.exists():
        try:
            motions = load_motions(candidate)
            if motions.motions:
                return motions.motions[0].topic
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return DEFAULT_MOTION


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Live debate
# ---------------------------------------------------------------------------


def _build_gatekeeper(cfg: DebateConfig) -> Gatekeeper:
    policy = GatekeeperPolicy(
        max_tokens_per_turn=cfg.token_limit_per_turn,
        max_tokens_per_debate=max(cfg.budget_total_tokens, cfg.token_limit_per_turn),
        max_usd_per_debate=10.0,
        max_requests_per_minute=600,
    )
    return Gatekeeper(policy)


def _build_router(gk: Gatekeeper) -> ToolRouter:
    return ToolRouter(gatekeeper=gk, search_client=FakeSearchClient(results_per_query=2))


def _build_judge_llm(*, model: str, fake: bool) -> LLMClient:
    if not fake:
        raise NotImplementedError(
            "Real LLM provider mode is not wired up yet; pass --fake (default) "
            "to run with the offline FakeLLMClient."
        )
    # ``model`` is recorded in the transcript so a future real-mode swap
    # can reuse the same CLI flag.
    del model
    return FakeLLMClient(response_text=DEFAULT_VERDICT_TEXT)


def _build_runs_dir(runs_root: Path, run_id: str | None) -> tuple[Path, RunLogger]:
    runs_root.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(runs_root=runs_root, run_id=run_id)
    return logger.run_dir, logger


def run_live(args: argparse.Namespace, out: Any) -> int:
    """Run a fresh end-to-end debate and write a transcript.

    Returns ``0`` on success, ``1`` on any handled error.
    """
    if args.seed is not None:
        random.seed(args.seed)

    cfg = _load_or_default_debate_config(args.config)
    motion = _resolve_motion(args.motion, args.motions_file)
    rounds = args.rounds if args.rounds is not None else cfg.rounds
    if rounds < 1 or rounds > 100:
        out.write(f"error: --rounds must be in [1, 100], got {rounds}\n")
        return 1

    runs_root = Path(args.runs_root)
    run_dir, logger = _build_runs_dir(runs_root, args.run_id)

    gk = _build_gatekeeper(cfg)
    router = _build_router(gk)
    fsm = DebateStateMachine(max_rounds=rounds)

    if not args.quiet:
        _print_banner(out, motion=motion, rounds=rounds, run_dir=run_dir, fake=args.fake)

    logger.log(
        role="cli",
        turn_id=0,
        event_type="cli_invoked",
        motion=motion,
        rounds=rounds,
        model=args.model,
        seed=args.seed,
        fake=args.fake,
        version=__version__,
    )

    try:
        judge_llm = _build_judge_llm(model=args.model, fake=args.fake)
    except NotImplementedError as exc:
        out.write(f"error: {exc}\n")
        return 1

    child_env = _build_child_env()

    with Supervisor(
        runs_dir=run_dir,
        env=child_env,
        terminate_timeout_s=cfg.per_turn_timeout_seconds,
    ) as supervisor:
        judge = Judge(
            supervisor=supervisor,
            fsm=fsm,
            router=router,
            gatekeeper=gk,
            llm_client=judge_llm,
            logger=logger,
            motion=motion,
            max_tokens_per_turn=cfg.token_limit_per_turn,
            per_turn_timeout_sec=cfg.per_turn_timeout_seconds,
        )
        try:
            verdict = judge.run_debate(motion=motion, rounds=rounds)
        except Exception as exc:  # noqa: BLE001 - top-level user-facing handler
            logger.log(
                role="cli",
                turn_id=0,
                event_type="cli_failed",
                error=type(exc).__name__,
                message=str(exc),
            )
            out.write(f"error: debate failed: {type(exc).__name__}: {exc}\n")
            return 1

    logger.log(
        role="cli",
        turn_id=0,
        event_type="cli_finished",
        winner=verdict.winner,
        ledger=_safe_ledger_snapshot(gk.ledger.snapshot()),
    )

    if not args.quiet:
        _print_summary(out, verdict=verdict, run_file=logger.run_file)
    return 0


def _safe_ledger_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a ledger snapshot whose keys won't trip the
    :mod:`debate.shared.redaction` substring filter.

    The Stage 3 redactor scrubs any key whose lower-cased name
    *contains* ``"token"`` (so e.g. an ``OPENAI_API_TOKEN`` field
    becomes ``<redacted>``). The :class:`Ledger.snapshot` keys
    (``tokens_in`` / ``tokens_out`` / ``total_tokens``) are
    counters, not secrets, but they trip the same filter. We
    rename them to ``llm_*_count`` for the on-disk record - the
    counts themselves stay verbatim.
    """
    return {
        "requests": snapshot.get("requests"),
        "llm_input_count": snapshot.get("tokens_in"),
        "llm_output_count": snapshot.get("tokens_out"),
        "llm_total_count": snapshot.get("total_tokens"),
        "usd_spent": snapshot.get("usd_spent"),
    }


def _build_child_env() -> dict[str, str]:
    """Return a minimal env dict for child agent subprocesses.

    The :class:`Supervisor` further filters this through its own
    allow-list and removes :data:`SEARCH_API_KEY` (defense in depth).
    We deliberately copy through the bits Python needs to import
    ``debate.agents.pro_agent`` / ``con_agent`` (PYTHONPATH for the
    ``src/`` layout, plus Windows-essential vars).
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOMEPATH",
        "HOMEDRIVE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    src_path = Path(__file__).resolve().parents[1]
    if src_path.is_dir():
        existing = env.get("PYTHONPATH", "")
        sep = os.pathsep
        env["PYTHONPATH"] = f"{src_path}{sep}{existing}" if existing else str(src_path)
    return env


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, out: Any | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 = success)."""
    if out is None:
        out = sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.replay is not None:
        return replay(Path(args.replay), out)

    return run_live(args, out)


if __name__ == "__main__":
    raise SystemExit(main())
