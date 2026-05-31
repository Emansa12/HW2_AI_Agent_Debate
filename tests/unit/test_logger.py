"""Unit tests for `debate.shared.logger`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from debate.shared.logger import RunLogger
from debate.shared.redaction import REDACTION_PLACEHOLDER


@pytest.fixture
def logger(tmp_path: Path) -> RunLogger:
    return RunLogger(runs_root=tmp_path, run_id="testrun")


class TestLayout:
    def test_run_dir_created(self, tmp_path: Path) -> None:
        lg = RunLogger(runs_root=tmp_path, run_id="myrun")
        assert lg.run_dir == tmp_path / "myrun"
        assert lg.run_dir.is_dir()

    def test_run_file_path(self, logger: RunLogger) -> None:
        assert logger.run_file.name == "run.jsonl"
        assert logger.run_file.parent == logger.run_dir

    def test_stderr_paths_for_pro_and_con_exist(self, logger: RunLogger) -> None:
        assert logger.pro_stderr_path == logger.run_dir / "pro_stderr.log"
        assert logger.con_stderr_path == logger.run_dir / "con_stderr.log"
        assert logger.pro_stderr_path.exists()
        assert logger.con_stderr_path.exists()

    def test_stderr_basename_is_not_a_dos_reserved_name(self, logger: RunLogger) -> None:
        """`con.stderr.log` would collide with the Windows reserved
        DOS device name `CON`; `con_stderr.log` does not.
        """
        for p in (logger.pro_stderr_path, logger.con_stderr_path):
            stem_head = p.name.split(".")[0].lower()
            assert stem_head not in {
                "con",
                "prn",
                "aux",
                "nul",
                "com1",
                "com2",
                "lpt1",
                "lpt2",
            }

    def test_stderr_paths_are_writable(self, logger: RunLogger) -> None:
        with logger.pro_stderr_path.open("ab") as f:
            f.write(b"pro error\n")
        with logger.con_stderr_path.open("ab") as f:
            f.write(b"con error\n")
        assert logger.pro_stderr_path.read_bytes() == b"pro error\n"
        assert logger.con_stderr_path.read_bytes() == b"con error\n"

    def test_collision_raises(self, tmp_path: Path) -> None:
        RunLogger(runs_root=tmp_path, run_id="collide")
        with pytest.raises(FileExistsError):
            RunLogger(runs_root=tmp_path, run_id="collide")

    def test_auto_run_id_is_iso_like_utc(self, tmp_path: Path) -> None:
        lg = RunLogger(runs_root=tmp_path)
        assert "T" in lg.run_id
        assert lg.run_id.endswith("Z")
        assert lg.run_dir.is_dir()


class TestLoggingShape:
    def test_log_writes_a_single_newline_terminated_line(self, logger: RunLogger) -> None:
        logger.log(role="judge", turn_id=0, event_type="start")
        text = logger.run_file.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert text.count("\n") == 1

    def test_each_line_is_valid_json(self, logger: RunLogger) -> None:
        for i in range(3):
            logger.log(role="pro", turn_id=i, event_type="reply", text=f"hi-{i}")
        for line in logger.run_file.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line), dict)

    def test_required_fields_present(self, logger: RunLogger) -> None:
        logger.log(role="con", turn_id=2, event_type="reply")
        data = json.loads(logger.run_file.read_text(encoding="utf-8").strip())
        assert "ts" in data
        assert isinstance(data["ts"], (int, float))
        assert data["role"] == "con"
        assert data["turn_id"] == 2
        assert data["event_type"] == "reply"

    def test_extra_fields_preserved(self, logger: RunLogger) -> None:
        logger.log(
            role="judge",
            turn_id=0,
            event_type="init",
            topic="t",
            winner=None,
            metrics={"latency_ms": 12},
        )
        data = json.loads(logger.run_file.read_text(encoding="utf-8").strip())
        assert data["topic"] == "t"
        assert data["winner"] is None
        assert data["metrics"] == {"latency_ms": 12}

    def test_multiple_writes_append(self, logger: RunLogger) -> None:
        for i in range(5):
            logger.log(role="pro", turn_id=i, event_type="reply")
        lines = logger.run_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5


class TestRedactionIntegration:
    def test_sensitive_keys_redacted_on_disk(self, logger: RunLogger) -> None:
        logger.log(
            role="pro",
            turn_id=0,
            event_type="reply",
            api_key="sk-VERY-SECRET",
            authorization="Bearer XXX",
            nested={"openai_token": "tk-abc", "ok": "ok"},
        )
        data = json.loads(logger.run_file.read_text(encoding="utf-8").strip())
        assert data["api_key"] == REDACTION_PLACEHOLDER
        assert data["authorization"] == REDACTION_PLACEHOLDER
        assert data["nested"]["openai_token"] == REDACTION_PLACEHOLDER
        assert data["nested"]["ok"] == "ok"

    def test_returned_record_matches_disk(self, logger: RunLogger) -> None:
        returned = logger.log(
            role="judge",
            turn_id=0,
            event_type="ping",
            password="hunter2",
        )
        on_disk = json.loads(logger.run_file.read_text(encoding="utf-8").strip())
        assert returned == on_disk
        assert returned["password"] == REDACTION_PLACEHOLDER


class TestClockInjection:
    def test_clock_is_used(self, tmp_path: Path) -> None:
        lg = RunLogger(runs_root=tmp_path, run_id="clock", clock=lambda: 42.0)
        lg.log(role="judge", turn_id=0, event_type="ping")
        data = json.loads(lg.run_file.read_text(encoding="utf-8").strip())
        assert data["ts"] == 42.0
