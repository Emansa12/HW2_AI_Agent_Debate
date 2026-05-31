"""Unit tests for `debate.shared.secrets`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from debate.shared.secrets import Secrets, load_secrets, maybe_load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env-example"
ENV_FILE = REPO_ROOT / ".env"
GITIGNORE = REPO_ROOT / ".gitignore"

PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "your",
    "example",
    "placeholder",
    "fake",
    "dummy",
    "xxx",
    "changeme",
    "todo",
    "here",
    "<",
)

SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
)


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic for 'this string could plausibly be a real credential'.

    Real API keys and tokens are long, opaque, and non-placeholder.
    Numeric counters (e.g. `MAX_TOKENS=400`) are not secrets even
    though their key contains the substring `token`. We treat a value
    as 'real-secret-shaped' only if it is non-numeric, longer than 15
    characters, and lacks any placeholder marker.
    """
    if _looks_like_placeholder(value):
        return False
    if len(value) < 15:
        return False
    try:
        float(value)
        return False
    except ValueError:
        pass
    return True


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe known secret env vars before each test."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestLoadSecrets:
    def test_all_unset_returns_none(self) -> None:
        s = load_secrets()
        assert isinstance(s, Secrets)
        assert s.openai_api_key is None
        assert s.anthropic_api_key is None
        assert s.google_api_key is None
        assert not s.has_any_llm_key

    def test_reads_openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abc")
        s = load_secrets()
        assert s.openai_api_key == "sk-test-abc"
        assert s.has_any_llm_key

    def test_reads_anthropic_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        s = load_secrets()
        assert s.anthropic_api_key == "sk-ant-x"
        assert s.has_any_llm_key

    def test_reads_google_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
        s = load_secrets()
        assert s.google_api_key == "g-key"
        assert s.has_any_llm_key

    def test_empty_string_treated_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "")
        s = load_secrets()
        assert s.openai_api_key is None
        assert not s.has_any_llm_key

    def test_secrets_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import FrozenInstanceError

        monkeypatch.setenv("OPENAI_API_KEY", "k")
        s = load_secrets()
        with pytest.raises(FrozenInstanceError):
            s.openai_api_key = "changed"  # type: ignore[misc]


class TestEnvExampleFile:
    def test_exists(self) -> None:
        assert ENV_EXAMPLE.exists(), ".env-example must exist at the repo root"

    def test_contains_no_real_secret_values(self) -> None:
        """For every sensitive-looking KEY=VALUE pair in .env-example,
        the VALUE must NOT look like a real credential (i.e. it must
        be a placeholder, short, or numeric).
        """
        offending: list[str] = []
        for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key_low = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            if not any(tok in key_low for tok in SENSITIVE_KEY_TOKENS):
                continue
            if not value:
                continue
            if _looks_like_real_secret(value):
                offending.append(raw_line)
        assert not offending, (
            f".env-example contains values that look like real secrets: {offending!r}"
        )


class TestGitignore:
    def test_gitignore_exists(self) -> None:
        assert GITIGNORE.exists(), ".gitignore must exist at the repo root"

    def test_env_is_gitignored(self) -> None:
        content = GITIGNORE.read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert any(line == ".env" or line.startswith(".env") for line in lines), (
            f".gitignore must exclude .env files; current ignore lines: {lines}"
        )

    def test_no_real_env_file_present(self) -> None:
        """A real `.env` should never be committed; we cannot
        directly check VCS state from a test, but at least the file
        should not exist in a clean checkout. If it does exist (a
        local dev convenience), this test is informational only and
        will skip rather than fail.
        """
        if ENV_FILE.exists():
            pytest.skip(".env exists locally (developer machine), skipping check")


class TestMaybeLoadDotenv:
    def test_returns_false_when_missing(self, tmp_path: Path) -> None:
        assert maybe_load_dotenv(tmp_path / "missing.env") is False

    def test_loads_existing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_path = tmp_path / "tmp.env"
        env_path.write_text("MY_DEBATE_TEST_VAR=loaded-value\n", encoding="utf-8")
        monkeypatch.delenv("MY_DEBATE_TEST_VAR", raising=False)
        try:
            assert maybe_load_dotenv(env_path) is True
            assert os.environ.get("MY_DEBATE_TEST_VAR") == "loaded-value"
        finally:
            os.environ.pop("MY_DEBATE_TEST_VAR", None)
