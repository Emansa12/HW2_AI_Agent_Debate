"""Offline tests for the optional Stage 11 :class:`RealSearchClient`.

The test suite never makes a real HTTP call. Every test wires the
client up with a :class:`httpx.MockTransport` (or pre-built
:class:`httpx.Client`), so this whole file runs without an internet
connection or a ``SEARCH_API_KEY`` / ``TAVILY_API_KEY``.

The tests cover the four behavioural promises the spec makes:

1. Missing-key error - ``MissingSearchAPIKeyError`` with a helpful
   message;
2. Headers - ``Authorization: Bearer <key>`` is sent and the key
   never appears in the URL or body;
3. Parse - a Tavily-shaped JSON response maps cleanly into
   :class:`SearchResponse` (sanitisation, size cap, URL scheme);
4. Errors - HTTP non-2xx and transport failures map to typed
   subclasses of :class:`RealSearchError`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from debate.sdk.real_search_client import (
    DEFAULT_TAVILY_URL,
    MissingSearchAPIKeyError,
    RealSearchClient,
    SearchProviderError,
    SearchProviderResponseError,
    SearchProviderUnavailableError,
)
from debate.sdk.search_client import SearchClient, SearchResponse
from debate.sdk.search_response_parser import _parse_tavily_payload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_payload(n: int = 3) -> dict[str, Any]:
    return {
        "query": "test",
        "results": [
            {
                "title": f"Result {i}",
                "url": f"https://example.com/r/{i}",
                "content": f"Snippet number {i} about something interesting.",
            }
            for i in range(1, n + 1)
        ],
    }


# ---------------------------------------------------------------------------
# Missing-key path
# ---------------------------------------------------------------------------


class TestMissingKey:
    def test_from_env_raises_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("SEARCH_API_KEY", "TAVILY_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(MissingSearchAPIKeyError) as ei:
            RealSearchClient.from_env()
        assert "SEARCH_API_KEY" in str(ei.value)
        assert "TAVILY_API_KEY" in str(ei.value)

    def test_from_env_prefers_search_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_API_KEY", "primary-key")
        monkeypatch.setenv("TAVILY_API_KEY", "alias-key")
        client = RealSearchClient.from_env(transport=_mock_transport(_always_ok))
        try:
            assert client._api_key == "primary-key"
        finally:
            client.close()

    def test_from_env_falls_back_to_tavily_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEARCH_API_KEY", raising=False)
        monkeypatch.setenv("TAVILY_API_KEY", "alias-key")
        client = RealSearchClient.from_env(transport=_mock_transport(_always_ok))
        try:
            assert client._api_key == "alias-key"
        finally:
            client.close()

    def test_blank_api_key_raises(self) -> None:
        with pytest.raises(MissingSearchAPIKeyError):
            RealSearchClient(api_key="   ")

    def test_empty_string_via_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEARCH_API_KEY", "")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with pytest.raises(MissingSearchAPIKeyError):
            RealSearchClient.from_env()


def _always_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_ok_payload(1))


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_authorization_header_set_and_key_not_in_url(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json=_ok_payload(1))

        client = RealSearchClient(
            api_key="sk-tavily-test-key",
            transport=_mock_transport(handler),
        )
        try:
            client.search("hello world")
        finally:
            client.close()

        assert captured["headers"]["authorization"] == "Bearer sk-tavily-test-key"
        # Key MUST NOT appear in URL or in the JSON body (Tavily's
        # legacy ``api_key`` field is intentionally not used).
        assert "sk-tavily-test-key" not in captured["url"]
        assert "sk-tavily-test-key" not in captured["body"]

    def test_request_targets_default_tavily_url(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json=_ok_payload(1))

        client = RealSearchClient(api_key="k", transport=_mock_transport(handler))
        try:
            client.search("q")
        finally:
            client.close()
        assert captured["url"] == DEFAULT_TAVILY_URL

    def test_request_body_includes_query_and_max_results(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_payload(1))

        client = RealSearchClient(
            api_key="k",
            results_per_query=4,
            transport=_mock_transport(handler),
        )
        try:
            client.search("  spaced query  ")
        finally:
            client.close()
        assert captured["body"]["query"] == "spaced query"
        assert captured["body"]["max_results"] == 4


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestResponseParsing:
    def test_happy_path_returns_search_response(self) -> None:
        client = RealSearchClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_ok_payload(3))),
        )
        try:
            resp = client.search("hi")
        finally:
            client.close()
        assert isinstance(resp, SearchResponse)
        assert len(resp.results) == 3
        assert resp.results[0].title == "Result 1"
        assert resp.results[0].url == "https://example.com/r/1"
        assert "Snippet number 1" in resp.results[0].snippet
        assert resp.usd > 0

    def test_results_are_capped_at_response_max(self) -> None:
        # Provider returns 50 results; we cap at the response max
        # (10) regardless of ``results_per_query``.
        client = RealSearchClient(
            api_key="k",
            results_per_query=10,
            transport=_mock_transport(lambda _r: httpx.Response(200, json=_ok_payload(50))),
        )
        try:
            resp = client.search("hi")
        finally:
            client.close()
        assert len(resp.results) <= 10

    def test_skip_items_with_missing_url_or_title(self) -> None:
        payload = {
            "results": [
                {"title": "Good", "url": "https://example.com/", "content": "ok"},
                {"title": "", "url": "https://example.com/empty-title"},
                {"url": "https://example.com/no-title", "content": "x"},
                {"title": "no url", "content": "x"},
                {"title": "bad scheme", "url": "ftp://example.com/x", "content": "x"},
            ]
        }
        resp = _parse_tavily_payload(payload, usd=0.001)
        assert len(resp.results) == 1
        assert resp.results[0].title == "Good"

    def test_accepts_snippet_alias(self) -> None:
        payload = {
            "results": [
                {
                    "title": "Snippet alias",
                    "url": "https://example.com/",
                    "snippet": "uses snippet field directly",
                }
            ]
        }
        resp = _parse_tavily_payload(payload, usd=0.001)
        assert resp.results[0].snippet == "uses snippet field directly"

    def test_sanitises_control_chars_in_provider_response(self) -> None:
        payload = {
            "results": [
                {
                    "title": "Title with \x07 bell",
                    "url": "https://example.com/x",
                    "content": "snippet with\x00null byte",
                }
            ]
        }
        resp = _parse_tavily_payload(payload, usd=0.0)
        assert "\x07" not in resp.results[0].title
        assert "\x00" not in resp.results[0].snippet

    def test_root_must_be_object(self) -> None:
        with pytest.raises(SearchProviderResponseError):
            _parse_tavily_payload([], usd=0.0)

    def test_results_field_must_be_list(self) -> None:
        with pytest.raises(SearchProviderResponseError):
            _parse_tavily_payload({"results": "not a list"}, usd=0.0)

    def test_non_json_body_raises_response_error(self) -> None:
        client = RealSearchClient(
            api_key="k",
            transport=_mock_transport(
                lambda _r: httpx.Response(
                    200, content=b"not-json", headers={"Content-Type": "text/plain"}
                )
            ),
        )
        try:
            with pytest.raises(SearchProviderResponseError):
                client.search("q")
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_4xx_maps_to_provider_error(self) -> None:
        client = RealSearchClient(
            api_key="k",
            transport=_mock_transport(
                lambda _r: httpx.Response(401, json={"error": "invalid key"})
            ),
        )
        try:
            with pytest.raises(SearchProviderError) as ei:
                client.search("q")
        finally:
            client.close()
        assert ei.value.status_code == 401
        # The key MUST NOT appear in the error message.
        assert "k" not in str(ei.value).split(":", 1)[0]
        # And we should not echo a request header in the message.
        assert "Bearer" not in str(ei.value)

    def test_5xx_maps_to_provider_error(self) -> None:
        client = RealSearchClient(
            api_key="k",
            transport=_mock_transport(lambda _r: httpx.Response(503, text="upstream busy")),
        )
        try:
            with pytest.raises(SearchProviderError) as ei:
                client.search("q")
        finally:
            client.close()
        assert ei.value.status_code == 503

    def test_transport_error_maps_to_unavailable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS lookup failed")

        client = RealSearchClient(api_key="k", transport=_mock_transport(handler))
        try:
            with pytest.raises(SearchProviderUnavailableError):
                client.search("q")
        finally:
            client.close()

    def test_search_rejects_blank_query(self) -> None:
        client = RealSearchClient(api_key="k", transport=_mock_transport(_always_ok))
        try:
            with pytest.raises(ValueError, match="non-empty"):
                client.search("   ")
        finally:
            client.close()


# ---------------------------------------------------------------------------
# SearchClient Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_real_client_satisfies_search_client_protocol(self) -> None:
        client = RealSearchClient(api_key="k", transport=_mock_transport(_always_ok))
        try:
            assert isinstance(client, SearchClient)
        finally:
            client.close()

    def test_context_manager_closes_client(self) -> None:
        with RealSearchClient(api_key="k", transport=_mock_transport(_always_ok)) as client:
            client.search("q")
        # No assertion needed; if .close() blew up, the test would
        # already have failed.


# ---------------------------------------------------------------------------
# Integration with ToolRouter + Gatekeeper (still mocked)
# ---------------------------------------------------------------------------


class TestRouterIntegration:
    def test_tool_router_caches_real_search_results(self) -> None:
        from debate.shared.gatekeeper import Gatekeeper, GatekeeperPolicy
        from debate.shared.router import ToolRouter

        call_count = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=_ok_payload(1))

        client = RealSearchClient(api_key="k", transport=_mock_transport(handler))
        gk = Gatekeeper(
            GatekeeperPolicy(
                max_tokens_per_turn=100,
                max_tokens_per_debate=10_000,
                max_usd_per_debate=1.0,
                max_requests_per_minute=60,
            )
        )
        try:
            router = ToolRouter(gatekeeper=gk, search_client=client)
            r1 = router.call("search", query="abc")
            r2 = router.call("search", query="abc")
        finally:
            client.close()
        # Cache hit means second call did not re-issue HTTP.
        assert call_count["n"] == 1
        assert r1 == r2
