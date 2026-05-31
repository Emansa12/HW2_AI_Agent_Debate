"""Unit tests for the Stage 5 deterministic debate state machine.

Covers:

- happy paths through 1 and 10 rounds;
- illegal transitions are rejected with a typed exception;
- budget exhaustion aborts from many non-terminal states;
- spawn failure aborts;
- verdict retry once -> success;
- verdict retry once -> tie-break -> DONE;
- heartbeat recovery from Pro and Con turns;
- restarts exhausted -> ABORT;
- `is_terminal()` semantics;
- the FSM module imports nothing from LLM / Search / Supervisor /
  Agent layers (purity).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from debate.orchestration import (
    DebateStateMachine,
    Event,
    IllegalTransitionError,
    State,
)
from debate.orchestration import state_machine as sm_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drive_to_pro_turn(sm: DebateStateMachine) -> None:
    """INIT -> SPAWNING -> OPENING -> PRO_TURN."""
    sm.transition(Event.START)
    sm.transition(Event.CHILDREN_READY)
    sm.transition(Event.SENT_OPENINGS)


def _drive_to_con_turn(sm: DebateStateMachine) -> None:
    """Get to CON_TURN of the first round."""
    _drive_to_pro_turn(sm)
    sm.transition(Event.PRO_REPLY)
    sm.transition(Event.SCORED)


def _drive_through_rounds(sm: DebateStateMachine, n_rounds: int) -> None:
    """Drive the FSM through exactly `n_rounds` full rounds.

    Ends with the FSM in `State.CLOSING`.
    """
    _drive_to_pro_turn(sm)
    for r in range(n_rounds):
        assert sm.state is State.PRO_TURN
        sm.transition(Event.PRO_REPLY)
        assert sm.state is State.SCORE_PRO
        sm.transition(Event.SCORED)
        assert sm.state is State.CON_TURN
        sm.transition(Event.CON_REPLY)
        assert sm.state is State.SCORE_CON
        sm.transition(Event.SCORED)
        assert sm.state is State.NEXT_ROUND
        assert sm.current_round == r + 1

        if r < n_rounds - 1:
            sm.transition(Event.SCORED)
        else:
            sm.transition(Event.ROUND_LIMIT_REACHED)
    assert sm.state is State.CLOSING


def _finish_with_valid_verdict(sm: DebateStateMachine) -> None:
    """CLOSING -> ... -> DONE via a valid non-tie verdict."""
    sm.transition(Event.CLOSINGS_RECEIVED)
    sm.transition(Event.JUDGE_REPLY)
    sm.transition(Event.VALID_NON_TIE)


# ---------------------------------------------------------------------------
# Constructor / invariants
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_initial_state(self) -> None:
        sm = DebateStateMachine()
        assert sm.state is State.INIT
        assert sm.current_round == 0
        assert sm.verdict_retry_count == 0
        assert sm.last_missed_role is None
        assert sm.remembered_turn_state is None
        assert sm.max_rounds == 10
        assert not sm.is_terminal()

    def test_custom_max_rounds(self) -> None:
        sm = DebateStateMachine(max_rounds=3)
        assert sm.max_rounds == 3

    @pytest.mark.parametrize("bad", [0, -1, -10])
    def test_rejects_invalid_max_rounds(self, bad: int) -> None:
        with pytest.raises(ValueError):
            DebateStateMachine(max_rounds=bad)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_happy_path_one_round(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        assert sm.state is State.CLOSING
        assert sm.current_round == 1

        _finish_with_valid_verdict(sm)
        assert sm.state is State.DONE
        assert sm.is_terminal()
        assert sm.verdict_retry_count == 0

    def test_happy_path_ten_rounds(self) -> None:
        sm = DebateStateMachine(max_rounds=10)
        _drive_through_rounds(sm, 10)
        assert sm.state is State.CLOSING
        assert sm.current_round == 10

        _finish_with_valid_verdict(sm)
        assert sm.state is State.DONE
        assert sm.is_terminal()

    def test_transition_returns_new_state(self) -> None:
        sm = DebateStateMachine()
        assert sm.transition(Event.START) is State.SPAWNING
        assert sm.transition(Event.CHILDREN_READY) is State.OPENING
        assert sm.transition(Event.SENT_OPENINGS) is State.PRO_TURN

    def test_string_event_names_accepted(self) -> None:
        sm = DebateStateMachine()
        assert sm.transition("start") is State.SPAWNING
        assert sm.transition("children_ready") is State.OPENING


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


class TestIllegalTransitions:
    def test_unknown_event_string_rejected(self) -> None:
        sm = DebateStateMachine()
        with pytest.raises(IllegalTransitionError) as exc:
            sm.transition("not_a_real_event")
        assert exc.value.state is State.INIT

    def test_wrong_event_for_state_rejected(self) -> None:
        sm = DebateStateMachine()
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.PRO_REPLY)

    def test_event_from_init_other_than_start_is_rejected(self) -> None:
        for ev in [
            Event.CHILDREN_READY,
            Event.SENT_OPENINGS,
            Event.PRO_REPLY,
            Event.CON_REPLY,
            Event.SCORED,
            Event.JUDGE_REPLY,
            Event.RESPAWNED,
        ]:
            sm_fresh = DebateStateMachine()
            with pytest.raises(IllegalTransitionError):
                sm_fresh.transition(ev)

    def test_transition_after_done_raises(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        _finish_with_valid_verdict(sm)
        assert sm.is_terminal()
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.START)

    def test_transition_after_abort_raises(self) -> None:
        sm = DebateStateMachine()
        sm.transition(Event.START)
        sm.transition(Event.SPAWN_FAILED)
        assert sm.state is State.ABORT
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.CHILDREN_READY)

    def test_state_unchanged_on_illegal_transition(self) -> None:
        sm = DebateStateMachine()
        sm.transition(Event.START)
        assert sm.state is State.SPAWNING
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.PRO_REPLY)
        assert sm.state is State.SPAWNING

    def test_exception_carries_state_and_event(self) -> None:
        sm = DebateStateMachine()
        with pytest.raises(IllegalTransitionError) as exc:
            sm.transition(Event.PRO_REPLY)
        assert exc.value.state is State.INIT
        assert exc.value.event is Event.PRO_REPLY


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


class TestBudgetExhaustion:
    @staticmethod
    def _at_init(sm: DebateStateMachine) -> None:
        return None

    @staticmethod
    def _at_spawning(sm: DebateStateMachine) -> None:
        sm.transition(Event.START)

    @staticmethod
    def _at_opening(sm: DebateStateMachine) -> None:
        sm.transition(Event.START)
        sm.transition(Event.CHILDREN_READY)

    @staticmethod
    def _at_pro_turn(sm: DebateStateMachine) -> None:
        _drive_to_pro_turn(sm)

    @staticmethod
    def _at_score_pro(sm: DebateStateMachine) -> None:
        _drive_to_pro_turn(sm)
        sm.transition(Event.PRO_REPLY)

    @staticmethod
    def _at_con_turn(sm: DebateStateMachine) -> None:
        _drive_to_con_turn(sm)

    @staticmethod
    def _at_score_con(sm: DebateStateMachine) -> None:
        _drive_to_con_turn(sm)
        sm.transition(Event.CON_REPLY)

    @staticmethod
    def _at_next_round(sm: DebateStateMachine) -> None:
        _drive_to_con_turn(sm)
        sm.transition(Event.CON_REPLY)
        sm.transition(Event.SCORED)

    @staticmethod
    def _at_closing(sm: DebateStateMachine) -> None:
        _drive_through_rounds(sm, 1)

    @staticmethod
    def _at_verdict(sm: DebateStateMachine) -> None:
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)

    @staticmethod
    def _at_validate_verdict(sm: DebateStateMachine) -> None:
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)
        sm.transition(Event.JUDGE_REPLY)

    @staticmethod
    def _at_tie_break(sm: DebateStateMachine) -> None:
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)
        sm.transition(Event.JUDGE_REPLY)
        sm.transition(Event.INVALID_OR_TIE)
        sm.transition(Event.JUDGE_REPLY)
        sm.transition(Event.INVALID_OR_TIE)

    @staticmethod
    def _at_recover(sm: DebateStateMachine) -> None:
        _drive_to_pro_turn(sm)
        sm.transition(Event.HEARTBEAT_MISS)

    @pytest.mark.parametrize(
        ("driver", "expected_state_before"),
        [
            (_at_init, State.INIT),
            (_at_spawning, State.SPAWNING),
            (_at_opening, State.OPENING),
            (_at_pro_turn, State.PRO_TURN),
            (_at_score_pro, State.SCORE_PRO),
            (_at_con_turn, State.CON_TURN),
            (_at_score_con, State.SCORE_CON),
            (_at_next_round, State.NEXT_ROUND),
            (_at_closing, State.CLOSING),
            (_at_verdict, State.VERDICT),
            (_at_validate_verdict, State.VALIDATE_VERDICT),
            (_at_tie_break, State.TIE_BREAK),
            (_at_recover, State.RECOVER),
        ],
    )
    def test_budget_exhausted_aborts_from(
        self,
        driver: Callable[[DebateStateMachine], None],
        expected_state_before: State,
    ) -> None:
        sm = DebateStateMachine(max_rounds=1)
        driver(sm)
        assert sm.state is expected_state_before
        assert not sm.is_terminal()

        result = sm.transition(Event.BUDGET_EXHAUSTED)
        assert result is State.ABORT
        assert sm.state is State.ABORT
        assert sm.is_terminal()

    def test_budget_exhausted_after_done_is_illegal(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        _finish_with_valid_verdict(sm)
        assert sm.state is State.DONE
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.BUDGET_EXHAUSTED)
        assert sm.state is State.DONE


# ---------------------------------------------------------------------------
# Spawn failure
# ---------------------------------------------------------------------------


class TestSpawnFailure:
    def test_spawn_failure_from_spawning_aborts(self) -> None:
        sm = DebateStateMachine()
        sm.transition(Event.START)
        assert sm.state is State.SPAWNING
        sm.transition(Event.SPAWN_FAILED)
        assert sm.state is State.ABORT
        assert sm.is_terminal()

    def test_spawn_failed_outside_spawning_is_illegal(self) -> None:
        sm = DebateStateMachine()
        sm.transition(Event.START)
        sm.transition(Event.CHILDREN_READY)
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.SPAWN_FAILED)


# ---------------------------------------------------------------------------
# Verdict retry / tie-break
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_verdict_retry_then_success(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)
        sm.transition(Event.JUDGE_REPLY)
        assert sm.state is State.VALIDATE_VERDICT

        sm.transition(Event.INVALID_OR_TIE)
        assert sm.state is State.VERDICT
        assert sm.verdict_retry_count == 1

        sm.transition(Event.JUDGE_REPLY)
        assert sm.state is State.VALIDATE_VERDICT

        sm.transition(Event.VALID_NON_TIE)
        assert sm.state is State.DONE
        assert sm.is_terminal()

    def test_verdict_retry_then_tie_break(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)
        sm.transition(Event.JUDGE_REPLY)

        sm.transition(Event.INVALID_OR_TIE)
        assert sm.state is State.VERDICT
        assert sm.verdict_retry_count == 1

        sm.transition(Event.JUDGE_REPLY)
        assert sm.state is State.VALIDATE_VERDICT

        sm.transition(Event.INVALID_OR_TIE)
        assert sm.state is State.TIE_BREAK

        sm.transition(Event.JUDGE_REPLY)
        assert sm.state is State.DONE
        assert sm.is_terminal()

    def test_tie_break_requires_judge_reply(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        sm.transition(Event.CLOSINGS_RECEIVED)
        sm.transition(Event.JUDGE_REPLY)
        sm.transition(Event.INVALID_OR_TIE)
        sm.transition(Event.JUDGE_REPLY)
        sm.transition(Event.INVALID_OR_TIE)
        assert sm.state is State.TIE_BREAK

        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.VALID_NON_TIE)
        assert sm.state is State.TIE_BREAK


# ---------------------------------------------------------------------------
# Heartbeat recovery
# ---------------------------------------------------------------------------


class TestHeartbeatRecovery:
    def test_recovery_from_pro_turn(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_to_pro_turn(sm)
        assert sm.state is State.PRO_TURN

        sm.transition(Event.HEARTBEAT_MISS)
        assert sm.state is State.RECOVER
        assert sm.last_missed_role == "pro"
        assert sm.remembered_turn_state is State.PRO_TURN

        sm.transition(Event.RESPAWNED)
        assert sm.state is State.PRO_TURN
        assert sm.last_missed_role is None
        assert sm.remembered_turn_state is None

        sm.transition(Event.PRO_REPLY)
        assert sm.state is State.SCORE_PRO

    def test_recovery_from_con_turn(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_to_con_turn(sm)
        assert sm.state is State.CON_TURN

        sm.transition(Event.HEARTBEAT_MISS)
        assert sm.state is State.RECOVER
        assert sm.last_missed_role == "con"
        assert sm.remembered_turn_state is State.CON_TURN

        sm.transition(Event.RESPAWNED)
        assert sm.state is State.CON_TURN
        assert sm.last_missed_role is None
        assert sm.remembered_turn_state is None

        sm.transition(Event.CON_REPLY)
        assert sm.state is State.SCORE_CON

    def test_recovery_preserves_round_counter(self) -> None:
        sm = DebateStateMachine(max_rounds=3)
        _drive_to_pro_turn(sm)
        sm.transition(Event.PRO_REPLY)
        sm.transition(Event.SCORED)
        sm.transition(Event.CON_REPLY)
        sm.transition(Event.SCORED)
        sm.transition(Event.SCORED)
        assert sm.state is State.PRO_TURN
        assert sm.current_round == 1

        sm.transition(Event.HEARTBEAT_MISS)
        assert sm.state is State.RECOVER
        sm.transition(Event.RESPAWNED)
        assert sm.state is State.PRO_TURN
        assert sm.current_round == 1

    def test_restarts_exhausted_aborts(self) -> None:
        sm = DebateStateMachine()
        _drive_to_pro_turn(sm)
        sm.transition(Event.HEARTBEAT_MISS)
        assert sm.state is State.RECOVER

        sm.transition(Event.RESTARTS_EXHAUSTED)
        assert sm.state is State.ABORT
        assert sm.is_terminal()

    def test_heartbeat_miss_outside_turns_is_illegal(self) -> None:
        sm = DebateStateMachine()
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.HEARTBEAT_MISS)

        sm.transition(Event.START)
        with pytest.raises(IllegalTransitionError):
            sm.transition(Event.HEARTBEAT_MISS)


# ---------------------------------------------------------------------------
# is_terminal
# ---------------------------------------------------------------------------


class TestIsTerminal:
    def test_init_not_terminal(self) -> None:
        assert DebateStateMachine().is_terminal() is False

    def test_done_is_terminal(self) -> None:
        sm = DebateStateMachine(max_rounds=1)
        _drive_through_rounds(sm, 1)
        _finish_with_valid_verdict(sm)
        assert sm.state is State.DONE
        assert sm.is_terminal() is True

    def test_abort_is_terminal(self) -> None:
        sm = DebateStateMachine()
        sm.transition(Event.START)
        sm.transition(Event.SPAWN_FAILED)
        assert sm.state is State.ABORT
        assert sm.is_terminal() is True

    @pytest.mark.parametrize(
        "non_terminal",
        [
            State.INIT,
            State.SPAWNING,
            State.OPENING,
            State.PRO_TURN,
            State.SCORE_PRO,
            State.CON_TURN,
            State.SCORE_CON,
            State.NEXT_ROUND,
            State.CLOSING,
            State.VERDICT,
            State.VALIDATE_VERDICT,
            State.TIE_BREAK,
            State.RECOVER,
        ],
    )
    def test_non_terminal_states_report_false(self, non_terminal: State) -> None:
        sm = DebateStateMachine()
        sm.state = non_terminal
        assert sm.is_terminal() is False


# ---------------------------------------------------------------------------
# Purity (Stage 5 must not pull in side-effecting modules)
# ---------------------------------------------------------------------------


class TestPurity:
    def test_module_imports_have_no_forbidden_targets(self) -> None:
        path = Path(sm_module.__file__)
        text = path.read_text(encoding="utf-8")
        import_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        forbidden_substrings = [
            "llm_client",
            "search_client",
            "supervisor",
            "watchdog",
            ".agents",
            "gatekeeper",
            "router",
            "ledger",
            "ipc",
            "subprocess",
            "asyncio",
            "openai",
            "anthropic",
            "httpx",
            "requests",
            "urllib",
            "socket",
        ]
        for line in import_lines:
            lower = line.lower()
            for tok in forbidden_substrings:
                assert tok not in lower, f"forbidden import token {tok!r} found in {line!r}"

    def test_module_does_not_write_files_or_call_subprocess(self) -> None:
        path = Path(sm_module.__file__)
        text = path.read_text(encoding="utf-8")
        for bad in [
            "open(",
            "Path(",
            "subprocess.",
            "os.system",
            "os.popen",
            "tempfile.",
        ]:
            assert bad not in text, f"forbidden call site {bad!r} present"
