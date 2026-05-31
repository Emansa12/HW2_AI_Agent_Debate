"""Tests for ``config/prompts/verdict.schema.json`` (Stage 10).

This JSON Schema is the public, language-agnostic mirror of
:class:`debate.sdk.schemas.Verdict` plus the
:meth:`debate.orchestration.judge.Judge.validate_verdict` contract.
We do not pull a JSON-schema validator into the runtime
dependencies; instead we drive a small, hand-rolled validator that
checks exactly the constraints we promise (winner enum, scores
shape + bounds, reasons array size, no extras).

The same constraints are exercised end-to-end by
``test_judge_agent.py`` against the live :class:`Verdict` Pydantic
model. This test layer just makes sure the JSON file itself stays
in sync with that contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SCHEMA_PATH: Path = Path("config") / "prompts" / "verdict.schema.json"


# ---------------------------------------------------------------------------
# Tiny inline validator
# ---------------------------------------------------------------------------


def _validate(schema: dict[str, Any], data: Any) -> None:
    """Validate ``data`` against the slice of JSON-Schema we use.

    Supports: ``type`` (incl. union list), ``enum``, ``properties``,
    ``required``, ``additionalProperties``, ``minimum`` /
    ``maximum`` for numbers, ``minLength`` / ``items`` /
    ``minItems`` for arrays. Any rule not in this slice is ignored
    (we only care about the rules our verdict schema declares).
    Raises :class:`ValueError` on the first violation.
    """
    expected_type = schema.get("type")
    if expected_type is not None:
        _check_type(expected_type, data)

    enum = schema.get("enum")
    if enum is not None and data not in enum:
        raise ValueError(f"value {data!r} not in enum {enum!r}")

    if isinstance(data, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                raise ValueError(f"missing required property {key!r}")

        properties = schema.get("properties", {})
        additional_ok = schema.get("additionalProperties", True)
        for key, value in data.items():
            if key in properties:
                _validate(properties[key], value)
            elif additional_ok is False:
                raise ValueError(f"extra property {key!r} is not allowed")

    elif isinstance(data, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            raise ValueError(f"array has {len(data)} items, minimum is {min_items}")
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(data):
                try:
                    _validate(item_schema, item)
                except ValueError as exc:
                    raise ValueError(f"items[{i}]: {exc}") from exc

    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and data < minimum:
            raise ValueError(f"value {data} below minimum {minimum}")
        if maximum is not None and data > maximum:
            raise ValueError(f"value {data} above maximum {maximum}")

    elif isinstance(data, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(data) < min_length:
            raise ValueError(f"string length {len(data)} below minLength {min_length}")


def _check_type(expected: Any, data: Any) -> None:
    """Validate ``data`` against a JSON-Schema ``type`` clause."""
    if isinstance(expected, list):
        if not any(_matches_type(t, data) for t in expected):
            raise ValueError(
                f"value of type {type(data).__name__} does not match any of {expected!r}"
            )
        return
    if not _matches_type(expected, data):
        raise ValueError(f"expected type {expected!r}, got {type(data).__name__}")


def _matches_type(expected: str, data: Any) -> bool:
    if expected == "object":
        return isinstance(data, dict)
    if expected == "array":
        return isinstance(data, list)
    if expected == "string":
        return isinstance(data, str)
    if expected == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if expected == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected == "boolean":
        return isinstance(data, bool)
    if expected == "null":
        return data is None
    return False


# ---------------------------------------------------------------------------
# File / shape
# ---------------------------------------------------------------------------


@pytest.fixture
def schema() -> dict[str, Any]:
    assert SCHEMA_PATH.exists(), f"missing {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class TestSchemaShape:
    def test_file_exists(self) -> None:
        assert SCHEMA_PATH.is_file(), f"{SCHEMA_PATH} must exist"

    def test_root_is_object_with_required_fields(self, schema: dict[str, Any]) -> None:
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"winner", "scores", "reasons"}
        assert schema.get("additionalProperties") is False, (
            "verdict.schema.json must reject extra root fields"
        )

    def test_winner_enum_is_pro_or_con_only(self, schema: dict[str, Any]) -> None:
        winner = schema["properties"]["winner"]
        assert winner["type"] == "string"
        assert sorted(winner["enum"]) == ["con", "pro"]
        assert "tie" not in winner["enum"], "tie verdicts are forbidden by protocol"

    def test_scores_shape_and_bounds(self, schema: dict[str, Any]) -> None:
        scores = schema["properties"]["scores"]
        assert scores["type"] == "object"
        assert set(scores["required"]) == {"pro", "con"}
        assert scores.get("additionalProperties") is False
        for side in ("pro", "con"):
            spec = scores["properties"][side]
            assert spec["type"] == "number"
            assert spec["minimum"] == 0
            assert spec["maximum"] == 100

    def test_reasons_array_min_items(self, schema: dict[str, Any]) -> None:
        reasons = schema["properties"]["reasons"]
        assert reasons["type"] == "array"
        assert reasons["minItems"] == 3
        items = reasons["items"]
        assert items["type"] == "string"
        assert items["minLength"] >= 1


# ---------------------------------------------------------------------------
# Constraint behavior
# ---------------------------------------------------------------------------


def _valid_verdict() -> dict[str, Any]:
    return {
        "winner": "pro",
        "scores": {"pro": 12, "con": 8},
        "reasons": ["a", "b", "c"],
        "rationale": "fine",
    }


class TestConstraints:
    def test_valid_verdict_passes(self, schema: dict[str, Any]) -> None:
        _validate(schema, _valid_verdict())

    def test_tie_winner_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["winner"] = "tie"
        with pytest.raises(ValueError, match="enum"):
            _validate(schema, bad)

    def test_unknown_winner_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["winner"] = "neither"
        with pytest.raises(ValueError):
            _validate(schema, bad)

    def test_missing_scores_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        del bad["scores"]
        with pytest.raises(ValueError, match="scores"):
            _validate(schema, bad)

    def test_missing_pro_score_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["scores"] = {"con": 5}
        with pytest.raises(ValueError):
            _validate(schema, bad)

    def test_score_above_100_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["scores"] = {"pro": 150, "con": 10}
        with pytest.raises(ValueError, match="maximum"):
            _validate(schema, bad)

    def test_negative_score_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["scores"] = {"pro": -1, "con": 10}
        with pytest.raises(ValueError, match="minimum"):
            _validate(schema, bad)

    def test_too_few_reasons_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["reasons"] = ["only one", "two"]
        with pytest.raises(ValueError, match="minItems|minimum"):
            _validate(schema, bad)

    def test_empty_reason_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["reasons"] = ["a", "b", ""]
        with pytest.raises(ValueError):
            _validate(schema, bad)

    def test_extra_root_field_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["unknown"] = 42
        with pytest.raises(ValueError, match="extra property"):
            _validate(schema, bad)

    def test_extra_score_side_rejected(self, schema: dict[str, Any]) -> None:
        bad = _valid_verdict()
        bad["scores"] = {"pro": 5, "con": 4, "judge": 9}
        with pytest.raises(ValueError, match="extra property"):
            _validate(schema, bad)

    def test_rationale_can_be_null(self, schema: dict[str, Any]) -> None:
        v = _valid_verdict()
        v["rationale"] = None
        _validate(schema, v)

    def test_rationale_can_be_omitted(self, schema: dict[str, Any]) -> None:
        v = _valid_verdict()
        del v["rationale"]
        _validate(schema, v)
