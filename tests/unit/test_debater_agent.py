"""Unit tests for :class:`debate.agents.debater_agent.DebaterAgent`
and the minimal :class:`ProAgent` / :class:`ConAgent` subclasses.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from debate.agents import (
    BaseAgent,
    ConAgent,
    DebaterAgent,
    ProAgent,
)
from debate.agents import con_agent as con_agent_module
from debate.agents import debater_agent as debater_agent_module
from debate.agents import pro_agent as pro_agent_module
from debate.orchestration.ipc import deserialize_message, serialize_message
from debate.sdk.llm_client import FakeLLMClient, LLMResponse
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Phase, Role

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class RecordingLLM:
    """Deterministic LLM that records every prompt it sees."""

    def __init__(self, text: str = "(reply text)") -> None:
        self._text = text
        self.prompts: list[str] = []
        self.max_tokens_seen: list[int] = []

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
        self.prompts.append(prompt)
        self.max_tokens_seen.append(max_tokens)
        return LLMResponse(text=self._text, tokens_in=1, tokens_out=1, usd=0.0)


def _msg(type_: MessageType, **kwargs: Any) -> Message:
    base: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "ts": 1.0,
        "turn_id": 0,
        "role": Role.JUDGE,
        "type": type_,
        "payload": {},
    }
    base.update(kwargs)
    return Message(**base)


def _enc(*messages: Message) -> BytesIO:
    buf = BytesIO()
    for m in messages:
        buf.write(serialize_message(m).encode("utf-8"))
    buf.seek(0)
    return buf


def _out(stdout: BytesIO) -> list[Message]:
    stdout.seek(0)
    return [
        deserialize_message(line.decode("utf-8")) for line in stdout.readlines() if line.strip()
    ]


def _pro(**kwargs: Any) -> ProAgent:
    kwargs.setdefault("llm_client", FakeLLMClient(response_text="(fake)"))
    kwargs.setdefault("motion", "cats are better than dogs")
    kwargs.setdefault("stdin", BytesIO())
    kwargs.setdefault("stdout", BytesIO())
    kwargs.setdefault("clock", lambda: 1.0)
    return ProAgent(**kwargs)


def _con(**kwargs: Any) -> ConAgent:
    kwargs.setdefault("llm_client", FakeLLMClient(response_text="(fake)"))
    kwargs.setdefault("motion", "cats are better than dogs")
    kwargs.setdefault("stdin", BytesIO())
    kwargs.setdefault("stdout", BytesIO())
    kwargs.setdefault("clock", lambda: 1.0)
    return ConAgent(**kwargs)


# ---------------------------------------------------------------------------
# Subclass shape: stance, role, minimal subclassing
# ---------------------------------------------------------------------------


class TestSubclassShape:
    def test_pro_agent_stance_is_pro(self) -> None:
        assert ProAgent.STANCE == "pro"

    def test_con_agent_stance_is_con(self) -> None:
        assert ConAgent.STANCE == "con"

    def test_pro_agent_role_is_pro(self) -> None:
        assert _pro().role is Role.PRO

    def test_con_agent_role_is_con(self) -> None:
        assert _con().role is Role.CON

    def test_debater_subclasses_only_set_stance(self) -> None:
        """Pro/Con must not introduce new methods or attributes beyond ``STANCE``."""
        for cls in (ProAgent, ConAgent):
            own = {
                k
                for k in vars(cls)
                if not k.startswith("__") and k != "STANCE" and k != "__main_block__"
            }
            # Allow trivial helpers like __all__, but reject anything callable.
            assert own == set(), f"{cls.__name__} defines extra attributes beyond STANCE: {own}"
        assert "STANCE" in vars(ProAgent)
        assert "STANCE" in vars(ConAgent)

    def test_invalid_stance_rejected_at_construction(self) -> None:
        class Weird(DebaterAgent):
            STANCE = "neutral"

        with pytest.raises(TypeError):
            Weird(llm_client=FakeLLMClient(), motion="m")

    def test_unset_stance_rejected_at_construction(self) -> None:
        class Bare(DebaterAgent):
            pass

        with pytest.raises(TypeError):
            Bare(llm_client=FakeLLMClient(), motion="m")

    def test_pro_and_con_inherit_handle_from_debater(self) -> None:
        # `handle` must be defined on DebaterAgent and inherited by Pro/Con
        # without being overridden.
        assert ProAgent.handle is DebaterAgent.handle
        assert ConAgent.handle is DebaterAgent.handle

    def test_inherits_from_base_agent(self) -> None:
        assert issubclass(DebaterAgent, BaseAgent)
        assert issubclass(ProAgent, DebaterAgent)
        assert issubclass(ConAgent, DebaterAgent)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    @pytest.mark.parametrize("phase", list(Phase))
    def test_includes_motion(self, phase: Phase) -> None:
        agent = _pro(motion="MOTION_TOKEN_XYZ")
        prompt = agent.build_prompt(phase)
        assert "MOTION_TOKEN_XYZ" in prompt

    def test_includes_pro_stance(self) -> None:
        prompt = _pro().build_prompt(Phase.OPENING)
        assert "pro" in prompt.lower()
        assert "in favor" in prompt.lower()

    def test_includes_con_stance(self) -> None:
        prompt = _con().build_prompt(Phase.OPENING)
        assert "con" in prompt.lower()
        assert "against" in prompt.lower()

    @pytest.mark.parametrize("phase", list(Phase))
    def test_includes_phase_marker(self, phase: Phase) -> None:
        prompt = _pro().build_prompt(phase)
        assert phase.value in prompt

    def test_argument_phase_includes_opponent_last(self) -> None:
        agent = _pro()
        agent.opponent_last = "THEIR_POINT_42"
        prompt = agent.build_prompt(Phase.ARGUMENT)
        assert "THEIR_POINT_42" in prompt

    def test_closing_phase_includes_opponent_last(self) -> None:
        agent = _pro()
        agent.opponent_last = "THEIR_POINT_99"
        prompt = agent.build_prompt(Phase.CLOSING)
        assert "THEIR_POINT_99" in prompt

    def test_opening_phase_omits_opponent_last(self) -> None:
        agent = _pro()
        agent.opponent_last = "SHOULD_NOT_APPEAR"
        prompt = agent.build_prompt(Phase.OPENING)
        assert "SHOULD_NOT_APPEAR" not in prompt

    def test_includes_selected_context(self) -> None:
        agent = _pro(selected_context=["FACT_A", "FACT_B"])
        prompt = agent.build_prompt(Phase.OPENING)
        assert "FACT_A" in prompt
        assert "FACT_B" in prompt

    def test_includes_previous_tool_results(self) -> None:
        agent = _pro()
        agent.previous_tool_results.append({"tool": "search", "results": ["TOOLRES_ZZZ"]})
        prompt = agent.build_prompt(Phase.ARGUMENT)
        assert "TOOLRES_ZZZ" in prompt

    def test_omits_optional_sections_when_unset(self) -> None:
        prompt = _pro().build_prompt(Phase.OPENING)
        assert "Opponent" not in prompt
        assert "Context" not in prompt
        assert "Tool results" not in prompt


# ---------------------------------------------------------------------------
# Reply generation per phase
# ---------------------------------------------------------------------------


class TestGenerateReply:
    @pytest.mark.parametrize("phase", list(Phase))
    def test_returns_reply_message(self, phase: Phase) -> None:
        agent = _pro()
        reply = agent.generate_reply(phase)
        assert isinstance(reply, Message)
        assert reply.type is MessageType.REPLY
        assert reply.role is Role.PRO
        assert reply.payload["phase"] == phase.value
        assert reply.payload["stance"] == "pro"

    def test_reply_uses_llm_text(self) -> None:
        llm = RecordingLLM(text="hello debate")
        agent = _pro(llm_client=llm)
        reply = agent.generate_reply(Phase.OPENING)
        assert reply.payload["content"] == "hello debate"

    def test_reply_passes_max_tokens_to_llm(self) -> None:
        llm = RecordingLLM()
        agent = _pro(llm_client=llm, max_tokens=137)
        agent.generate_reply(Phase.ARGUMENT)
        assert llm.max_tokens_seen == [137]

    def test_reply_contains_token_usage(self) -> None:
        llm = RecordingLLM()
        agent = _pro(llm_client=llm)
        reply = agent.generate_reply(Phase.OPENING)
        assert reply.payload["tokens_in"] == 1
        assert reply.payload["tokens_out"] == 1

    def test_reply_round_trips_through_ipc(self) -> None:
        agent = _pro()
        reply = agent.generate_reply(Phase.OPENING)
        line = serialize_message(reply)
        round_trip = deserialize_message(line)
        assert round_trip == reply

    def test_con_reply_carries_con_stance(self) -> None:
        reply = _con().generate_reply(Phase.OPENING)
        assert reply.payload["stance"] == "con"
        assert reply.role is Role.CON


# ---------------------------------------------------------------------------
# Stance discipline (agent stays in role)
# ---------------------------------------------------------------------------


class TestStanceDiscipline:
    def test_pro_reply_always_stamps_pro(self) -> None:
        agent = _pro()
        for phase in Phase:
            reply = agent.generate_reply(phase)
            assert reply.payload["stance"] == "pro"
            assert reply.role is Role.PRO

    def test_con_reply_always_stamps_con(self) -> None:
        agent = _con()
        for phase in Phase:
            reply = agent.generate_reply(phase)
            assert reply.payload["stance"] == "con"
            assert reply.role is Role.CON

    def test_init_rejects_stance_mismatch_directly(self) -> None:
        """Called directly, _on_init raises and motion is not mutated."""
        agent = _pro()
        init = _msg(MessageType.INIT, payload={"stance": "con", "motion": "m"})
        with pytest.raises(ValueError, match="stance mismatch"):
            agent.handle(init)
        assert agent.motion == "cats are better than dogs"

    def test_init_stance_mismatch_in_loop_is_swallowed(self) -> None:
        """Driven through run(): the loop catches the ValueError and keeps going."""
        init = _msg(MessageType.INIT, payload={"stance": "con", "motion": "NEW"})
        ping = _msg(MessageType.PING, turn_id=7)
        shutdown = _msg(MessageType.SHUTDOWN)
        stdout = BytesIO()
        agent = _pro(stdin=_enc(init, ping, shutdown), stdout=stdout)
        agent.run()
        wire = _out(stdout)
        assert len(wire) == 1
        assert wire[0].type is MessageType.PONG
        assert agent.motion == "cats are better than dogs"


# ---------------------------------------------------------------------------
# Search tool_call emission
# ---------------------------------------------------------------------------


class TestRequestSearch:
    def test_emits_tool_call_message(self) -> None:
        stdout = BytesIO()
        agent = _pro(stdout=stdout)
        msg = agent.request_search("are cats better than dogs?")
        assert msg.type is MessageType.TOOL_CALL
        assert msg.role is Role.PRO
        assert msg.payload == {
            "tool": "search",
            "query": "are cats better than dogs?",
        }

    def test_writes_tool_call_to_stdout(self) -> None:
        stdout = BytesIO()
        agent = _pro(stdout=stdout)
        agent.request_search("hello")
        wire = _out(stdout)
        assert len(wire) == 1
        assert wire[0].type is MessageType.TOOL_CALL
        assert wire[0].payload["query"] == "hello"

    def test_rejects_empty_query(self) -> None:
        agent = _pro()
        with pytest.raises(ValueError):
            agent.request_search("")

    def test_rejects_whitespace_query(self) -> None:
        agent = _pro()
        with pytest.raises(ValueError):
            agent.request_search("   ")


# ---------------------------------------------------------------------------
# init / prompt / tool_result dispatch via run loop
# ---------------------------------------------------------------------------


class TestRunLoopIntegration:
    def test_prompt_message_yields_reply(self) -> None:
        llm = RecordingLLM(text="argued")
        prompt = _msg(
            MessageType.PROMPT,
            payload={"phase": "argument", "opponent_last": "they said X"},
        )
        shutdown = _msg(MessageType.SHUTDOWN)
        stdout = BytesIO()
        agent = _pro(
            llm_client=llm,
            stdin=_enc(prompt, shutdown),
            stdout=stdout,
        )
        agent.run()
        wire = _out(stdout)
        assert len(wire) == 1
        assert wire[0].type is MessageType.REPLY
        assert wire[0].payload["content"] == "argued"
        assert wire[0].payload["phase"] == "argument"
        # The opponent_last must have made it into the LLM prompt:
        assert "they said X" in llm.prompts[0]

    def test_init_updates_motion_and_context(self) -> None:
        agent = _pro(motion="original motion")
        init = _msg(
            MessageType.INIT,
            payload={
                "stance": "pro",
                "motion": "new motion",
                "max_tokens": 42,
                "selected_context": ["c1", "c2"],
            },
        )
        agent.handle(init)
        assert agent.motion == "new motion"
        assert agent.max_tokens == 42
        assert agent.selected_context == ["c1", "c2"]

    def test_tool_result_records_into_history(self) -> None:
        agent = _pro()
        tr = _msg(
            MessageType.TOOL_RESULT,
            payload={"tool": "search", "results": ["r1", "r2"], "query": "q"},
        )
        agent.handle(tr)
        assert agent.previous_tool_results == [
            {"tool": "search", "results": ["r1", "r2"], "query": "q"}
        ]
        prompt = agent.build_prompt(Phase.ARGUMENT)
        assert "r1" in prompt or "['r1', 'r2']" in prompt

    def test_ping_inside_debate_flow_still_yields_pong(self) -> None:
        ping = _msg(MessageType.PING, turn_id=99)
        shutdown = _msg(MessageType.SHUTDOWN)
        stdout = BytesIO()
        agent = _pro(stdin=_enc(ping, shutdown), stdout=stdout)
        agent.run()
        wire = _out(stdout)
        assert len(wire) == 1
        assert wire[0].type is MessageType.PONG

    def test_outgoing_reply_is_valid_single_line_jsonl(self) -> None:
        prompt = _msg(MessageType.PROMPT, payload={"phase": "opening"})
        shutdown = _msg(MessageType.SHUTDOWN)
        stdout = BytesIO()
        agent = _pro(stdin=_enc(prompt, shutdown), stdout=stdout)
        agent.run()
        stdout.seek(0)
        raw = stdout.readline()
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
        # And it survives the IPC round-trip.
        deserialize_message(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Static checks: DebaterAgent must not bypass the parent for tools
# ---------------------------------------------------------------------------


class TestNoDirectToolImports:
    @pytest.fixture
    def debater_src(self) -> str:
        return Path(debater_agent_module.__file__).read_text(encoding="utf-8")

    @pytest.fixture
    def pro_src(self) -> str:
        return Path(pro_agent_module.__file__).read_text(encoding="utf-8")

    @pytest.fixture
    def con_src(self) -> str:
        return Path(con_agent_module.__file__).read_text(encoding="utf-8")

    @staticmethod
    def _import_lines(src: str) -> list[str]:
        return [
            line.strip()
            for line in src.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]

    @pytest.mark.parametrize(
        "forbidden",
        [
            "SearchClient",
            "FakeSearchClient",
            "search_client",
            "ToolRouter",
            "shared.router",
            "Gatekeeper",
            "shared.gatekeeper",
            "shared.ledger",
        ],
    )
    def test_debater_module_does_not_import_forbidden(
        self, debater_src: str, forbidden: str
    ) -> None:
        for line in self._import_lines(debater_src):
            assert forbidden not in line, (
                f"debater_agent must not import {forbidden!r}; saw: {line}"
            )

    @pytest.mark.parametrize(
        "forbidden",
        [
            "SearchClient",
            "FakeSearchClient",
            "ToolRouter",
            "Gatekeeper",
            "subprocess",
            "requests",
            "httpx",
            "openai",
        ],
    )
    def test_pro_and_con_modules_do_not_import_forbidden(
        self, pro_src: str, con_src: str, forbidden: str
    ) -> None:
        for src, name in ((pro_src, "pro_agent"), (con_src, "con_agent")):
            for line in self._import_lines(src):
                assert forbidden not in line, f"{name} must not import {forbidden!r}; saw: {line}"

    def test_debater_module_uses_ipc_via_base_agent(self, debater_src: str) -> None:
        # DebaterAgent itself should NOT import the IPC helpers directly;
        # it sends through BaseAgent.send, which uses them.
        # (Importing serialize_message would suggest bypassing the base.)
        for line in self._import_lines(debater_src):
            assert "serialize_message" not in line
            assert "deserialize_message" not in line

    def test_debater_module_does_not_call_search_directly(self, debater_src: str) -> None:
        # Any of these substrings would suggest the agent was trying to
        # call search outside the tool_call envelope.
        for forbidden in (
            "SearchClient(",
            "FakeSearchClient(",
            "ToolRouter(",
            ".search(",
        ):
            assert forbidden not in debater_src, f"debater_agent must not call {forbidden!r}"
