"""Lightweight offline integration test for the Stage 9 Judge flow.

This complements the unit tests in ``tests/unit/test_judge_agent.py``
by running a complete 2-round debate end-to-end using:

- a simple in-memory ``FakeSupervisor`` (no subprocesses),
- the real :class:`debate.shared.gatekeeper.Gatekeeper`,
- the real :class:`debate.shared.router.ToolRouter` over the offline
  :class:`debate.sdk.search_client.FakeSearchClient`,
- the real :class:`debate.shared.logger.RunLogger` writing JSONL to
  a per-run tmp dir,
- a tiny scripted LLM client that produces a JSON verdict.

We assert end-to-end on the produced :class:`debate.sdk.schemas.Verdict`,
the FSM final state, the cumulative scores, and the on-disk
``run.jsonl`` transcript.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from debate.orchestration.judge import Judge
from debate.orchestration.state_machine import DebateStateMachine, State
from debate.orchestration.supervisor import ChildReceiveTimeoutError
from debate.sdk.llm_client import LLMResponse
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
from debate.shared.logger import RunLogger
from debate.shared.router import ToolRouter

# ---------------------------------------------------------------------------
# In-memory test doubles
# ---------------------------------------------------------------------------


class _FakeChild:
    def __init__(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive


class _FakeSupervisor:
    def __init__(self) -> None:
        self._children: dict[str, _FakeChild] = {}
        self._inbox: dict[str, deque[Any]] = {"pro": deque(), "con": deque()}
        self.sent: list[tuple[str, Message]] = []
        self.terminate_all_calls = 0

    def spawn(self, role: str) -> _FakeChild:
        c = _FakeChild()
        self._children[role] = c
        return c

    def child(self, role: str) -> _FakeChild | None:
        return self._children.get(role)

    def send(self, role: str, message: Message) -> None:
        self.sent.append((role, message))

    def receive(self, role: str, timeout: float | None = None) -> Message:
        del timeout
        if not self._inbox[role]:
            raise ChildReceiveTimeoutError(role, 0.0)
        item = self._inbox[role].popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def terminate(self, role: str) -> None:
        self._children.pop(role, None)

    def terminate_all(self) -> None:
        self.terminate_all_calls += 1
        self._children.clear()

    def queue(self, role: str, msg: Message) -> None:
        self._inbox[role].append(msg)


class _ScriptedLLM:
    """Returns a fixed verdict JSON every time."""

    def __init__(self, *, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self.text, tokens_in=10, tokens_out=10, usd=0.001)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reply(role: Role, content: str) -> Message:
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=0,
        role=role,
        type=MessageType.REPLY,
        payload={
            "phase": Phase.ARGUMENT.value,
            "stance": role.value,
            "content": content,
            "tokens_in": 10,
            "tokens_out": 11,
        },
    )


def _tool_call(role: Role, query: str) -> Message:
    return Message(
        v=SCHEMA_VERSION,
        ts=1.0,
        turn_id=0,
        role=role,
        type=MessageType.TOOL_CALL,
        payload={"tool": "search", "query": query},
    )


def _verdict_text(*, winner: str = "pro") -> str:
    return json.dumps(
        {
            "winner": winner,
            "scores": {"pro": 12, "con": 7},
            "reasons": [
                "Pro had stronger opening framing.",
                "Con failed to rebut the empirical evidence.",
                "Pro stayed strictly in stance and used cited tool results.",
            ],
            "rationale": "Pro's argument cohesion outweighed Con's narrower critique.",
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def _build_judge(
    *,
    sup: _FakeSupervisor,
    runs_root: Path,
    rounds: int,
    llm_text: str = _verdict_text(),
) -> tuple[Judge, RunLogger]:
    fsm = DebateStateMachine(max_rounds=rounds)
    policy = GatekeeperPolicy(
        max_tokens_per_turn=2000,
        max_tokens_per_debate=10_000_000,
        max_usd_per_debate=100.0,
        max_requests_per_minute=10_000,
    )
    gk = Gatekeeper(policy)
    router = ToolRouter(gatekeeper=gk, search_client=FakeSearchClient(results_per_query=2))
    logger = RunLogger(runs_root=runs_root, run_id="judge-flow-test")
    judge = Judge(
        supervisor=sup,
        fsm=fsm,
        router=router,
        gatekeeper=gk,
        llm_client=_ScriptedLLM(text=llm_text),
        logger=logger,
        per_turn_timeout_sec=1.0,
        clock=lambda: 1234.5,
    )
    return judge, logger


class TestTwoRoundDebate:
    def test_runs_to_done_with_fake_subprocesses(self, runs_root: Path) -> None:
        sup = _FakeSupervisor()
        # Opening
        sup.queue("pro", _reply(Role.PRO, "Pro opens with a strong framing argument."))
        sup.queue("con", _reply(Role.CON, "Con counters with a critical concern."))
        # Round 1
        sup.queue("pro", _reply(Role.PRO, "Pro round 1 argument with new evidence."))
        sup.queue("con", _reply(Role.CON, "Con round 1 rebuttal pointing out flaws."))
        # Round 2 (with a tool call interleaved)
        sup.queue("pro", _tool_call(Role.PRO, "AI ethics counterexamples"))
        sup.queue("pro", _reply(Role.PRO, "Pro round 2 builds on cited tool results."))
        sup.queue("con", _reply(Role.CON, "Con round 2 brings final critique."))
        # Closing
        sup.queue("pro", _reply(Role.PRO, "Pro closing summary tying it together."))
        sup.queue("con", _reply(Role.CON, "Con closing summary asks for skepticism."))

        judge, logger = _build_judge(sup=sup, runs_root=runs_root, rounds=2)
        verdict = judge.run_debate(motion="AI improves outcomes", rounds=2)

        assert isinstance(verdict, Verdict)
        assert verdict.winner == "pro"
        assert judge.fsm.state is State.DONE
        assert judge.cumulative_scores["pro"] >= 1
        assert judge.cumulative_scores["con"] >= 1

        # Every prompt sent into either child carried Role.JUDGE - i.e.
        # Pro and Con never saw each other's envelopes.
        assert all(m.role is Role.JUDGE for _, m in sup.sent)

        # Transcript exists and has content.
        run_file: Path = logger.run_file
        assert run_file.exists()
        lines = [ln for ln in run_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) >= 10
        events = [json.loads(ln) for ln in lines]
        types = {e["event_type"] for e in events}
        for required in (
            "debate_started",
            "init_sent",
            "prompt_sent",
            "reply_received",
            "tool_call_received",
            "tool_result_sent",
            "score_recorded",
            "verdict_recorded",
            "debate_done",
        ):
            assert required in types, f"missing event {required!r} in transcript: {sorted(types)}"

        # No log line ever leaked an api_key / token / secret-shaped field.
        for e in events:
            for k in e:
                assert (
                    all(
                        bad not in k.lower()
                        for bad in ("api_key", "token", "secret", "password", "authorization")
                    )
                    or e[k] == "<redacted>"
                )

    def test_terminate_all_invoked_on_clean_finish(self, runs_root: Path) -> None:
        sup = _FakeSupervisor()
        sup.queue("pro", _reply(Role.PRO, "open"))
        sup.queue("con", _reply(Role.CON, "open"))
        sup.queue("pro", _reply(Role.PRO, "arg"))
        sup.queue("con", _reply(Role.CON, "arg"))
        sup.queue("pro", _reply(Role.PRO, "close"))
        sup.queue("con", _reply(Role.CON, "close"))
        judge, _logger = _build_judge(sup=sup, runs_root=runs_root, rounds=1)
        judge.run_debate(motion="m", rounds=1)
        assert sup.terminate_all_calls >= 1
