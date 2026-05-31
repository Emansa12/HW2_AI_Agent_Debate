"""ToolRouter: single entry point for tool calls.

Stage 4 supports one tool: `search`. Every search request goes
through the Gatekeeper (budget + rate-limit enforcement) and the
result is then stashed in a small LRU cache. A cache hit short-
circuits the Gatekeeper entirely - the underlying `SearchClient`
is not called again, no budget is consumed, no rate-limit slot is
used.

The cache key is the normalized query string (case-folded, internal
whitespace collapsed). Cache size is configurable; default 128.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Final

from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    SearchClient,
    SearchResult,
)
from debate.shared.gatekeeper import Gatekeeper

DEFAULT_CACHE_SIZE: Final[int] = 128


class _LRUCache:
    """Tiny LRU keyed by normalized query string."""

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("cache max_size must be >= 1")
        self._max: int = max_size
        self._data: OrderedDict[str, list[SearchResult]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str) -> list[SearchResult] | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: list[SearchResult]) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            self._data[key] = value
            return
        if len(self._data) >= self._max:
            self._data.popitem(last=False)
        self._data[key] = value

    def clear(self) -> None:
        self._data.clear()


class ToolRouter:
    """Single entry point for tool calls (Stage 4: search only)."""

    def __init__(
        self,
        *,
        gatekeeper: Gatekeeper,
        search_client: SearchClient,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._gk: Gatekeeper = gatekeeper
        self._search_client: SearchClient = search_client
        self._cache: _LRUCache = _LRUCache(max_size=cache_size)
        self._hits: int = 0
        self._misses: int = 0

    @staticmethod
    def _normalize_key(query: str) -> str:
        return " ".join(query.lower().split())

    def search(self, query: str) -> list[SearchResult]:
        """Return at most `MAX_RESULTS_PER_RESPONSE` results.

        On a cache hit, the underlying `SearchClient` is not called
        and no Gatekeeper budget is consumed.
        """
        key = self._normalize_key(query)
        if not key:
            raise ValueError("query must contain non-whitespace text")

        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return list(cached)

        self._misses += 1
        response = self._gk.call_search(self._search_client, query=query)
        results: list[SearchResult] = list(response.results)[:MAX_RESULTS_PER_RESPONSE]
        self._cache.set(key, results)
        return list(results)

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def cache_stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": len(self._cache),
        }

    def clear_cache(self) -> None:
        self._cache.clear()
