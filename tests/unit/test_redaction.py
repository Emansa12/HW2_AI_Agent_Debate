"""Unit tests for `debate.shared.redaction`."""

from __future__ import annotations

from debate.shared.redaction import (
    REDACTION_PLACEHOLDER,
    SENSITIVE_KEY_TOKENS,
    is_sensitive_key,
    redact,
)


class TestIsSensitiveKey:
    def test_exact_tokens_match(self) -> None:
        for token in SENSITIVE_KEY_TOKENS:
            assert is_sensitive_key(token)

    def test_required_keys_match(self) -> None:
        for key in (
            "api_key",
            "token",
            "secret",
            "password",
            "authorization",
        ):
            assert is_sensitive_key(key)

    def test_case_insensitive(self) -> None:
        assert is_sensitive_key("API_KEY")
        assert is_sensitive_key("Authorization")
        assert is_sensitive_key("PASSWORD")
        assert is_sensitive_key("Secret")
        assert is_sensitive_key("Token")

    def test_substring_match(self) -> None:
        assert is_sensitive_key("openai_api_key")
        assert is_sensitive_key("user_password")
        assert is_sensitive_key("auth_token")
        assert is_sensitive_key("client_secret")
        assert is_sensitive_key("X-Authorization-Header")

    def test_non_sensitive_keys(self) -> None:
        for key in ("name", "topic", "ts", "role", "turn_id", "event_type"):
            assert not is_sensitive_key(key)


class TestRedactDict:
    def test_each_trigger_keyword_redacted(self) -> None:
        for key in ("api_key", "token", "secret", "password", "authorization"):
            assert redact({key: "x"}) == {key: REDACTION_PLACEHOLDER}

    def test_case_insensitive_redaction(self) -> None:
        assert redact({"API_KEY": "x"}) == {"API_KEY": REDACTION_PLACEHOLDER}
        assert redact({"Authorization": "Bearer x"}) == {"Authorization": REDACTION_PLACEHOLDER}

    def test_substring_redaction(self) -> None:
        assert redact({"openai_api_key": "x"}) == {"openai_api_key": REDACTION_PLACEHOLDER}

    def test_non_sensitive_preserved(self) -> None:
        d = {"name": "alice", "role": "pro", "ts": 1.0, "turn_id": 0}
        assert redact(d) == d


class TestRedactNested:
    def test_nested_dict(self) -> None:
        d = {"outer": {"api_key": "x", "ok": "ok"}}
        assert redact(d) == {"outer": {"api_key": REDACTION_PLACEHOLDER, "ok": "ok"}}

    def test_list_of_dicts(self) -> None:
        d = {"users": [{"token": "x"}, {"name": "alice"}]}
        assert redact(d) == {"users": [{"token": REDACTION_PLACEHOLDER}, {"name": "alice"}]}

    def test_tuple_of_dicts(self) -> None:
        d = {"pair": ({"secret": "x"}, {"role": "judge"})}
        out = redact(d)
        assert isinstance(out["pair"], tuple)
        assert out["pair"][0] == {"secret": REDACTION_PLACEHOLDER}
        assert out["pair"][1] == {"role": "judge"}

    def test_deeply_nested(self) -> None:
        d = {"a": {"b": {"c": {"password": "x", "ok": 1}}}}
        out = redact(d)
        assert out["a"]["b"]["c"]["password"] == REDACTION_PLACEHOLDER
        assert out["a"]["b"]["c"]["ok"] == 1


class TestRedactPurity:
    def test_does_not_mutate_input(self) -> None:
        original = {"api_key": "sk-secret", "name": "alice"}
        snapshot = dict(original)
        redact(original)
        assert original == snapshot

    def test_returns_new_object(self) -> None:
        original = {"api_key": "sk-secret"}
        out = redact(original)
        assert out is not original

    def test_scalars_pass_through(self) -> None:
        assert redact("hello") == "hello"
        assert redact(42) == 42
        assert redact(None) is None
        assert redact(3.14) == 3.14
        assert redact(True) is True
