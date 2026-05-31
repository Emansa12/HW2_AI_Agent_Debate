"""Unit tests for `debate.orchestration.ipc`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from debate.orchestration.ipc import (
    MAX_MESSAGE_BYTES,
    IPCError,
    MalformedMessageError,
    MultilineError,
    OversizeError,
    SchemaVersionError,
    deserialize_message,
    serialize_message,
)
from debate.sdk.schemas import SCHEMA_VERSION, Message, MessageType, Role


def make_message(**overrides: Any) -> Message:
    base: dict[str, Any] = {
        "ts": 1.5,
        "turn_id": 3,
        "role": Role.PRO,
        "type": MessageType.PROMPT,
        "payload": {"text": "hi"},
    }
    base.update(overrides)
    return Message(**base)


def raw_envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "ts": 1.0,
        "turn_id": 0,
        "role": "pro",
        "type": "ping",
        "payload": {},
    }
    base.update(overrides)
    return base


class TestSerialize:
    def test_returns_single_newline_terminated_line(self) -> None:
        line = serialize_message(make_message())
        assert line.endswith("\n")
        assert line.count("\n") == 1

    def test_body_is_valid_json(self) -> None:
        line = serialize_message(make_message())
        data = json.loads(line)
        assert data["v"] == SCHEMA_VERSION
        assert data["role"] == "pro"
        assert data["type"] == "prompt"
        assert data["payload"] == {"text": "hi"}

    def test_serialize_rejects_oversize_payload(self) -> None:
        big_msg = make_message(payload={"text": "x" * (MAX_MESSAGE_BYTES + 10)})
        with pytest.raises(OversizeError):
            serialize_message(big_msg)


class TestRoundtrip:
    def test_simple_roundtrip(self) -> None:
        original = make_message()
        parsed = deserialize_message(serialize_message(original))
        assert parsed.role == original.role
        assert parsed.type == original.type
        assert parsed.payload == original.payload
        assert parsed.turn_id == original.turn_id

    def test_verdict_roundtrip(self) -> None:
        original = make_message(
            role=Role.JUDGE,
            type=MessageType.VERDICT,
            payload={"winner": "con", "rationale": "tighter rebuttals"},
        )
        parsed = deserialize_message(serialize_message(original))
        assert parsed.type == MessageType.VERDICT
        assert parsed.payload == {"winner": "con", "rationale": "tighter rebuttals"}

    def test_accepts_line_without_trailing_newline(self) -> None:
        line = serialize_message(make_message()).rstrip("\n")
        parsed = deserialize_message(line)
        assert parsed.role == Role.PRO


class TestDeserializeRejects:
    def test_multiline_input(self) -> None:
        good = serialize_message(make_message())
        bad = good + '{"v":1,"ts":1.0}'
        with pytest.raises(MultilineError):
            deserialize_message(bad)

    def test_embedded_carriage_return(self) -> None:
        body = '{"v":1,"ts":1.0,"turn_id":0,"role":"pro",\r"type":"ping","payload":{}}'
        with pytest.raises(MultilineError):
            deserialize_message(body)

    def test_unsupported_schema_version(self) -> None:
        body = json.dumps(raw_envelope(v=2))
        with pytest.raises(SchemaVersionError):
            deserialize_message(body)

    def test_missing_schema_version(self) -> None:
        env = raw_envelope()
        del env["v"]
        with pytest.raises(SchemaVersionError):
            deserialize_message(json.dumps(env))

    def test_oversize_input(self) -> None:
        oversized = "x" * (MAX_MESSAGE_BYTES + 1)
        with pytest.raises(OversizeError):
            deserialize_message(oversized)

    def test_invalid_role(self) -> None:
        body = json.dumps(raw_envelope(role="moderator"))
        with pytest.raises(MalformedMessageError):
            deserialize_message(body)

    def test_invalid_message_type(self) -> None:
        body = json.dumps(raw_envelope(type="rumination"))
        with pytest.raises(MalformedMessageError):
            deserialize_message(body)

    def test_verdict_tie_rejected(self) -> None:
        body = json.dumps(
            raw_envelope(role="judge", type="verdict", payload={"winner": "tie"}),
        )
        with pytest.raises(MalformedMessageError):
            deserialize_message(body)

    def test_extra_envelope_field_rejected(self) -> None:
        env = raw_envelope()
        env["extra_field"] = "oops"
        with pytest.raises(MalformedMessageError):
            deserialize_message(json.dumps(env))

    def test_malformed_json(self) -> None:
        with pytest.raises(MalformedMessageError):
            deserialize_message("{not valid json}")

    def test_non_object_json_root(self) -> None:
        with pytest.raises(MalformedMessageError):
            deserialize_message("[1, 2, 3]")


class TestErrorHierarchy:
    def test_all_errors_inherit_from_ipc_error(self) -> None:
        for exc in (
            OversizeError,
            MultilineError,
            SchemaVersionError,
            MalformedMessageError,
        ):
            assert issubclass(exc, IPCError)
