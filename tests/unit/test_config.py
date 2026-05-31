"""Unit tests for `debate.shared.config`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from debate.shared.config import (
    DebateConfig,
    Motions,
    default_debate_config_path,
    default_motions_path,
    load_debate_config,
    load_motions,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _valid_debate_dict() -> dict[str, Any]:
    return {
        "rounds": 10,
        "token_limit_per_turn": 400,
        "budget_total_tokens": 50000,
        "heartbeat_seconds": 5.0,
        "max_message_bytes": 65536,
        "per_turn_timeout_seconds": 30.0,
        "total_timeout_seconds": 300.0,
    }


def _valid_motions_dict() -> dict[str, Any]:
    return {
        "motions": [
            {"id": "m1", "topic": "Resolved: water is wet."},
        ]
    }


class TestDebateConfigValid:
    def test_minimal_valid(self) -> None:
        cfg = DebateConfig(**_valid_debate_dict())
        assert cfg.rounds == 10
        assert cfg.max_message_bytes == 65536
        assert cfg.heartbeat_seconds == 5.0

    def test_frozen(self) -> None:
        cfg = DebateConfig(**_valid_debate_dict())
        with pytest.raises(ValidationError):
            cfg.rounds = 99  # type: ignore[misc]


class TestDebateConfigInvalid:
    @pytest.mark.parametrize("rounds", [0, -1, 101])
    def test_rejects_bad_rounds(self, rounds: int) -> None:
        d = _valid_debate_dict()
        d["rounds"] = rounds
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    @pytest.mark.parametrize("limit", [0, -1, 8193])
    def test_rejects_bad_token_limit(self, limit: int) -> None:
        d = _valid_debate_dict()
        d["token_limit_per_turn"] = limit
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    @pytest.mark.parametrize("budget", [0, -1])
    def test_rejects_bad_budget(self, budget: int) -> None:
        d = _valid_debate_dict()
        d["budget_total_tokens"] = budget
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    @pytest.mark.parametrize("hb", [0, -0.1, 601.0])
    def test_rejects_bad_heartbeat(self, hb: float) -> None:
        d = _valid_debate_dict()
        d["heartbeat_seconds"] = hb
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    @pytest.mark.parametrize("size", [0, 1023, -1])
    def test_rejects_too_small_max_message_bytes(self, size: int) -> None:
        d = _valid_debate_dict()
        d["max_message_bytes"] = size
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    def test_rejects_extra_field(self) -> None:
        d = _valid_debate_dict()
        d["unknown_extra"] = "x"
        with pytest.raises(ValidationError):
            DebateConfig(**d)

    def test_rejects_missing_field(self) -> None:
        d = _valid_debate_dict()
        del d["rounds"]
        with pytest.raises(ValidationError):
            DebateConfig(**d)


class TestMotions:
    def test_minimal_valid(self) -> None:
        m = Motions(**_valid_motions_dict())
        assert len(m.motions) == 1
        assert m.motions[0].id == "m1"

    def test_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError):
            Motions(motions=[])

    def test_rejects_empty_id(self) -> None:
        with pytest.raises(ValidationError):
            Motions(motions=[{"id": "", "topic": "x"}])

    def test_rejects_extra_field_on_motion(self) -> None:
        with pytest.raises(ValidationError):
            Motions(motions=[{"id": "m1", "topic": "x", "extra": True}])


class TestLoaders:
    def test_load_debate_config_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "debate.json"
        p.write_text(json.dumps(_valid_debate_dict()), encoding="utf-8")
        cfg = load_debate_config(p)
        assert cfg.rounds == 10

    def test_load_motions_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "motions.json"
        p.write_text(json.dumps(_valid_motions_dict()), encoding="utf-8")
        m = load_motions(p)
        assert m.motions[0].topic == "Resolved: water is wet."

    def test_load_debate_config_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_debate_config(tmp_path / "nope.json")

    def test_load_motions_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_motions(tmp_path / "nope.json")

    def test_load_debate_config_malformed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_debate_config(p)

    def test_load_debate_config_non_object_root(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_debate_config(p)

    def test_load_debate_config_invalid_schema(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"rounds": -1}), encoding="utf-8")
        with pytest.raises(ValidationError):
            load_debate_config(p)


class TestBundledConfigs:
    """The `config/*.json` files shipped with the repo must validate."""

    def test_default_debate_config_path(self) -> None:
        assert default_debate_config_path() == Path("config") / "debate.json"

    def test_default_motions_path(self) -> None:
        assert default_motions_path() == Path("config") / "motions.json"

    def test_bundled_debate_config_loads(self) -> None:
        cfg = load_debate_config(REPO_ROOT / "config" / "debate.json")
        assert cfg.rounds >= 1
        assert cfg.max_message_bytes >= 1024
        assert cfg.per_turn_timeout_seconds > 0

    def test_bundled_motions_loads(self) -> None:
        motions = load_motions(REPO_ROOT / "config" / "motions.json")
        assert len(motions.motions) >= 1
        for m in motions.motions:
            assert m.id
            assert m.topic
