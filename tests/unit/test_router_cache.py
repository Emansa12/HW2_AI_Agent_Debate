"""Unit tests for `debate.shared.router.ToolRouter`."""

from __future__ import annotations

import pytest

from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    SearchClient,
    SearchResponse,
    SearchResult,
)
from debate.shared.gatekeeper import (
    BudgetExceededError,
    BudgetKind,
    Gatekeeper,
    GatekeeperPolicy,
)
from debate.shared.router import ToolRouter


class CountingSearchClient:
    """Search client that records every call it receives.

    Implements the `SearchClient` Protocol but is fully controllable
    for cache-hit tests.
    """

    def __init__(
        self,
        *,
        results_per_query: int = 2,
        usd_per_query: float = 0.001,
    ) -> None:
        self.calls: list[str] = []
        self._n = results_per_query
        self._usd = usd_per_query

    def search(self, query: str) -> SearchResponse:
        self.calls.append(query)
        results = [
            SearchResult(
                title=f"r{i}-{query}",
                url=f"https://ex.com/{i}/{query.replace(' ', '_')}",
                snippet=f"snippet {i} for {query}",
            )
            for i in range(self._n)
        ]
        return SearchResponse(results=results, usd=self._usd)


def _generous_policy() -> GatekeeperPolicy:
    return GatekeeperPolicy(
        max_tokens_per_turn=1000,
        max_tokens_per_debate=1_000_000,
        max_usd_per_debate=100.0,
        max_requests_per_minute=1000,
    )


def _make_router(
    *,
    counting: CountingSearchClient | None = None,
    cache_size: int = 8,
    policy: GatekeeperPolicy | None = None,
) -> tuple[ToolRouter, CountingSearchClient, Gatekeeper]:
    client = counting or CountingSearchClient()
    gk = Gatekeeper(policy or _generous_policy())
    router = ToolRouter(gatekeeper=gk, search_client=client, cache_size=cache_size)
    return router, client, gk


class TestProtocol:
    def test_counting_client_satisfies_protocol(self) -> None:
        assert isinstance(CountingSearchClient(), SearchClient)


class TestBasicSearch:
    def test_returns_results(self) -> None:
        router, client, _ = _make_router()
        results = router.search("hello world")
        assert client.calls == ["hello world"]
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)

    def test_rejects_blank_query(self) -> None:
        router, _, _ = _make_router()
        with pytest.raises(ValueError):
            router.search("   ")


class TestCacheHits:
    def test_repeat_query_does_not_call_client(self) -> None:
        router, client, gk = _make_router()
        a = router.search("ai labels")
        b = router.search("ai labels")
        assert client.calls == ["ai labels"]
        assert a == b
        assert gk.ledger.requests == 1
        assert router.cache_stats == {"hits": 1, "misses": 1, "size": 1}

    def test_cache_hit_skips_gatekeeper_budget(self) -> None:
        """A pure-cache lookup must not consume USD or rate-limit slots."""
        router, client, gk = _make_router()
        router.search("q1")
        usd_after_first = gk.ledger.usd_spent
        reqs_after_first = gk.ledger.requests
        router.search("q1")
        router.search("q1")
        router.search("q1")
        assert gk.ledger.usd_spent == usd_after_first
        assert gk.ledger.requests == reqs_after_first
        assert client.calls == ["q1"]

    def test_case_and_whitespace_normalized(self) -> None:
        router, client, _ = _make_router()
        router.search("AI Ethics")
        router.search("ai   ethics")
        router.search("  ai ethics  ")
        assert client.calls == ["AI Ethics"]

    def test_different_queries_are_separate_entries(self) -> None:
        router, client, _ = _make_router()
        router.search("q1")
        router.search("q2")
        router.search("q3")
        assert client.calls == ["q1", "q2", "q3"]
        assert router.cache_stats["size"] == 3


class TestCacheEviction:
    def test_lru_evicts_oldest(self) -> None:
        router, client, _ = _make_router(cache_size=2)
        router.search("a")
        router.search("b")
        router.search("c")  # evicts "a"
        assert router.cache_size == 2
        router.search("a")  # miss again
        assert client.calls == ["a", "b", "c", "a"]

    def test_recent_access_is_kept(self) -> None:
        router, client, _ = _make_router(cache_size=2)
        router.search("a")
        router.search("b")
        router.search("a")  # touch "a" -> b becomes oldest
        router.search("c")  # evicts "b"
        router.search("a")  # still cached
        router.search("b")  # miss
        assert client.calls == ["a", "b", "c", "b"]


class TestResultLimits:
    def test_router_caps_result_list_length(self) -> None:
        class ManyResults:
            def __init__(self) -> None:
                self.calls = 0

            def search(self, query: str) -> SearchResponse:
                self.calls += 1
                results = [
                    SearchResult(
                        title=f"t{i}",
                        url=f"https://x.com/{i}",
                        snippet="s",
                    )
                    for i in range(MAX_RESULTS_PER_RESPONSE)
                ]
                return SearchResponse(results=results, usd=0.0)

        client = ManyResults()
        gk = Gatekeeper(_generous_policy())
        router = ToolRouter(gatekeeper=gk, search_client=client)
        results = router.search("q")
        assert len(results) == MAX_RESULTS_PER_RESPONSE


class TestRouterGatekeeperIntegration:
    def test_budget_exceeded_propagates(self) -> None:
        tight = GatekeeperPolicy(
            max_tokens_per_turn=10,
            max_tokens_per_debate=10,
            max_usd_per_debate=0.0005,
            max_requests_per_minute=10,
        )
        client = CountingSearchClient(usd_per_query=0.01)
        gk = Gatekeeper(tight)
        router = ToolRouter(gatekeeper=gk, search_client=client)
        with pytest.raises(BudgetExceededError) as ei:
            router.search("q")
        assert ei.value.kind is BudgetKind.USD_PER_DEBATE
        assert client.calls == ["q"]

    def test_clear_cache_forces_re_call(self) -> None:
        router, client, _ = _make_router()
        router.search("q")
        router.clear_cache()
        router.search("q")
        assert client.calls == ["q", "q"]
