"""Unit tests for :mod:`debate.agents.base_agent`.

Every test drives the agent's run loop through in-memory binary
streams (``BytesIO``), so there is no real subprocess and no real
stdin/stdout.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import pytest

from debate.agents.base_agent import BaseAgent
from debate.orchestration.ipc import deserialize_message, serialize_message
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(type_: MessageType, *, role: Role = Role.JUDGE, **kwargs: Any) -> Message:
    base: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "ts": 1.0,
        "turn_id": 0,
        "role": role,
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


def _out_lines(out: BytesIO) -> list[Message]:
    out.seek(0)
    return [deserialize_message(line.decode("utf-8")) for line in out.readlines() if line.strip()]


class RecordingAgent(BaseAgent):
    """BaseAgent subclass that records every dispatched message."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.received: list[Message] = []

    def handle(self, msg: Message) -> None:
        self.received.append(msg)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_role_is_stored(self) -> None:
        agent = BaseAgent(role=Role.PRO, stdin=BytesIO(), stdout=BytesIO())
        assert agent.role is Role.PRO

    def test_initial_turn_id_is_zero(self) -> None:
        agent = BaseAgent(role=Role.PRO, stdin=BytesIO(), stdout=BytesIO())
        assert agent.next_turn_id() == 0
        assert agent.next_turn_id() == 1

    def test_is_running_starts_true(self) -> None:
        agent = BaseAgent(role=Role.CON, stdin=BytesIO(), stdout=BytesIO())
        assert agent.is_running is True

    def test_stop_sets_not_running(self) -> None:
        agent = BaseAgent(role=Role.PRO, stdin=BytesIO(), stdout=BytesIO())
        agent.stop()
        assert agent.is_running is False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_ping_produces_pong(self) -> None:
        ping = _msg(MessageType.PING, role=Role.JUDGE, turn_id=42)
        stdin = _enc(ping)
        stdout = BytesIO()

        BaseAgent(role=Role.PRO, stdin=stdin, stdout=stdout, clock=lambda: 9.0).run()

        replies = _out_lines(stdout)
        assert len(replies) == 1
        pong = replies[0]
        assert pong.type is MessageType.PONG
        assert pong.role is Role.PRO
        assert pong.ts == 9.0
        assert pong.payload.get("in_reply_to") == 42

    def test_ping_does_not_call_handle(self) -> None:
        ping = _msg(MessageType.PING)
        agent = RecordingAgent(role=Role.PRO, stdin=_enc(ping), stdout=BytesIO())
        agent.run()
        assert agent.received == []

    def test_shutdown_does_not_call_handle(self) -> None:
        shutdown = _msg(MessageType.SHUTDOWN)
        agent = RecordingAgent(role=Role.PRO, stdin=_enc(shutdown), stdout=BytesIO())
        agent.run()
        assert agent.received == []


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_exits_loop_before_next_message(self) -> None:
        shutdown = _msg(MessageType.SHUTDOWN, turn_id=1)
        late_ping = _msg(MessageType.PING, turn_id=2)
        stdout = BytesIO()
        agent = BaseAgent(role=Role.PRO, stdin=_enc(shutdown, late_ping), stdout=stdout)

        rc = agent.run()

        assert rc == 0
        assert agent.is_running is False
        assert _out_lines(stdout) == []  # the post-shutdown ping was never read

    def test_run_returns_zero_on_eof(self) -> None:
        agent = BaseAgent(role=Role.PRO, stdin=BytesIO(), stdout=BytesIO())
        assert agent.run() == 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.parametrize(
        "mtype",
        [MessageType.INIT, MessageType.PROMPT, MessageType.TOOL_RESULT],
    )
    def test_handle_is_called_for_routed_types(self, mtype: MessageType) -> None:
        m = _msg(mtype, payload={"hello": "world"})
        agent = RecordingAgent(role=Role.PRO, stdin=_enc(m), stdout=BytesIO())
        agent.run()
        assert len(agent.received) == 1
        assert agent.received[0].type is mtype

    def test_unknown_handled_types_do_not_call_handle(self) -> None:
        # SCORE / VERDICT / EVENT are not in the child's dispatch set.
        score = _msg(
            MessageType.SCORE,
            payload={"score": 1, "rationale": "ok"},
        )
        agent = RecordingAgent(role=Role.PRO, stdin=_enc(score), stdout=BytesIO())
        agent.run()
        assert agent.received == []

    def test_handler_exception_does_not_crash_loop(self) -> None:
        class Boom(BaseAgent):
            def handle(self, msg: Message) -> None:
                raise RuntimeError("kaboom")

        prompt = _msg(MessageType.PROMPT)
        ping = _msg(MessageType.PING, turn_id=11)
        stdout = BytesIO()
        agent = Boom(role=Role.PRO, stdin=_enc(prompt, ping), stdout=stdout)
        agent.run()
        replies = _out_lines(stdout)
        assert len(replies) == 1
        assert replies[0].type is MessageType.PONG


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_invalid_json_does_not_crash_loop(self) -> None:
        good = serialize_message(_msg(MessageType.PING, turn_id=7)).encode("utf-8")
        stdin = BytesIO(b"not json at all\n" + good)
        stdout = BytesIO()
        agent = BaseAgent(role=Role.PRO, stdin=stdin, stdout=stdout, clock=lambda: 1.0)
        agent.run()
        replies = _out_lines(stdout)
        assert len(replies) == 1
        assert replies[0].type is MessageType.PONG

    def test_wrong_schema_version_does_not_crash_loop(self) -> None:
        bad = (
            json.dumps(
                {
                    "v": 999,
                    "ts": 0.0,
                    "turn_id": 0,
                    "role": "judge",
                    "type": "ping",
                    "payload": {},
                }
            )
            + "\n"
        ).encode("utf-8")
        good = serialize_message(_msg(MessageType.PING, turn_id=2)).encode("utf-8")
        stdout = BytesIO()
        agent = BaseAgent(
            role=Role.PRO,
            stdin=BytesIO(bad + good),
            stdout=stdout,
            clock=lambda: 1.0,
        )
        agent.run()
        replies = _out_lines(stdout)
        assert len(replies) == 1
        assert replies[0].type is MessageType.PONG

    def test_unknown_role_does_not_crash_loop(self) -> None:
        bad = (
            json.dumps(
                {
                    "v": SCHEMA_VERSION,
                    "ts": 0.0,
                    "turn_id": 0,
                    "role": "moderator",
                    "type": "ping",
                    "payload": {},
                }
            )
            + "\n"
        ).encode("utf-8")
        good = serialize_message(_msg(MessageType.PING, turn_id=2)).encode("utf-8")
        stdout = BytesIO()
        agent = BaseAgent(
            role=Role.PRO,
            stdin=BytesIO(bad + good),
            stdout=stdout,
            clock=lambda: 1.0,
        )
        agent.run()
        assert len(_out_lines(stdout)) == 1

    def test_invalid_utf8_does_not_crash_loop(self) -> None:
        # 0xff is invalid as UTF-8 start byte
        bad = b"\xff\xfe garbage \n"
        good = serialize_message(_msg(MessageType.PING, turn_id=2)).encode("utf-8")
        stdout = BytesIO()
        agent = BaseAgent(
            role=Role.PRO,
            stdin=BytesIO(bad + good),
            stdout=stdout,
            clock=lambda: 1.0,
        )
        agent.run()
        replies = _out_lines(stdout)
        assert len(replies) == 1
        assert replies[0].type is MessageType.PONG


# ---------------------------------------------------------------------------
# Outgoing wire format
# ---------------------------------------------------------------------------


class TestOutgoingWireFormat:
    def test_outgoing_is_single_newline_terminated_jsonl(self) -> None:
        ping = _msg(MessageType.PING, turn_id=1)
        stdout = BytesIO()
        BaseAgent(role=Role.PRO, stdin=_enc(ping), stdout=stdout, clock=lambda: 1.0).run()

        stdout.seek(0)
        raw = stdout.read()
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
        body = raw.decode("utf-8").rstrip("\n")
        # Embedded newlines / CR are forbidden by the IPC contract.
        assert "\n" not in body
        assert "\r" not in body
        parsed = json.loads(body)
        assert parsed["type"] == "pong"
        assert parsed["v"] == SCHEMA_VERSION

    def test_make_message_uses_injected_clock(self) -> None:
        agent = BaseAgent(
            role=Role.CON,
            stdin=BytesIO(),
            stdout=BytesIO(),
            clock=lambda: 123.456,
        )
        msg = agent.make_message(MessageType.PONG, {"k": "v"})
        assert msg.ts == 123.456
        assert msg.role is Role.CON
        assert msg.payload == {"k": "v"}

    def test_send_writes_via_ipc_serializer(self) -> None:
        stdout = BytesIO()
        agent = BaseAgent(
            role=Role.PRO,
            stdin=BytesIO(),
            stdout=stdout,
            clock=lambda: 1.0,
        )
        msg = agent.make_message(MessageType.EVENT, {"e": "hi"})
        agent.send(msg)
        stdout.seek(0)
        line = stdout.read().decode("utf-8")
        # Round-trips back to an identical Message via the IPC helper.
        round_trip = deserialize_message(line)
        assert round_trip == msg


# ---------------------------------------------------------------------------
# Multiple messages in one stdin
# ---------------------------------------------------------------------------


class TestMultiplePings:
    def test_two_pings_yield_two_pongs(self) -> None:
        ping1 = _msg(MessageType.PING, turn_id=1)
        ping2 = _msg(MessageType.PING, turn_id=2)
        shutdown = _msg(MessageType.SHUTDOWN, turn_id=3)
        stdout = BytesIO()
        agent = BaseAgent(
            role=Role.PRO,
            stdin=_enc(ping1, ping2, shutdown),
            stdout=stdout,
            clock=lambda: 1.0,
        )
        agent.run()
        replies = _out_lines(stdout)
        assert len(replies) == 2
        assert all(r.type is MessageType.PONG for r in replies)
        assert [r.payload["in_reply_to"] for r in replies] == [1, 2]
