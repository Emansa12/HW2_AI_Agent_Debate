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
from debate.sdk.search_client import FakeSearchClient, SearchClient
from debate.shared.config import DebateConfig, load_debate_config, load_motions
from debate.shared.gatekeeper import Gatekeeper, GatekeeperPolicy
from debate.shared.logger import RunLogger
from debate.shared.router import ToolRouter
from debate.shared.secrets import maybe_load_dotenv
from debate.shared.transcript_log import print_readable_transcript

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
        help="LLM model identifier. Used when `--real-llm` is set (passed "
        "through to RealLLMClient). 'fake' (default) selects FakeLLMClient.",
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
        help="Shorthand for `--real-llm --real-search`. Both real "
        "clients require API keys in the environment "
        "(LLM_API_KEY/OPENAI_API_KEY, SEARCH_API_KEY/TAVILY_API_KEY).",
    )
    parser.add_argument(
        "--real-search",
        dest="real_search",
        action="store_true",
        default=False,
        help="Stage 11: use the real Tavily-backed SearchClient for "
        "tool calls. Requires SEARCH_API_KEY (or TAVILY_API_KEY) in "
        "the environment. The Gatekeeper + ToolRouter still wrap "
        "every call.",
    )
    parser.add_argument(
        "--real-llm",
        dest="real_llm",
        action="store_true",
        default=False,
        help="Stage 11: use the real OpenAI-compatible LLMClient for "
        "the Judge's verdict generation AND for both Pro/Con "
        "subprocesses. Requires LLM_API_KEY (or OPENAI_API_KEY) in "
        "the environment.",
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
        "--print-transcript",
        action="store_true",
        default=False,
        help="After a live run, print a readable transcript summary from run.jsonl.",
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


def _build_router(gk: Gatekeeper, *, real_search: bool) -> ToolRouter:
    """Build the Stage 4 :class:`ToolRouter`.

    ``real_search=True`` swaps in the Stage 11
    :class:`RealSearchClient` (Tavily), which raises
    :class:`MissingSearchAPIKeyError` if neither
    ``SEARCH_API_KEY`` nor ``TAVILY_API_KEY`` is set. Either way
    the Gatekeeper + LRU cache still wrap every call.
    """
    client: SearchClient
    if real_search:
        from debate.sdk.real_search_client import RealSearchClient

        client = RealSearchClient.from_env()
    else:
        client = FakeSearchClient(results_per_query=2)
    return ToolRouter(gatekeeper=gk, search_client=client)


def _build_judge_llm(*, model: str, real_llm: bool) -> LLMClient:
    """Build the Judge-side :class:`LLMClient`.

    ``real_llm=True`` swaps in the Stage 11
    :class:`RealLLMClient`, which raises
    :class:`MissingLLMAPIKeyError` if neither ``LLM_API_KEY`` nor
    ``OPENAI_API_KEY`` is set. Otherwise the offline
    :class:`FakeLLMClient` is used (pre-loaded with the canned
    Stage 10 verdict JSON for a clean demo).
    """
    if real_llm:
        from debate.sdk.real_llm_client import RealLLMClient

        kwargs: dict[str, Any] = {}
        if model and model != "fake":
            kwargs["model"] = model
        return RealLLMClient.from_env(**kwargs)

    # ``model`` is recorded in the transcript so a real-mode swap
    # can reuse the same CLI flag.
    del model
    return FakeLLMClient(response_text=DEFAULT_VERDICT_TEXT)


def _build_runs_dir(runs_root: Path, run_id: str | None) -> tuple[Path, RunLogger]:
    runs_root.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(runs_root=runs_root, run_id=run_id)
    return logger.run_dir, logger


def _resolve_modes(args: argparse.Namespace) -> tuple[bool, bool]:
    """Return ``(use_real_llm, use_real_search)`` after collapsing
    ``--fake`` / ``--no-fake`` / ``--real-llm`` / ``--real-search``.

    Rules:

    - Default: both real flags are off (full fake mode).
    - ``--no-fake`` is shorthand for both real flags on, but a
      caller-specified ``--real-llm`` / ``--real-search`` wins if
      present.
    - ``--real-llm`` and ``--real-search`` can be combined with
      ``--fake`` (the default) so e.g. fake LLM + real search is a
      legitimate hybrid demo mode.
    """
    real_llm = args.real_llm or (not args.fake)
    real_search = args.real_search or (not args.fake)
    return real_llm, real_search


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

    real_llm, real_search = _resolve_modes(args)

    runs_root = Path(args.runs_root)
    run_dir, logger = _build_runs_dir(runs_root, args.run_id)

    gk = _build_gatekeeper(cfg)
    fsm = DebateStateMachine(max_rounds=rounds)

    if not args.quiet:
        _print_banner(
            out,
            motion=motion,
            rounds=rounds,
            run_dir=run_dir,
            fake=args.fake,
            real_llm=real_llm,
            real_search=real_search,
        )

    logger.log(
        role="cli",
        turn_id=0,
        event_type="cli_invoked",
        motion=motion,
        rounds=rounds,
        model=args.model,
        seed=args.seed,
        fake=args.fake,
        real_llm=real_llm,
        real_search=real_search,
        version=__version__,
    )

    try:
        router = _build_router(gk, real_search=real_search)
    except Exception as exc:  # noqa: BLE001 - top-level user-facing handler
        logger.log(
            role="cli",
            turn_id=0,
            event_type="cli_failed",
            error=type(exc).__name__,
            message=str(exc),
        )
        out.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1

    try:
        judge_llm = _build_judge_llm(model=args.model, real_llm=real_llm)
    except Exception as exc:  # noqa: BLE001 - top-level user-facing handler
        logger.log(
            role="cli",
            turn_id=0,
            event_type="cli_failed",
            error=type(exc).__name__,
            message=str(exc),
        )
        out.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1

    child_env = _build_child_env(real_llm=real_llm, real_search=real_search)

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
            max_logged_text_chars=cfg.max_logged_text_chars,
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

    if args.print_transcript:
        print_readable_transcript(logger.run_file, out=out)

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


def _build_child_env(*, real_llm: bool = False, real_search: bool = False) -> dict[str, str]:
    """Return a minimal env dict for child agent subprocesses.

    The :class:`Supervisor` further filters this through its own
    allow-list and removes search API keys (defense in depth).
    We deliberately copy through the bits Python needs to import
    ``debate.agents.pro_agent`` / ``con_agent`` (PYTHONPATH for the
    ``src/`` layout, plus Windows-essential vars).

    When ``real_llm=True`` (Stage 11) we also forward the
    ``LLM_API_KEY`` / ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
    ``OPENAI_MODEL`` values from the parent env (so the child's
    ``RealLLMClient.from_env()`` finds them) and set
    ``DEBATE_REAL_LLM=1`` to flip the child's ``__main__`` block
    to the real client.

    When ``real_search=True``, set ``DEBATE_REAL_SEARCH=1`` so each
    debater emits one brokered search ``tool_call`` on opening /
    first argument. Search keys stay in the parent only.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    passthrough_keys = (
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
    )
    for key in passthrough_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    if real_llm:
        env["DEBATE_REAL_LLM"] = "1"
        for key in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
            if key in os.environ:
                env[key] = os.environ[key]

    if real_search:
        env["DEBATE_REAL_SEARCH"] = "1"

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
    maybe_load_dotenv()
    if out is None:
        out = sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.replay is not None:
        return replay(Path(args.replay), out)

    return run_live(args, out)


if __name__ == "__main__":
    raise SystemExit(main())
