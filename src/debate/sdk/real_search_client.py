"""Optional real-provider :class:`SearchClient` (Stage 11, Tavily default)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from debate.sdk.search_client import MAX_RESULTS_PER_RESPONSE, SearchResponse
from debate.sdk.search_response_parser import (
    _parse_tavily_payload,
    _short_reason,
)

DEFAULT_TAVILY_URL: str = "https://api.tavily.com/search"
DEFAULT_TIMEOUT_SECONDS: float = 15.0
DEFAULT_RESULTS_PER_QUERY: int = 5
DEFAULT_USD_PER_QUERY: float = 0.005

_KEY_ENV_NAMES: tuple[str, ...] = ("SEARCH_API_KEY", "TAVILY_API_KEY")


class RealSearchError(RuntimeError):
    provider: str = "tavily"


class MissingSearchAPIKeyError(RealSearchError):
    def __init__(self, env_names: tuple[str, ...] = _KEY_ENV_NAMES) -> None:
        names = " or ".join(env_names)
        super().__init__(
            f"missing search API key: set {names} in your environment "
            "(e.g. via .env or `--real-search` will fail without it)."
        )


class SearchProviderError(RealSearchError):
    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"search provider returned HTTP {status_code}: {reason}")


class SearchProviderUnavailableError(RealSearchError):
    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"search provider unavailable: {type(cause).__name__}: {cause}")


class SearchProviderResponseError(RealSearchError):
    """Raised when the upstream returns 2xx but the body is not the expected JSON shape."""


class RealSearchClient:
    """Real HTTP-backed :class:`SearchClient` for Tavily."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_TAVILY_URL,
        results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        usd_per_query: float = DEFAULT_USD_PER_QUERY,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingSearchAPIKeyError()
        if results_per_query < 1:
            raise ValueError("results_per_query must be >= 1")
        if results_per_query > MAX_RESULTS_PER_RESPONSE:
            raise ValueError(
                f"results_per_query must be <= {MAX_RESULTS_PER_RESPONSE} (SearchResponse cap)"
            )
        if usd_per_query < 0:
            raise ValueError("usd_per_query must be >= 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        self._api_key: str = api_key.strip()
        self._base_url: str = base_url
        self._n: int = results_per_query
        self._usd: float = usd_per_query

        if client is not None:
            self._client: httpx.Client = client
            self._owns_client: bool = False
        else:
            self._client = httpx.Client(
                timeout=timeout_seconds,
                transport=transport,
            )
            self._owns_client = True

    @classmethod
    def from_env(
        cls,
        *,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> RealSearchClient:
        source = env if env is not None else os.environ
        api_key: str | None = None
        for name in _KEY_ENV_NAMES:
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                api_key = value.strip()
                break
        if api_key is None:
            raise MissingSearchAPIKeyError()
        return cls(api_key=api_key, **kwargs)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RealSearchClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def search(self, query: str) -> SearchResponse:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        body: dict[str, Any] = {
            "query": query.strip(),
            "max_results": self._n,
            "search_depth": "basic",
        }
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = self._client.post(self._base_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchProviderUnavailableError(exc) from exc

        if resp.status_code >= 400:
            raise SearchProviderError(resp.status_code, _short_reason(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SearchProviderResponseError(f"upstream response was not JSON: {exc}") from exc

        return _parse_tavily_payload(payload, usd=self._usd)
