"""Debate CLI entry point (Stage 10).

See ``debate.cli_*`` and ``debate.provider_*`` for wiring details.
Supports live runs and ``--replay`` transcript pretty-printing.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from debate import __version__
from debate.cli_args import build_parser
from debate.cli_config import (
    DEFAULT_MOTION,
    DEFAULT_VERDICT_TEXT,
    _load_or_default_debate_config,
    _resolve_motion,
)
from debate.cli_output import _log_cli_failed, _print_banner, _print_summary, replay
from debate.orchestration.judge import Judge
from debate.orchestration.state_machine import DebateStateMachine
from debate.orchestration.supervisor import Supervisor
from debate.provider_child_env import _build_child_env
from debate.provider_factory import (
    _build_gatekeeper,
    _build_judge_llm,
    _build_router,
    _build_runs_dir,
    _resolve_modes,
    _safe_ledger_snapshot,
)
from debate.shared.secrets import maybe_load_dotenv
from debate.shared.transcript_log import print_readable_transcript


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
        return _log_cli_failed(logger, out, exc)

    try:
        judge_llm = _build_judge_llm(model=args.model, real_llm=real_llm)
    except Exception as exc:  # noqa: BLE001 - top-level user-facing handler
        return _log_cli_failed(logger, out, exc)

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
            return _log_cli_failed(logger, out, exc, label="debate failed")

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


__all__ = [
    "DEFAULT_MOTION",
    "DEFAULT_VERDICT_TEXT",
    "_build_child_env",
    "_build_gatekeeper",
    "_build_judge_llm",
    "_load_or_default_debate_config",
    "_resolve_motion",
    "build_parser",
    "main",
    "replay",
    "run_live",
]


if __name__ == "__main__":
    raise SystemExit(main())
