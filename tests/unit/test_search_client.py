"""Unit tests for `debate.sdk.search_client`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    FakeSearchClient,
    SearchClient,
    SearchResponse,
    SearchResult,
)


class TestSearchResultShape:
    def test_minimal_valid(self) -> None:
        r = SearchResult(
            title="hello",
            url="https://example.com/",
            snippet="world",
        )
        assert r.title == "hello"
        assert r.url == "https://example.com/"
        assert r.snippet == "world"

    def test_frozen(self) -> None:
        r = SearchResult(title="t", url="https://x.com", snippet="s")
        with pytest.raises(ValidationError):
            r.title = "x"  # type: ignore[misc]

    def test_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="https://x.com", snippet="s", extra="bad")


class TestUrlValidation:
    def test_accepts_http(self) -> None:
        SearchResult(title="t", url="http://x.com", snippet="s")

    def test_accepts_https(self) -> None:
        SearchResult(title="t", url="https://x.com", snippet="s")

    def test_rejects_javascript_scheme(self) -> None:
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="javascript:alert(1)", snippet="s")

    def test_rejects_relative_url(self) -> None:
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="/path/only", snippet="s")

    def test_rejects_data_url(self) -> None:
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="data:text/html,<x>", snippet="s")


class TestSizeLimits:
    def test_title_is_truncated(self) -> None:
        r = SearchResult(
            title="x" * (MAX_TITLE_CHARS + 100),
            url="https://x.com",
            snippet="s",
        )
        assert len(r.title) == MAX_TITLE_CHARS

    def test_snippet_is_truncated(self) -> None:
        r = SearchResult(
            title="t",
            url="https://x.com",
            snippet="y" * (MAX_SNIPPET_CHARS + 100),
        )
        assert len(r.snippet) == MAX_SNIPPET_CHARS

    def test_url_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchResult(
                title="t",
                url="https://x.com/" + ("z" * (MAX_URL_CHARS + 10)),
                snippet="s",
            )


class TestSanitization:
    def test_control_chars_stripped_from_title(self) -> None:
        r = SearchResult(
            title="hi\x00\x01\x02world",
            url="https://x.com",
            snippet="s",
        )
        assert r.title == "hiworld"

    def test_control_chars_stripped_from_snippet(self) -> None:
        r = SearchResult(
            title="t",
            url="https://x.com",
            snippet="evil\x00snippet",
        )
        assert r.snippet == "evilsnippet"

    def test_tabs_and_newlines_preserved(self) -> None:
        r = SearchResult(
            title="t",
            url="https://x.com",
            snippet="line1\nline2\tend",
        )
        assert "\n" in r.snippet
        assert "\t" in r.snippet

    def test_outer_whitespace_stripped(self) -> None:
        r = SearchResult(
            title="   hi   ",
            url="https://x.com",
            snippet="s",
        )
        assert r.title == "hi"


class TestSearchResponse:
    def test_minimal_valid(self) -> None:
        r = SearchResponse(
            results=[SearchResult(title="t", url="https://x.com", snippet="s")],
            usd=0.001,
        )
        assert len(r.results) == 1

    def test_rejects_too_many_results(self) -> None:
        many = [
            SearchResult(
                title=f"t{i}",
                url=f"https://x.com/{i}",
                snippet="s",
            )
            for i in range(MAX_RESULTS_PER_RESPONSE + 1)
        ]
        with pytest.raises(ValidationError):
            SearchResponse(results=many, usd=0.001)

    def test_negative_usd_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchResponse(results=[], usd=-0.01)


class TestFakeSearchClient:
    def test_satisfies_protocol(self) -> None:
        client = FakeSearchClient()
        assert isinstance(client, SearchClient)

    def test_returns_response(self) -> None:
        client = FakeSearchClient(results_per_query=2)
        resp = client.search("ai ethics")
        assert isinstance(resp, SearchResponse)
        assert len(resp.results) == 2
        for r in resp.results:
            assert r.title
            assert r.url.startswith("https://")
            assert r.snippet
            assert r.url

    def test_no_network_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.delenv(var, raising=False)
        client = FakeSearchClient()
        resp = client.search("topic")
        assert resp.results

    def test_rejects_empty_query(self) -> None:
        client = FakeSearchClient()
        with pytest.raises(ValueError):
            client.search("   ")

    def test_rejects_oversize_n(self) -> None:
        with pytest.raises(ValueError):
            FakeSearchClient(results_per_query=MAX_RESULTS_PER_RESPONSE + 1)

    def test_zero_results_is_allowed(self) -> None:
        client = FakeSearchClient(results_per_query=0)
        resp = client.search("q")
        assert resp.results == []
        assert resp.usd >= 0
