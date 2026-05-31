"""Unit tests for `debate.sdk.schemas`."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from debate.sdk.schemas import (
    SCHEMA_VERSION,
    Message,
    MessageType,
    Phase,
    Role,
    Verdict,
)


def _envelope(**overrides: Any) -> dict[str, Any]:
    """Build kwargs for a minimal valid `Message`."""
    base: dict[str, Any] = {
        "ts": 1.0,
        "turn_id": 0,
        "role": Role.PRO,
        "type": MessageType.PROMPT,
        "payload": {"text": "hello"},
    }
    base.update(overrides)
    return base


class TestEnums:
    def test_roles_are_exactly_judge_pro_con(self) -> None:
        assert {r.value for r in Role} == {"judge", "pro", "con"}

    def test_message_types_are_exhaustive(self) -> None:
        expected = {
            "init",
            "prompt",
            "reply",
            "tool_call",
            "tool_result",
            "ping",
            "pong",
            "score",
            "verdict",
            "event",
            "shutdown",
        }
        assert {t.value for t in MessageType} == expected

    def test_phases_are_opening_argument_closing(self) -> None:
        assert {p.value for p in Phase} == {"opening", "argument", "closing"}


class TestMessageEnvelopeValid:
    def test_minimal_message_constructs(self) -> None:
        msg = Message(**_envelope())
        assert msg.v == SCHEMA_VERSION
        assert msg.ts == 1.0
        assert msg.turn_id == 0
        assert msg.role == Role.PRO
        assert msg.type == MessageType.PROMPT
        assert msg.payload == {"text": "hello"}

    def test_explicit_schema_version_accepted(self) -> None:
        msg = Message(**_envelope(v=1))
        assert msg.v == 1

    def test_default_payload_is_empty_dict(self) -> None:
        data = _envelope()
        data.pop("payload")
        msg = Message(**data)
        assert msg.payload == {}

    def test_accepts_string_role_value(self) -> None:
        msg = Message(**_envelope(role="con"))
        assert msg.role == Role.CON

    def test_accepts_string_type_value(self) -> None:
        msg = Message(**_envelope(type="ping"))
        assert msg.type == MessageType.PING


class TestMessageEnvelopeInvalid:
    def test_rejects_unknown_role(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(role="moderator"))

    def test_rejects_unknown_message_type(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(type="rumination"))

    def test_rejects_unknown_schema_version(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(v=2))

    def test_rejects_negative_turn_id(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(turn_id=-1))

    def test_rejects_negative_ts(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(ts=-0.1))

    def test_rejects_extra_envelope_field(self) -> None:
        with pytest.raises(ValidationError):
            Message(**_envelope(extra_field="oops"))

    def test_rejects_missing_required_field(self) -> None:
        data = _envelope()
        data.pop("role")
        with pytest.raises(ValidationError):
            Message(**data)


class TestVerdictPayload:
    def test_winner_pro_ok(self) -> None:
        v = Verdict(winner="pro")
        assert v.winner == "pro"

    def test_winner_con_ok(self) -> None:
        v = Verdict(winner="con")
        assert v.winner == "con"

    def test_winner_tie_is_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(winner="tie")

    def test_winner_arbitrary_string_is_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Verdict(winner="judge")

    def test_verdict_message_with_pro_ok(self) -> None:
        msg = Message(
            **_envelope(
                role=Role.JUDGE,
                type=MessageType.VERDICT,
                payload={"winner": "pro", "rationale": "stronger evidence"},
            )
        )
        assert msg.payload["winner"] == "pro"

    def test_verdict_message_with_con_ok(self) -> None:
        msg = Message(
            **_envelope(
                role=Role.JUDGE,
                type=MessageType.VERDICT,
                payload={"winner": "con"},
            )
        )
        assert msg.payload["winner"] == "con"

    def test_verdict_message_with_tie_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(
                **_envelope(
                    role=Role.JUDGE,
                    type=MessageType.VERDICT,
                    payload={"winner": "tie"},
                )
            )

    def test_verdict_message_without_winner_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(
                **_envelope(
                    role=Role.JUDGE,
                    type=MessageType.VERDICT,
                    payload={"rationale": "no winner field"},
                )
            )

    def test_non_verdict_message_does_not_validate_winner_field(self) -> None:
        msg = Message(
            **_envelope(
                role=Role.PRO,
                type=MessageType.REPLY,
                payload={"winner": "tie", "text": "this is just a reply"},
            )
        )
        assert msg.payload["winner"] == "tie"
