"""Unit tests for `debate.sdk.llm_client`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from debate.sdk.llm_client import FakeLLMClient, LLMClient, LLMResponse


class TestLLMResponse:
    def test_minimal_valid(self) -> None:
        r = LLMResponse(text="hi", tokens_in=1, tokens_out=2, usd=0.01)
        assert r.total_tokens == 3
        assert r.text == "hi"

    def test_frozen(self) -> None:
        r = LLMResponse(text="hi", tokens_in=1, tokens_out=1, usd=0.0)
        with pytest.raises(ValidationError):
            r.text = "boom"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("tokens_in", -1),
            ("tokens_out", -1),
            ("usd", -0.01),
        ],
    )
    def test_rejects_negative_metrics(self, field: str, value: float) -> None:
        kw = {"text": "x", "tokens_in": 1, "tokens_out": 1, "usd": 0.0}
        kw[field] = value
        with pytest.raises(ValidationError):
            LLMResponse(**kw)

    def test_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            LLMResponse(text="x", tokens_in=1, tokens_out=1, usd=0.0, extra="bad")


class TestFakeLLMClient:
    def test_satisfies_protocol(self) -> None:
        client = FakeLLMClient()
        assert isinstance(client, LLMClient)

    def test_complete_returns_response(self) -> None:
        client = FakeLLMClient(response_text="hello world")
        r = client.complete(prompt="say hi", max_tokens=50)
        assert isinstance(r, LLMResponse)
        assert r.text == "hello world"
        assert r.tokens_in >= 1
        assert r.tokens_out >= 1
        assert r.usd >= 0.0

    def test_cost_scales_with_tokens(self) -> None:
        c_cheap = FakeLLMClient(response_text="ok", price_per_1k_tokens=0.001)
        c_pricey = FakeLLMClient(response_text="ok", price_per_1k_tokens=0.100)
        cheap = c_cheap.complete(prompt="x" * 400, max_tokens=10)
        pricey = c_pricey.complete(prompt="x" * 400, max_tokens=10)
        assert pricey.usd > cheap.usd

    def test_max_tokens_caps_output(self) -> None:
        client = FakeLLMClient(response_text="x" * 1000)
        r = client.complete(prompt="hi", max_tokens=3)
        assert r.tokens_out <= 3

    def test_no_api_key_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        client = FakeLLMClient()
        r = client.complete(prompt="hi", max_tokens=8)
        assert r.text

    def test_rejects_zero_max_tokens(self) -> None:
        client = FakeLLMClient()
        with pytest.raises(ValueError):
            client.complete(prompt="hi", max_tokens=0)

    def test_rejects_negative_price(self) -> None:
        with pytest.raises(ValueError):
            FakeLLMClient(price_per_1k_tokens=-0.1)

    def test_deterministic_for_same_input(self) -> None:
        client = FakeLLMClient(response_text="canned")
        a = client.complete(prompt="same prompt", max_tokens=50)
        b = client.complete(prompt="same prompt", max_tokens=50)
        assert a == b
