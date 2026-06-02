"""Debate phase drivers, child lifecycle, and run_debate."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from debate.orchestration import judge_bookkeeping as bk
from debate.orchestration import judge_turns
from debate.orchestration import verdict_pipeline as vp
from debate.orchestration.judge_logging import log
from debate.orchestration.judge_types import DebateHistory
from debate.orchestration.state_machine import Event
from debate.orchestration.supervisor import SupervisorError
from debate.sdk.schemas import MessageType, Phase, Verdict

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def run_debate(judge: Judge, motion: str | None = None, rounds: int | None = None) -> Verdict:
    topic = motion if motion is not None else judge._motion
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("motion must be a non-empty string")
    round_count = rounds if rounds is not None else judge._fsm.max_rounds
    if round_count < 1:
        raise ValueError("rounds must be >= 1")

    judge.history = DebateHistory(motion=topic)
    log(judge, "debate_started", motion=topic, rounds=round_count)
    try:
        judge._fsm.transition(Event.START)
        spawn_children(judge)
        judge._fsm.transition(Event.CHILDREN_READY)
        send_init_to_both(judge, topic)
        run_opening_phase(judge)
        judge._fsm.transition(Event.SENT_OPENINGS)
        run_argument_rounds(judge, round_count)
        run_closing_phase(judge)
        judge._fsm.transition(Event.CLOSINGS_RECEIVED)
        verdict = vp.verdict_with_retry(judge)
        log(judge, "debate_done", **vp.verdict_log_fields(verdict))
        return verdict
    finally:
        with contextlib.suppress(Exception):
            send_shutdown_to_both(judge)
        with contextlib.suppress(Exception):
            judge._supervisor.terminate_all()


def spawn_children(judge: Judge) -> None:
    try:
        judge._supervisor.spawn("pro")
        judge._supervisor.spawn("con")
    except SupervisorError as exc:
        log(judge, "spawn_failed", error=type(exc).__name__, message=str(exc))
        with contextlib.suppress(Exception):
            judge._fsm.transition(Event.SPAWN_FAILED)
        raise
    log(judge, "children_spawned", roles=["pro", "con"])


def send_init_to_both(judge: Judge, motion: str) -> None:
    for role in ("pro", "con"):
        init_msg = judge_turns.build_init(judge, role, motion)
        judge._supervisor.send(role, init_msg)
        log(
            judge,
            "init_sent",
            target_role=role,
            init_turn_id=init_msg.turn_id,
            motion_length=len(motion),
        )


def send_shutdown_to_both(judge: Judge) -> None:
    for role in ("pro", "con"):
        child = judge._supervisor.child(role)
        if child is None:
            continue
        shutdown_msg = bk.make_judge_message(judge, MessageType.SHUTDOWN, {})
        with contextlib.suppress(Exception):
            judge._supervisor.send(role, shutdown_msg)


def run_opening_phase(judge: Judge) -> None:
    pro_open = judge_turns.run_turn(judge, "pro", Phase.OPENING, opponent_last=None)
    bk.record_turn(judge, "pro", Phase.OPENING, round_number=0, reply=pro_open)
    con_open = judge_turns.run_turn(
        judge, "con", Phase.OPENING, opponent_last=pro_open.payload["content"]
    )
    bk.record_turn(judge, "con", Phase.OPENING, round_number=0, reply=con_open)


def run_argument_rounds(judge: Judge, round_count: int) -> None:
    for r in range(1, round_count + 1):
        pro_reply = judge_turns.run_turn(
            judge, "pro", Phase.ARGUMENT, opponent_last=judge.history.last_con
        )
        judge._fsm.transition(Event.PRO_REPLY)
        bk.record_turn(judge, "pro", Phase.ARGUMENT, round_number=r, reply=pro_reply)
        judge._fsm.transition(Event.SCORED)

        con_reply = judge_turns.run_turn(
            judge, "con", Phase.ARGUMENT, opponent_last=judge.history.last_pro
        )
        judge._fsm.transition(Event.CON_REPLY)
        bk.record_turn(judge, "con", Phase.ARGUMENT, round_number=r, reply=con_reply)
        judge._fsm.transition(Event.SCORED)

        if r < round_count:
            judge._fsm.transition(Event.SCORED)
        else:
            judge._fsm.transition(Event.ROUND_LIMIT_REACHED)


def run_closing_phase(judge: Judge) -> None:
    pro_close = judge_turns.run_turn(
        judge, "pro", Phase.CLOSING, opponent_last=judge.history.last_con
    )
    bk.record_turn(
        judge, "pro", Phase.CLOSING, round_number=judge._fsm.current_round, reply=pro_close
    )
    con_close = judge_turns.run_turn(
        judge, "con", Phase.CLOSING, opponent_last=pro_close.payload["content"]
    )
    bk.record_turn(
        judge, "con", Phase.CLOSING, round_number=judge._fsm.current_round, reply=con_close
    )
