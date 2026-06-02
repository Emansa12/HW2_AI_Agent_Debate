"""Offline tests for the optional Stage 11 :class:`RealLLMClient`.

Mirrors :mod:`tests.unit.test_real_search_client` in spirit: every
test wires the client up with a :class:`httpx.MockTransport` so no
real HTTP traffic ever leaves the process, and no
``LLM_API_KEY`` / ``OPENAI_API_KEY`` is ever required to run the
suite.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from debate.sdk.llm_client import LLMClient, LLMResponse
from debate.sdk.llm_response_parser import _parse_chat_completion
from debate.sdk.real_llm_client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMProviderError,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
    MissingLLMAPIKeyError,
    RealLLMClient,
)


def _mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _chat_payload(
    content: str = "hello", *, prompt_tokens: int = 5, completion_tokens: int = 7
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Missing-key path
# ---------------------------------------------------------------------------


class TestMissingKey:
    def test_from_env_raises_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("LLM_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(MissingLLMAPIKeyError) as ei:
            RealLLMClient.from_env()
        assert "LLM_API_KEY" in str(ei.value)
        assert "OPENAI_API_KEY" in str(ei.value)

    def test_from_env_prefers_llm_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_API_KEY", "primary")
        monkeypatch.setenv("OPENAI_API_KEY", "alias")
        client = RealLLMClient.from_env(
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload()))
        )
        try:
            assert client._api_key == "primary"
        finally:
            client.close()

    def test_from_env_falls_back_to_openai_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "alias")
        client = RealLLMClient.from_env(
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload()))
        )
        try:
            assert client._api_key == "alias"
        finally:
            client.close()

    def test_from_env_passes_through_base_url_and_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
        monkeypatch.setenv("OPENAI_MODEL", "llama-3.1-70b")
        client = RealLLMClient.from_env(
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload()))
        )
        try:
            assert client._base_url == "https://api.groq.com/openai/v1"
            assert client._model == "llama-3.1-70b"
        finally:
            client.close()

    def test_blank_api_key_raises(self) -> None:
        with pytest.raises(MissingLLMAPIKeyError):
            RealLLMClient(api_key="   ")


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_authorization_header_set_and_key_not_in_url_or_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json=_chat_payload())

        client = RealLLMClient(
            api_key="sk-real-llm-test",
            transport=_mock_transport(handler),
        )
        try:
            client.complete(prompt="hi", max_tokens=10)
        finally:
            client.close()
        assert captured["headers"]["authorization"] == "Bearer sk-real-llm-test"
        assert "sk-real-llm-test" not in captured["url"]
        assert "sk-real-llm-test" not in captured["body"]

    def test_request_targets_chat_completions_endpoint(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_chat_payload())

        client = RealLLMClient(api_key="k", transport=_mock_transport(handler))
        try:
            client.complete(prompt="x", max_tokens=10)
        finally:
            client.close()
        assert captured["url"] == f"{DEFAULT_BASE_URL}/chat/completions"

    def test_body_includes_model_messages_and_temperature(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_chat_payload())

        client = RealLLMClient(
            api_key="k",
            model="gpt-4o-mini",
            temperature=0.7,
            transport=_mock_transport(handler),
        )
        try:
            client.complete(prompt="hi there", max_tokens=42)
        finally:
            client.close()
        assert captured["body"]["model"] == "gpt-4o-mini"
        assert captured["body"]["messages"] == [{"role": "user", "content": "hi there"}]
        assert captured["body"]["max_tokens"] == 42
        assert captured["body"]["temperature"] == 0.7

    def test_default_model_is_gpt4o_mini(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload())),
        )
        try:
            assert client._model == DEFAULT_MODEL
            assert client._model == "gpt-4o-mini"
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    def test_happy_path_returns_llm_response(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(
                lambda _r: httpx.Response(200, json=_chat_payload("the answer"))
            ),
        )
        try:
            resp = client.complete(prompt="q", max_tokens=20)
        finally:
            client.close()
        assert isinstance(resp, LLMResponse)
        assert resp.text == "the answer"
        assert resp.tokens_in == 5
        assert resp.tokens_out == 7
        assert resp.total_tokens == 12
        assert resp.usd > 0

    def test_usd_uses_input_and_output_pricing(self) -> None:
        # 1000 input tokens at $0.001/1K = $0.001
        # 1000 output tokens at $0.004/1K = $0.004
        # total = $0.005
        client = RealLLMClient(
            api_key="k",
            price_per_1k_input_usd=0.001,
            price_per_1k_output_usd=0.004,
            transport=_mock_transport(
                lambda _r: httpx.Response(
                    200,
                    json=_chat_payload(prompt_tokens=1000, completion_tokens=1000),
                )
            ),
        )
        try:
            resp = client.complete(prompt="q", max_tokens=2000)
        finally:
            client.close()
        assert abs(resp.usd - 0.005) < 1e-9

    def test_missing_usage_block_yields_zero_tokens(self) -> None:
        payload = _chat_payload()
        del payload["usage"]
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=payload)),
        )
        try:
            resp = client.complete(prompt="q", max_tokens=1)
        finally:
            client.close()
        assert resp.tokens_in == 0
        assert resp.tokens_out == 0
        assert resp.usd == 0

    def test_root_must_be_object(self) -> None:
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion([])

    def test_choices_must_be_non_empty(self) -> None:
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion({"choices": []})

    def test_choices_first_must_be_object(self) -> None:
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion({"choices": [123]})

    def test_message_content_must_be_string(self) -> None:
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion(
                {"choices": [{"message": {"role": "assistant", "content": None}}]}
            )

    def test_negative_token_counts_rejected(self) -> None:
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": -1, "completion_tokens": 1},
                }
            )

    def test_bool_token_counts_rejected(self) -> None:
        # ``True`` is an ``int`` subclass; we explicitly reject so a
        # weird upstream cannot turn ``True`` into 1 token silently.
        with pytest.raises(LLMProviderResponseError):
            _parse_chat_completion(
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": True, "completion_tokens": 1},
                }
            )

    def test_non_json_body_raises_response_error(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(
                lambda _r: httpx.Response(
                    200, content=b"<html>not json</html>", headers={"Content-Type": "text/html"}
                )
            ),
        )
        try:
            with pytest.raises(LLMProviderResponseError):
                client.complete(prompt="q", max_tokens=1)
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_401_maps_to_provider_error(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(
                lambda _r: httpx.Response(401, json={"error": {"message": "bad key"}})
            ),
        )
        try:
            with pytest.raises(LLMProviderError) as ei:
                client.complete(prompt="q", max_tokens=1)
        finally:
            client.close()
        assert ei.value.status_code == 401
        # API key never echoed in error.
        assert "Bearer" not in str(ei.value)

    def test_429_rate_limit_maps_to_provider_error(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(429, text="Too Many Requests")),
        )
        try:
            with pytest.raises(LLMProviderError) as ei:
                client.complete(prompt="q", max_tokens=1)
        finally:
            client.close()
        assert ei.value.status_code == 429

    def test_transport_error_maps_to_unavailable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS lookup failed")

        client = RealLLMClient(api_key="k", transport=_mock_transport(handler))
        try:
            with pytest.raises(LLMProviderUnavailableError):
                client.complete(prompt="q", max_tokens=1)
        finally:
            client.close()

    def test_complete_rejects_zero_max_tokens(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload())),
        )
        try:
            with pytest.raises(ValueError, match=">= 1"):
                client.complete(prompt="q", max_tokens=0)
        finally:
            client.close()


# ---------------------------------------------------------------------------
# LLMClient Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_real_client_satisfies_llm_client_protocol(self) -> None:
        client = RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload())),
        )
        try:
            assert isinstance(client, LLMClient)
        finally:
            client.close()

    def test_context_manager_closes_client(self) -> None:
        with RealLLMClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_chat_payload())),
        ) as client:
            client.complete(prompt="q", max_tokens=1)
