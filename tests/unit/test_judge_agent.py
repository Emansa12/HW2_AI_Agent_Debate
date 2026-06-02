"""Unit tests for :mod:`debate.orchestration.judge`.

These tests drive the :class:`Judge` with a fully in-memory
``FakeSupervisor`` test double, a ``FakeLLMClient`` configured to
emit JSON-shaped verdicts, and a real :class:`ToolRouter` wrapped
around the offline :class:`FakeSearchClient`. No subprocesses, no
threads, no network.
"""

from __future__ import annotations

import ast
import inspect
from collections import deque
from typing import Any

import pytest

from debate.orchestration import judge as judge_module
from debate.orchestration.judge import (
    DEFAULT_RECEIVE_MAX_ITERS,
    MIN_VERDICT_REASONS,
    DebateHistory,
    InvalidReplyError,
    InvalidVerdictError,
    Judge,
)
from debate.orchestration.state_machine import DebateStateMachine, Event, State
from debate.orchestration.supervisor import (
    ChildReceiveTimeoutError,
    ChildStreamClosedError,
    SupervisorError,
)
from debate.sdk.llm_client import FakeLLMClient
from debate.sdk.schemas import (
    SCHEMA_VERSION,
    Message,
    MessageType,
    Phase,
    Role,
    Verdict,
)
from debate.sdk.search_client import FakeSearchClient
from debate.shared.gatekeeper import Gatekeeper, GatekeeperPolicy
from debate.shared.router import ToolRouter

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeChild:
    """Mimics the bit of ChildProcess the Judge / supervisor reads."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class FakeSupervisor:
    """In-memory Supervisor: records every send and serves queued receives.

    Tests configure responses per role via ``queue_receive``. ``send``
    stores the (role, Message) pair. ``spawn`` registers a FakeChild
    so :meth:`child` returns a live one. ``terminate`` removes it.
    """

    def __init__(self) -> None:
        self._children: dict[str, FakeChild] = {}
        self._receive_queue: dict[str, deque[Any]] = {"pro": deque(), "con": deque()}
        self.sent: list[tuple[str, Message]] = []
        self.spawn_calls: list[str] = []
        self.terminated: list[str] = []
        self.terminate_all_calls: int = 0
        self.send_exceptions: dict[str, deque[BaseException]] = {
            "pro": deque(),
            "con": deque(),
        }

    # --- supervisor surface used by Judge ---

    def spawn(self, role: str) -> FakeChild:
        self.spawn_calls.append(role)
        child = FakeChild(alive=True)
        self._children[role] = child
        return child

    def child(self, role: str) -> FakeChild | None:
        return self._children.get(role)

    def send(self, role: str, message: Message) -> None:
        self.sent.append((role, message))
        if self.send_exceptions[role]:
            raise self.send_exceptions[role].popleft()

    def receive(self, role: str, timeout: float | None = None) -> Message:
        del timeout
        if not self._receive_queue[role]:
            raise ChildReceiveTimeoutError(role, 0.0)
        item = self._receive_queue[role].popleft()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            if issubclass(item, ChildReceiveTimeoutError):
                raise item(role, 0.0)
            if issubclass(item, ChildStreamClosedError):
                raise item(role)
            raise item()
        return item

    def terminate(self, role: str) -> None:
        self.terminated.append(role)
        self._children.pop(role, None)

    def terminate_all(self) -> None:
        self.terminate_all_calls += 1
        for role in list(self._children):
            self.terminate(role)

    # --- test helpers ---

    def queue_receive(self, role: str, item: Any) -> None:
        self._receive_queue[role].append(item)

    def queue_send_exception(self, role: str, exc: BaseException) -> None:
        self.send_exceptions[role].append(exc)


class RecordingLogger:
    """Captures every ``log(role, turn_id, event_type, **fields)`` call."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, *, role: str, turn_id: int, event_type: str, **fields: Any) -> None:
        self.events.append({"role": role, "turn_id": turn_id, "event_type": event_type, **fields})

    def event_types(self) -> list[str]:
        return [e["event_type"] for e in self.events]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generous_policy() -> GatekeeperPolicy:
    return GatekeeperPolicy(
        max_tokens_per_turn=2000,
        max_tokens_per_debate=10_000_000,
        max_usd_per_debate=100.0,
        max_requests_per_minute=10_000,
    )


def _reply(role: Role, content: str, *, turn_id: int = 0, stance: str | None = None) -> Message:
    payload: dict[str, Any] = {
        "phase": Phase.ARGUMENT.value,
        "stance": stance if stance is not None else role.value,
        "content": content,
        "tokens_in": 10,
        "tokens_out": 12,
    }
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=turn_id,
        role=role,
        type=MessageType.REPLY,
        payload=payload,
    )


def _tool_call(role: Role, query: str, *, turn_id: int = 0) -> Message:
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=turn_id,
        role=role,
        type=MessageType.TOOL_CALL,
        payload={"tool": "search", "query": query},
    )


def _verdict_json(
    *,
    winner: str = "pro",
    pro: int = 12,
    con: int = 8,
    reasons: tuple[str, ...] = ("a reason", "another reason", "third reason"),
    rationale: str = "summary",
) -> str:
    parts = ['{"winner":"' + winner + '"']
    parts.append(f',"scores":{{"pro":{pro},"con":{con}}}')
    reason_strs = ",".join(f'"{r}"' for r in reasons)
    parts.append(f',"reasons":[{reason_strs}]')
    parts.append(f',"rationale":"{rationale}"')
    parts.append("}")
    return "".join(parts)


def _make_judge(
    *,
    supervisor: FakeSupervisor,
    llm_text: str = _verdict_json(),
    rounds: int = 1,
    logger: Any = None,
    receive_max_iters: int = DEFAULT_RECEIVE_MAX_ITERS,
) -> tuple[Judge, RecordingLogger | None, ToolRouter]:
    fsm = DebateStateMachine(max_rounds=rounds)
    gk = Gatekeeper(_generous_policy())
    router = ToolRouter(gatekeeper=gk, search_client=FakeSearchClient(results_per_query=1))
    llm = FakeLLMClient(response_text=llm_text)
    judge = Judge(
        supervisor=supervisor,
        fsm=fsm,
        router=router,
        gatekeeper=gk,
        llm_client=llm,
        logger=logger,
        per_turn_timeout_sec=1.0,
        receive_max_iters=receive_max_iters,
        clock=lambda: 100.0,
    )
    return judge, logger, router


def _queue_full_debate(
    sup: FakeSupervisor,
    rounds: int = 1,
    *,
    pro_content: str = "P",
    con_content: str = "C",
) -> None:
    """Queue replies for opening + N rounds + closing for a clean run."""
    sup.queue_receive("pro", _reply(Role.PRO, pro_content + " open"))
    sup.queue_receive("con", _reply(Role.CON, con_content + " open"))
    for r in range(1, rounds + 1):
        sup.queue_receive("pro", _reply(Role.PRO, f"{pro_content} arg {r}"))
        sup.queue_receive("con", _reply(Role.CON, f"{con_content} arg {r}"))
    sup.queue_receive("pro", _reply(Role.PRO, pro_content + " close"))
    sup.queue_receive("con", _reply(Role.CON, con_content + " close"))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_non_positive_timeout(self) -> None:
        sup = FakeSupervisor()
        gk = Gatekeeper(_generous_policy())
        router = ToolRouter(gatekeeper=gk, search_client=FakeSearchClient())
        with pytest.raises(ValueError):
            Judge(
                supervisor=sup,
                fsm=DebateStateMachine(),
                router=router,
                gatekeeper=gk,
                llm_client=FakeLLMClient(),
                per_turn_timeout_sec=0.0,
            )

    def test_rejects_zero_iter_budget(self) -> None:
        sup = FakeSupervisor()
        gk = Gatekeeper(_generous_policy())
        router = ToolRouter(gatekeeper=gk, search_client=FakeSearchClient())
        with pytest.raises(ValueError):
            Judge(
                supervisor=sup,
                fsm=DebateStateMachine(),
                router=router,
                gatekeeper=gk,
                llm_client=FakeLLMClient(),
                receive_max_iters=0,
            )

    def test_history_starts_zero(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        assert judge.cumulative_scores == {"pro": 0, "con": 0}
        assert isinstance(judge.history, DebateHistory)
        assert judge.history.turns == []


# ---------------------------------------------------------------------------
# build_init / build_prompt
# ---------------------------------------------------------------------------


class TestBuildInit:
    def test_init_carries_stance_and_motion(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        msg = judge.build_init("pro", "AI > humans")
        assert msg.role is Role.JUDGE
        assert msg.type is MessageType.INIT
        assert msg.payload["stance"] == "pro"
        assert msg.payload["motion"] == "AI > humans"
        assert msg.payload["max_tokens"] >= 1

    def test_init_rejects_unknown_role(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(ValueError):
            judge.build_init("judge", "topic")


class TestBuildPrompt:
    def test_prompt_envelope_is_judge_role_prompt_type(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        msg = judge.build_prompt("pro", Phase.ARGUMENT, opponent_last="hi")
        assert msg.role is Role.JUDGE
        assert msg.type is MessageType.PROMPT
        assert msg.payload["phase"] == Phase.ARGUMENT.value
        assert msg.payload["opponent_last"] == "hi"

    def test_prompt_rejects_invalid_role(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(ValueError):
            judge.build_prompt("judge", Phase.ARGUMENT)

    def test_prompt_rejects_message_as_opponent_last(self) -> None:
        """opponent_last must be a *string*; passing a Message is forbidden
        because that would amount to forwarding the other side's envelope.
        """
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(TypeError):
            judge.build_prompt(
                "pro",
                Phase.ARGUMENT,
                opponent_last=_reply(Role.CON, "hello"),  # type: ignore[arg-type]
            )

    def test_prompt_omits_opponent_last_when_none(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        msg = judge.build_prompt("pro", Phase.OPENING, opponent_last=None)
        assert "opponent_last" not in msg.payload


# ---------------------------------------------------------------------------
# validate_child_reply
# ---------------------------------------------------------------------------


class TestValidateReply:
    def test_accepts_valid_reply(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        msg = _reply(Role.PRO, "hello world")
        assert judge.validate_child_reply(msg, expected_role="pro") is msg

    def test_rejects_wrong_sender(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        msg = _reply(Role.CON, "hi", stance="con")
        with pytest.raises(InvalidReplyError, match="sender role mismatch"):
            judge.validate_child_reply(msg, expected_role="pro")

    def test_rejects_wrong_message_type(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        wrong = Message(
            v=SCHEMA_VERSION,
            ts=1.0,
            turn_id=0,
            role=Role.PRO,
            type=MessageType.TOOL_CALL,
            payload={"tool": "search", "query": "x"},
        )
        with pytest.raises(InvalidReplyError, match="message type"):
            judge.validate_child_reply(wrong, expected_role="pro")

    def test_rejects_empty_content(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        empty = _reply(Role.PRO, "   ")
        with pytest.raises(InvalidReplyError, match="empty content"):
            judge.validate_child_reply(empty, expected_role="pro")

    def test_rejects_stance_mismatch(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        crossed = _reply(Role.PRO, "ok", stance="con")
        with pytest.raises(InvalidReplyError, match="stance"):
            judge.validate_child_reply(crossed, expected_role="pro")

    def test_rejects_invalid_expected_role(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(InvalidReplyError, match="expected_role"):
            judge.validate_child_reply(_reply(Role.PRO, "hi"), expected_role="judge")


# ---------------------------------------------------------------------------
# score_turn
# ---------------------------------------------------------------------------


class TestScoring:
    def test_score_is_deterministic(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        s1 = judge.score_turn("pro", _reply(Role.PRO, "hello world"), 1)
        s2 = judge.score_turn("pro", _reply(Role.PRO, "hello world"), 1)
        assert s1 == s2
        assert s1["score"] >= 1

    def test_longer_content_scores_more(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        short = judge.score_turn("pro", _reply(Role.PRO, "x"), 1)
        long = judge.score_turn("pro", _reply(Role.PRO, "x" * 500), 1)
        assert long["score"] > short["score"]

    def test_score_capped(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        huge = judge.score_turn("pro", _reply(Role.PRO, "x" * 100_000), 1)
        assert huge["score"] <= 10  # base 1 + cap 9

    def test_score_rejects_invalid_role(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(ValueError):
            judge.score_turn("judge", _reply(Role.PRO, "hi"), 1)


# ---------------------------------------------------------------------------
# generate_verdict / parse / validate
# ---------------------------------------------------------------------------


class TestVerdictParseAndValidate:
    def test_valid_verdict_passes(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup, llm_text=_verdict_json())
        v = judge.generate_verdict()
        judge.validate_verdict(v)
        assert v.winner == "pro"

    def test_tie_winner_rejected_at_schema(self) -> None:
        """Schema forbids ``winner="tie"``; this must fail at parse time
        and surface as InvalidVerdictError so the retry path engages."""
        sup = FakeSupervisor()
        bad = _verdict_json(winner="tie")
        judge, _, _ = _make_judge(supervisor=sup, llm_text=bad)
        with pytest.raises(InvalidVerdictError):
            judge.generate_verdict()

    def test_missing_scores_rejected(self) -> None:
        sup = FakeSupervisor()
        text = '{"winner":"pro","reasons":["a","b","c"]}'
        judge, _, _ = _make_judge(supervisor=sup, llm_text=text)
        v = judge.generate_verdict()
        with pytest.raises(InvalidVerdictError, match="scores"):
            judge.validate_verdict(v)

    def test_missing_reasons_rejected(self) -> None:
        sup = FakeSupervisor()
        text = '{"winner":"con","scores":{"pro":1,"con":2}}'
        judge, _, _ = _make_judge(supervisor=sup, llm_text=text)
        v = judge.generate_verdict()
        with pytest.raises(InvalidVerdictError, match=str(MIN_VERDICT_REASONS)):
            judge.validate_verdict(v)

    def test_too_few_reasons_rejected(self) -> None:
        sup = FakeSupervisor()
        text = '{"winner":"con","scores":{"pro":1,"con":2},"reasons":["only one"]}'
        judge, _, _ = _make_judge(supervisor=sup, llm_text=text)
        v = judge.generate_verdict()
        with pytest.raises(InvalidVerdictError):
            judge.validate_verdict(v)

    def test_garbage_text_rejected(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup, llm_text="totally not json")
        with pytest.raises(InvalidVerdictError):
            judge.generate_verdict()

    def test_extracts_json_from_code_fence(self) -> None:
        sup = FakeSupervisor()
        wrapped = "```json\n" + _verdict_json() + "\n```"
        judge, _, _ = _make_judge(supervisor=sup, llm_text=wrapped)
        v = judge.generate_verdict()
        judge.validate_verdict(v)
        assert v.winner == "pro"

    def test_extracts_json_from_prose(self) -> None:
        sup = FakeSupervisor()
        wrapped = "Here is my verdict: " + _verdict_json() + " That's all."
        judge, _, _ = _make_judge(supervisor=sup, llm_text=wrapped)
        v = judge.generate_verdict()
        judge.validate_verdict(v)
        assert v.winner == "pro"

    def test_validate_verdict_requires_pydantic_instance(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(InvalidVerdictError):
            judge.validate_verdict({"winner": "pro"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# apply_tie_breaker
# ---------------------------------------------------------------------------


class TestTieBreaker:
    def test_higher_pro_wins(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        v = judge.apply_tie_breaker({"pro": 5, "con": 3})
        assert v.winner == "pro"
        assert v.model_extra is not None
        assert v.model_extra["scores"] == {"pro": 5, "con": 3}
        assert len(v.model_extra["reasons"]) >= MIN_VERDICT_REASONS

    def test_higher_con_wins(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        v = judge.apply_tie_breaker({"pro": 2, "con": 9})
        assert v.winner == "con"

    def test_exact_tie_chooses_con(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        v = judge.apply_tie_breaker({"pro": 4, "con": 4})
        assert v.winner == "con"
        assert v.model_extra is not None
        assert v.model_extra["scores"] == {"pro": 4, "con": 5}
        assert "deterministic" in (v.rationale or "").lower()

    def test_tie_breaker_never_leaves_equal_scores(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        for pro, con in ((4, 4), (10, 10), (0, 0)):
            v = judge.apply_tie_breaker({"pro": pro, "con": con})
            scores = v.model_extra["scores"]
            assert scores["pro"] != scores["con"]
            assert v.winner in ("pro", "con")

    def test_tie_breaker_passes_validation(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        v = judge.apply_tie_breaker({"pro": 1, "con": 2})
        judge.validate_verdict(v)


# ---------------------------------------------------------------------------
# run_turn (single turn with prompt -> reply)
# ---------------------------------------------------------------------------


class TestRunTurn:
    def test_sends_prompt_and_returns_reply(self) -> None:
        sup = FakeSupervisor()
        sup.queue_receive("pro", _reply(Role.PRO, "answer"))
        judge, _, _ = _make_judge(supervisor=sup)
        out = judge.run_turn("pro", Phase.OPENING, opponent_last=None)
        assert out.payload["content"] == "answer"
        # Exactly one prompt was sent to pro.
        sent_to_pro = [m for r, m in sup.sent if r == "pro"]
        assert len(sent_to_pro) == 1
        assert sent_to_pro[0].type is MessageType.PROMPT
        assert sent_to_pro[0].role is Role.JUDGE

    def test_handles_tool_call_and_routes_through_router(self) -> None:
        sup = FakeSupervisor()
        sup.queue_receive("pro", _tool_call(Role.PRO, "ai ethics"))
        sup.queue_receive("pro", _reply(Role.PRO, "ok"))
        judge, _, router = _make_judge(supervisor=sup)
        out = judge.run_turn("pro", Phase.ARGUMENT, opponent_last="x")
        assert out.payload["content"] == "ok"

        # Sequence sent to pro: prompt -> tool_result -> (no more)
        types = [m.type for r, m in sup.sent if r == "pro"]
        assert types == [MessageType.PROMPT, MessageType.TOOL_RESULT]

        tool_result_msg = [m for r, m in sup.sent if r == "pro"][1]
        assert tool_result_msg.role is Role.JUDGE
        assert tool_result_msg.payload["tool"] == "search"
        assert isinstance(tool_result_msg.payload["results"], list)
        # Router was actually used (has at least one cache miss).
        assert router.cache_stats["misses"] >= 1

    def test_unknown_tool_responds_with_error_payload(self) -> None:
        sup = FakeSupervisor()
        unknown = Message(
            v=SCHEMA_VERSION,
            ts=1.0,
            turn_id=1,
            role=Role.PRO,
            type=MessageType.TOOL_CALL,
            payload={"tool": "calculator", "expr": "1+1"},
        )
        sup.queue_receive("pro", unknown)
        sup.queue_receive("pro", _reply(Role.PRO, "still got reply"))
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_turn("pro", Phase.ARGUMENT, opponent_last="x")
        result_msgs = [m for r, m in sup.sent if r == "pro" and m.type is MessageType.TOOL_RESULT]
        assert len(result_msgs) == 1
        assert result_msgs[0].payload["error"] == "unknown_tool"
        assert result_msgs[0].payload["tool"] == "calculator"

    def test_timeout_propagates_and_logs(self) -> None:
        sup = FakeSupervisor()
        # No message queued -> ChildReceiveTimeoutError
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, logger=logger)
        with pytest.raises(ChildReceiveTimeoutError):
            judge.run_turn("pro", Phase.OPENING, opponent_last=None)
        assert "turn_failed" in logger.event_types()

    def test_iter_budget_exhausted(self) -> None:
        sup = FakeSupervisor()
        # Queue 2 tool_calls but the receive_max_iters=2 means we only
        # service tool calls; never get a reply -> InvalidReplyError.
        for _ in range(3):
            sup.queue_receive("pro", _tool_call(Role.PRO, "q"))
        judge, _, _ = _make_judge(supervisor=sup, receive_max_iters=2)
        with pytest.raises(InvalidReplyError, match="too many tool_call"):
            judge.run_turn("pro", Phase.ARGUMENT, opponent_last="x")

    def test_unexpected_message_type_rejected(self) -> None:
        sup = FakeSupervisor()
        score_msg = Message(
            v=SCHEMA_VERSION,
            ts=1.0,
            turn_id=1,
            role=Role.PRO,
            type=MessageType.SCORE,
            payload={"value": 1},
        )
        sup.queue_receive("pro", score_msg)
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(InvalidReplyError, match="unexpected message type"):
            judge.run_turn("pro", Phase.ARGUMENT, opponent_last="x")


# ---------------------------------------------------------------------------
# run_debate end-to-end
# ---------------------------------------------------------------------------


class TestRunDebate:
    def test_one_round_happy_path(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, llm_text=_verdict_json(), logger=logger)

        verdict = judge.run_debate(motion="AI is good", rounds=1)

        assert isinstance(verdict, Verdict)
        assert verdict.winner in ("pro", "con")
        assert judge.fsm.state is State.DONE

    def test_init_sent_to_both_pro_and_con(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=1)
        init_targets = [r for r, m in sup.sent if m.type is MessageType.INIT]
        assert init_targets == ["pro", "con"]

    def test_alternates_pro_con_for_each_round(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=2)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=2)
        # Look at the sequence of PROMPT recipients only.
        prompt_targets = [r for r, m in sup.sent if m.type is MessageType.PROMPT]
        # opening: pro, con; round1: pro, con; round2: pro, con; closing: pro, con
        assert prompt_targets == ["pro", "con"] * 4
        # Same side never speaks twice in a row across argument rounds:
        assert all(
            prompt_targets[i] != prompt_targets[i + 1] for i in range(len(prompt_targets) - 1)
        )

    def test_pro_never_receives_con_envelope(self) -> None:
        """Every Message ever sent to either child must originate from
        Role.JUDGE. Pro/Con MUST NOT see each other's envelopes - they
        only see the *content string* via the prompt's
        ``opponent_last`` field."""
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=1)
        for role_target, msg in sup.sent:
            assert msg.role is Role.JUDGE, (
                f"Message of type {msg.type.value} sent to {role_target} "
                f"carried non-Judge role {msg.role.value}"
            )

    def test_opponent_last_is_string_only(self) -> None:
        """Sanity check: the opponent's content string is forwarded
        but the original Message envelope is not."""
        sup = FakeSupervisor()
        sup.queue_receive("pro", _reply(Role.PRO, "PRO_OPENING"))
        sup.queue_receive("con", _reply(Role.CON, "CON_OPENING"))
        sup.queue_receive("pro", _reply(Role.PRO, "PRO_ARG_1"))
        sup.queue_receive("con", _reply(Role.CON, "CON_ARG_1"))
        sup.queue_receive("pro", _reply(Role.PRO, "PRO_CLOSE"))
        sup.queue_receive("con", _reply(Role.CON, "CON_CLOSE"))
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=1)
        prompts_to_con = [m for r, m in sup.sent if r == "con" and m.type is MessageType.PROMPT]
        # Con sees Pro's content string in opponent_last.
        for p in prompts_to_con:
            ol = p.payload.get("opponent_last")
            assert ol is None or isinstance(ol, str)

    def test_score_accumulation_grows_monotonically(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=2, pro_content="PROCONTENT" * 10, con_content="ConC" * 5)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=2)
        scores = judge.cumulative_scores
        assert scores["pro"] >= 1
        assert scores["con"] >= 1
        # All recorded turns had non-zero scores.
        assert all(t.score >= 1 for t in judge.history.turns)

    def test_logs_capture_full_lifecycle(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, logger=logger)
        judge.run_debate(motion="m", rounds=1)
        types = set(logger.event_types())
        for required in (
            "debate_started",
            "children_spawned",
            "init_sent",
            "prompt_sent",
            "reply_received",
            "score_recorded",
            "verdict_recorded",
            "debate_done",
        ):
            assert required in types, f"missing log event {required!r}; got {sorted(types)}"

    def test_logs_tool_call_and_result(self) -> None:
        sup = FakeSupervisor()
        sup.queue_receive("pro", _reply(Role.PRO, "P open"))
        sup.queue_receive("con", _reply(Role.CON, "C open"))
        sup.queue_receive("pro", _tool_call(Role.PRO, "evidence"))
        sup.queue_receive("pro", _reply(Role.PRO, "P arg"))
        sup.queue_receive("con", _reply(Role.CON, "C arg"))
        sup.queue_receive("pro", _reply(Role.PRO, "P close"))
        sup.queue_receive("con", _reply(Role.CON, "C close"))
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, logger=logger)
        judge.run_debate(motion="m", rounds=1)
        types = logger.event_types()
        assert "tool_call_received" in types
        assert "tool_result_sent" in types

    def test_logs_include_readable_prompt_and_reply_content(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1, pro_content="PRO_TEXT", con_content="CON_TEXT")
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, logger=logger)
        judge.run_debate(motion="motion topic", rounds=1)

        prompts = [e for e in logger.events if e["event_type"] == "prompt_sent"]
        replies = [e for e in logger.events if e["event_type"] == "reply_received"]
        assert prompts and replies
        assert isinstance(prompts[0]["prompt_text"], str)
        assert "phase" in prompts[0]["prompt_payload"]
        assert "PRO_TEXT" in replies[0]["content"]
        assert replies[0]["content_length"] == len(replies[0]["content"])

        verdict = [e for e in logger.events if e["event_type"] == "verdict_recorded"][-1]
        assert verdict["winner"] in ("pro", "con")
        assert isinstance(verdict.get("reasons"), list) and len(verdict["reasons"]) >= 3
        assert isinstance(verdict.get("verdict_text"), str)

    def test_logs_redact_sensitive_tool_payload_keys(self) -> None:
        sup = FakeSupervisor()
        sup.queue_receive("pro", _reply(Role.PRO, "P open"))
        sup.queue_receive("con", _reply(Role.CON, "C open"))
        sup.queue_receive(
            "pro",
            Message(
                v=SCHEMA_VERSION,
                ts=1.0,
                turn_id=0,
                role=Role.PRO,
                type=MessageType.TOOL_CALL,
                payload={"tool": "search", "query": "x", "client_api_key": "sk-leak-test"},
            ),
        )
        sup.queue_receive("pro", _reply(Role.PRO, "P arg"))
        sup.queue_receive("con", _reply(Role.CON, "C arg"))
        sup.queue_receive("pro", _reply(Role.PRO, "P close"))
        sup.queue_receive("con", _reply(Role.CON, "C close"))
        logger = RecordingLogger()
        judge, _, _ = _make_judge(supervisor=sup, logger=logger)
        judge.run_debate(motion="m", rounds=1)
        tool_calls = [e for e in logger.events if e["event_type"] == "tool_call_received"]
        assert tool_calls
        payload = tool_calls[0]["tool_call_payload"]
        assert payload["client_api_key"] == "<redacted>"
        assert "sk-leak-test" not in str(tool_calls)

    def test_terminate_all_on_success(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=1)
        assert sup.terminate_all_calls >= 1

    def test_terminate_all_on_failure(self) -> None:
        sup = FakeSupervisor()
        # Queue only one reply then nothing -> turn fails -> finally cleans up
        sup.queue_receive("pro", _reply(Role.PRO, "open"))
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises((ChildReceiveTimeoutError, ChildStreamClosedError, SupervisorError)):
            judge.run_debate(motion="m", rounds=1)
        assert sup.terminate_all_calls >= 1

    def test_invalid_motion_rejected(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(ValueError):
            judge.run_debate(motion="   ", rounds=1)

    def test_invalid_rounds_rejected(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        with pytest.raises(ValueError):
            judge.run_debate(motion="m", rounds=0)


# ---------------------------------------------------------------------------
# Verdict retry / tie-break path
# ---------------------------------------------------------------------------


class _ScriptedLLMClient:
    """LLM client that returns each script entry in order."""

    def __init__(self, *, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self._calls: list[str] = []

    def complete(self, *, prompt: str, max_tokens: int) -> Any:  # pragma: no cover - trivial
        from debate.sdk.llm_client import LLMResponse

        self._calls.append(prompt)
        text = self._scripts.pop(0) if self._scripts else "(empty)"
        return LLMResponse(text=text, tokens_in=10, tokens_out=10, usd=0.001)


def _make_judge_with_scripted_llm(
    *,
    supervisor: FakeSupervisor,
    scripts: list[str],
    rounds: int = 1,
    logger: Any = None,
) -> Judge:
    fsm = DebateStateMachine(max_rounds=rounds)
    gk = Gatekeeper(_generous_policy())
    router = ToolRouter(gatekeeper=gk, search_client=FakeSearchClient(results_per_query=1))
    return Judge(
        supervisor=supervisor,
        fsm=fsm,
        router=router,
        gatekeeper=gk,
        llm_client=_ScriptedLLMClient(scripts=scripts),
        logger=logger,
        per_turn_timeout_sec=1.0,
        clock=lambda: 100.0,
    )


class TestVerdictRetryPath:
    def test_first_invalid_then_valid_retries_once(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        scripts = ["not json at all", _verdict_json(winner="con")]
        logger = RecordingLogger()
        judge = _make_judge_with_scripted_llm(
            supervisor=sup, scripts=scripts, rounds=1, logger=logger
        )
        verdict = judge.run_debate(motion="m", rounds=1)
        assert verdict.winner == "con"
        # Verdict invalid was logged exactly once.
        invalids = [e for e in logger.events if e["event_type"] == "verdict_invalid"]
        assert len(invalids) == 1
        assert judge.fsm.state is State.DONE

    def test_two_invalid_triggers_tie_breaker(self) -> None:
        sup = FakeSupervisor()
        # Make Pro produce longer content so cumulative pro score > con,
        # the tie-breaker should pick "pro".
        sup.queue_receive("pro", _reply(Role.PRO, "P open " + "x" * 200))
        sup.queue_receive("con", _reply(Role.CON, "C open"))
        sup.queue_receive("pro", _reply(Role.PRO, "P arg " + "x" * 200))
        sup.queue_receive("con", _reply(Role.CON, "C arg"))
        sup.queue_receive("pro", _reply(Role.PRO, "P close " + "x" * 200))
        sup.queue_receive("con", _reply(Role.CON, "C close"))

        scripts = ["garbage 1", "still garbage 2"]
        logger = RecordingLogger()
        judge = _make_judge_with_scripted_llm(
            supervisor=sup, scripts=scripts, rounds=1, logger=logger
        )
        verdict = judge.run_debate(motion="m", rounds=1)
        assert verdict.winner == "pro"
        types = [e["event_type"] for e in logger.events]
        assert types.count("verdict_invalid") == 2
        # Tie-break recorded with source=tie_break.
        tied = [e for e in logger.events if e.get("source") == "tie_break"]
        assert len(tied) == 1
        assert judge.fsm.state is State.DONE

    def test_tie_break_with_equal_cumulative_picks_con(self) -> None:
        sup = FakeSupervisor()
        # Both sides emit identical content -> identical scores -> tie -> con.
        same = "x" * 100
        sup.queue_receive("pro", _reply(Role.PRO, same))
        sup.queue_receive("con", _reply(Role.CON, same))
        sup.queue_receive("pro", _reply(Role.PRO, same))
        sup.queue_receive("con", _reply(Role.CON, same))
        sup.queue_receive("pro", _reply(Role.PRO, same))
        sup.queue_receive("con", _reply(Role.CON, same))
        scripts = ["bad json once", "bad json twice"]
        judge = _make_judge_with_scripted_llm(supervisor=sup, scripts=scripts, rounds=1)
        verdict = judge.run_debate(motion="m", rounds=1)
        assert judge.cumulative_scores["pro"] == judge.cumulative_scores["con"]
        assert verdict.winner == "con"
        assert verdict.model_extra is not None
        assert verdict.model_extra["scores"]["pro"] != verdict.model_extra["scores"]["con"]


class TestTiedVerdictScores:
    def test_llm_tied_scores_bump_winner_pro(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner="pro", pro=120, con=120),
            rounds=1,
        )
        verdict = judge.generate_verdict()
        judge.validate_verdict(verdict)
        final, applied, reason = judge._finalize_verdict(verdict)
        assert applied is True
        assert reason == "scores_equal"
        assert final.winner == "pro"
        assert final.model_extra is not None
        assert final.model_extra["scores"] == {"pro": 121, "con": 120}

    def test_llm_tied_scores_bump_winner_con(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner="con", pro=50, con=50),
        )
        verdict = judge.generate_verdict()
        final, applied, _reason = judge._finalize_verdict(verdict)
        assert applied is True
        assert final.winner == "con"
        assert final.model_extra is not None
        assert final.model_extra["scores"] == {"pro": 50, "con": 51}

    def test_unequal_llm_scores_unchanged(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner="pro", pro=55, con=40),
        )
        verdict = judge.generate_verdict()
        final, applied, reason = judge._finalize_verdict(verdict)
        assert applied is False
        assert reason is None
        assert final.model_extra is not None
        assert final.model_extra["scores"] == {"pro": 55, "con": 40}

    def test_run_debate_logs_tiebreak_for_tied_llm_scores(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        logger = RecordingLogger()
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner="pro", pro=120, con=120),
            rounds=1,
            logger=logger,
        )
        verdict = judge.run_debate(motion="m", rounds=1)
        assert verdict.model_extra is not None
        assert verdict.model_extra["scores"] == {"pro": 121, "con": 120}
        recorded = [e for e in logger.events if e["event_type"] == "verdict_recorded"][-1]
        assert recorded["verdict_tiebreak_applied"] is True
        assert recorded["tiebreak_reason"] == "scores_equal"
        assert recorded["scores"]["pro"] != recorded["scores"]["con"]


class TestWinnerNotHardcoded:
    """The Judge must not always pick the same side.

    Valid mocked LLM verdicts preserve ``winner``; tie-break paths
    follow cumulative scores (Con only on exact cumulative ties).
    """

    @pytest.mark.parametrize("winner", ("pro", "con"))
    def test_mocked_valid_verdict_preserves_llm_winner(self, winner: str) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        pro_score, con_score = (70, 55) if winner == "pro" else (45, 72)
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner=winner, pro=pro_score, con=con_score),
            rounds=1,
        )
        verdict = judge.run_debate(motion="m", rounds=1)
        assert verdict.winner == winner

    @pytest.mark.parametrize("winner", ("pro", "con"))
    def test_tied_llm_scores_follow_llm_winner_not_fixed_side(self, winner: str) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner=winner, pro=100, con=100),
        )
        verdict = judge.generate_verdict()
        final, applied, reason = judge._finalize_verdict(verdict)
        assert applied is True
        assert reason == "scores_equal"
        assert final.winner == winner

    def test_cumulative_tiebreak_follows_higher_side(self) -> None:
        sup = FakeSupervisor()
        judge, _, _ = _make_judge(supervisor=sup)
        assert judge.apply_tie_breaker({"pro": 3, "con": 7}).winner == "con"
        assert judge.apply_tie_breaker({"pro": 9, "con": 2}).winner == "pro"

    def test_verdict_recorded_fields_include_winner_scores_reasons(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        logger = RecordingLogger()
        judge, _, _ = _make_judge(
            supervisor=sup,
            llm_text=_verdict_json(winner="con", pro=40, con=60),
            rounds=1,
            logger=logger,
        )
        judge.run_debate(motion="m", rounds=1)
        recorded = [e for e in logger.events if e["event_type"] == "verdict_recorded"][-1]
        assert recorded["winner"] == "con"
        assert recorded["winner"] != "tie"
        assert isinstance(recorded.get("scores"), dict)
        assert recorded["scores"]["pro"] != recorded["scores"]["con"]
        assert isinstance(recorded.get("reasons"), list) and len(recorded["reasons"]) >= 3
        assert isinstance(recorded.get("rationale"), str)


# ---------------------------------------------------------------------------
# Stage boundary
# ---------------------------------------------------------------------------


def _imported_module_names(module: Any) -> set[str]:
    """Return the dotted module names imported by ``module``.

    Uses :mod:`ast` so docstrings that *mention* a forbidden module
    (to explicitly say "we don't use it") don't false-positive.
    """
    src = inspect.getsource(module)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestStageBoundary:
    def test_judge_does_not_import_agent_modules(self) -> None:
        imported = _imported_module_names(judge_module)
        for forbidden in (
            "debate.agents",
            "debate.agents.base_agent",
            "debate.agents.debater_agent",
            "debate.agents.pro_agent",
            "debate.agents.con_agent",
        ):
            assert not any(
                name == forbidden or name.startswith(forbidden + ".") for name in imported
            ), f"judge.py must not import {forbidden!r}; child agents run as subprocesses"

    def test_judge_does_not_import_subprocess_or_io(self) -> None:
        imported = _imported_module_names(judge_module)
        for forbidden in ("subprocess", "socket", "httpx", "requests", "urllib", "urllib.request"):
            assert forbidden not in imported, (
                f"judge.py must not import {forbidden!r}; the Supervisor owns child IO"
            )

    def test_judge_does_not_serialize_json_directly(self) -> None:
        """The Judge is allowed to *parse* LLM verdict JSON via
        ``json.loads`` because the LLM response text is opaque to the
        IPC layer. But it must NEVER ``json.dumps`` an outgoing
        envelope - all wire I/O goes through the Supervisor and IPC
        helpers."""
        src = inspect.getsource(judge_module)
        assert "json.dumps" not in src

    def test_judge_does_not_touch_raw_stdio_in_code(self) -> None:
        """The Judge talks to children via supervisor.send/receive only.

        We tokenize the source via :mod:`ast` so the docstring's
        explanation of *why* we don't touch sys.stdin/stdout doesn't
        false-positive."""
        src = inspect.getsource(judge_module)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                base = node.value
                if isinstance(base, ast.Name) and base.id == "sys":
                    assert node.attr not in ("stdin", "stdout", "stderr"), (
                        "judge.py must not touch sys.std* directly"
                    )
                if isinstance(base, ast.Name) and base.id == "subprocess":
                    raise AssertionError("judge.py must not touch the subprocess module directly")


# ---------------------------------------------------------------------------
# Misc / clean-up
# ---------------------------------------------------------------------------


class TestSupervisorCleanup:
    def test_shutdown_attempted_on_both_children(self) -> None:
        sup = FakeSupervisor()
        _queue_full_debate(sup, rounds=1)
        judge, _, _ = _make_judge(supervisor=sup)
        judge.run_debate(motion="m", rounds=1)
        shutdown_targets = [r for r, m in sup.sent if m.type is MessageType.SHUTDOWN]
        assert set(shutdown_targets) == {"pro", "con"}
