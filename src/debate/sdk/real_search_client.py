"""Optional real-provider :class:`SearchClient` (Stage 11).

Implements the same :class:`debate.sdk.search_client.SearchClient`
Protocol as :class:`FakeSearchClient`, but talks to a real HTTP
search provider. The default provider is **Tavily**
(`https://api.tavily.com/search`) which has the simplest /
cheapest free tier among options the HW2 spec allows
(Tavily / Brave Search / SerpAPI).

This module is **opt-in only**:

- The CLI defaults to :class:`FakeSearchClient`;
  :class:`RealSearchClient` is only constructed when the user
  passes ``--real-search``.
- The whole test suite is offline. Real-client tests use
  :class:`httpx.MockTransport` to drive the parse + error paths
  with synthetic responses, so a missing key (or no internet) is
  never a problem for ``pytest``.
- The API key is read from the environment **only**
  (``SEARCH_API_KEY`` is the canonical name; ``TAVILY_API_KEY``
  is accepted as a provider-specific alias). The key is never
  embedded in URLs or logs - it only appears as the
  ``Authorization`` request header passed to ``httpx``.
- The Stage 4 :class:`debate.shared.gatekeeper.Gatekeeper` and
  :class:`debate.shared.router.ToolRouter` still wrap every call.
  Pro / Con never instantiate :class:`RealSearchClient` directly;
  the Supervisor's deny-list strips ``SEARCH_API_KEY`` and
  ``TAVILY_API_KEY`` from the child env so they couldn't even if
  they tried.

The provider response is parsed defensively and routed through the
same :class:`debate.sdk.search_client.SearchResult` /
:class:`SearchResponse` Pydantic models, which means the size-cap
+ control-character sanitisation that already protects
:class:`FakeSearchClient` also protects the real provider's
output.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    SearchResponse,
    SearchResult,
)

DEFAULT_TAVILY_URL: str = "https://api.tavily.com/search"
"""Tavily search endpoint. Override via the ``base_url`` constructor
argument for self-hosted gateways or alternate OpenAI-compatible
search proxies."""

DEFAULT_TIMEOUT_SECONDS: float = 15.0
"""Per-request timeout for the underlying ``httpx`` client."""

DEFAULT_RESULTS_PER_QUERY: int = 5
"""Tavily's ``max_results`` request parameter; capped at
:data:`MAX_RESULTS_PER_RESPONSE` to match :class:`SearchResponse`."""

DEFAULT_USD_PER_QUERY: float = 0.005
"""Best-effort USD cost recorded in the :class:`SearchResponse`.

Tavily does not return a per-call price in the response body, so
we record a small constant that the Stage 4 :class:`Ledger` can
sum. Override via ``usd_per_query`` if your plan / provider has a
known unit cost."""

_KEY_ENV_NAMES: tuple[str, ...] = ("SEARCH_API_KEY", "TAVILY_API_KEY")
"""Environment variables read in priority order. The generic
``SEARCH_API_KEY`` wins so a single ``.env`` works for any
SearchClient implementation; ``TAVILY_API_KEY`` is accepted as a
provider-specific alias."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RealSearchError(RuntimeError):
    """Base class for typed errors raised by :class:`RealSearchClient`.

    All sub-classes carry a ``provider`` attribute so the CLI / Judge
    can render a helpful message without inspecting the cause chain.
    """

    provider: str = "tavily"


class MissingSearchAPIKeyError(RealSearchError):
    """Raised when neither ``SEARCH_API_KEY`` nor ``TAVILY_API_KEY``
    is set in the environment.

    Constructed with ``RealSearchClient.from_env()`` or
    ``RealSearchClient()`` itself when ``api_key`` is omitted.
    """

    def __init__(self, env_names: tuple[str, ...] = _KEY_ENV_NAMES) -> None:
        names = " or ".join(env_names)
        super().__init__(
            f"missing search API key: set {names} in your environment "
            "(e.g. via .env or `--real-search` will fail without it)."
        )


class SearchProviderError(RealSearchError):
    """Raised on non-2xx responses from the upstream search provider.

    The HTTP status and a short reason are included; the API key is
    never echoed back into the message.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"search provider returned HTTP {status_code}: {reason}")


class SearchProviderUnavailableError(RealSearchError):
    """Raised on transport-layer failures (DNS, TLS, connect refused,
    timeouts). Wraps the underlying :class:`httpx.HTTPError`."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"search provider unavailable: {type(cause).__name__}: {cause}")


class SearchProviderResponseError(RealSearchError):
    """Raised when the upstream returns 2xx but the body is not the
    JSON shape we expect (missing ``results``, malformed item, etc.).
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RealSearchClient:
    """Real HTTP-backed :class:`SearchClient` for Tavily.

    Construct via :meth:`from_env` to read the API key from the
    environment, or pass ``api_key=`` directly (tests do this with a
    placeholder + a :class:`httpx.MockTransport`).
    """

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
            # Tests usually inject their own client (e.g. one wired to
            # a MockTransport). When we accept it we do not own it -
            # callers close it.
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
        """Build from environment variables.

        Reads ``SEARCH_API_KEY`` (preferred) or ``TAVILY_API_KEY``
        from ``env`` (defaults to :data:`os.environ`). Raises
        :class:`MissingSearchAPIKeyError` if neither is set.
        """
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

    # -- Context-manager / cleanup -----------------------------------------

    def close(self) -> None:
        """Close the underlying ``httpx`` client if we own it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RealSearchClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- SearchClient.search -----------------------------------------------

    def search(self, query: str) -> SearchResponse:
        """Run one search query through the upstream provider.

        Returns the same :class:`SearchResponse` shape as
        :class:`FakeSearchClient`; the result list is sanitised by
        the Pydantic model on construction (control-character
        stripping, length caps, URL scheme check), so a malicious
        provider cannot escape into the transcript.
        """
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
            # The provider may return JSON, plain text, or HTML on
            # error. We surface a short reason without echoing the
            # request headers (which would leak the bearer key).
            reason = _short_reason(resp)
            raise SearchProviderError(resp.status_code, reason)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SearchProviderResponseError(f"upstream response was not JSON: {exc}") from exc

        return _parse_tavily_payload(payload, usd=self._usd)


# ---------------------------------------------------------------------------
# Helpers (module-level so they are unit-testable in isolation)
# ---------------------------------------------------------------------------


def _short_reason(resp: httpx.Response) -> str:
    """Best-effort short reason string for an error response.

    Never returns request headers or the API key. Truncates the body
    to keep log lines small.
    """
    reason = resp.reason_phrase or ""
    text = (resp.text or "").strip()
    if text:
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        return f"{reason} - {text}" if reason else text
    return reason or "(no reason)"


def _parse_tavily_payload(payload: Any, *, usd: float) -> SearchResponse:
    """Convert a Tavily JSON response into :class:`SearchResponse`.

    Defensive: silently skips items missing ``title`` / ``url``, but
    raises :class:`SearchProviderResponseError` if the top-level
    shape is wrong (no ``results`` array).
    """
    if not isinstance(payload, dict):
        raise SearchProviderResponseError("upstream JSON root must be an object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise SearchProviderResponseError("upstream JSON must contain a 'results' array")

    parsed: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        # Tavily uses ``content`` for the snippet; some Tavily-compatible
        # providers use ``snippet`` directly. Accept both.
        snippet = item.get("snippet")
        if snippet is None:
            snippet = item.get("content", "")

        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(url, str) or not url.strip():
            continue
        if not isinstance(snippet, str):
            snippet = ""

        try:
            parsed.append(SearchResult(title=title, url=url, snippet=snippet))
        except ValueError:
            # Per-item validation failure (e.g. non-http URL) is
            # silently skipped: better to return fewer results than
            # to fail the whole search call on one bad row.
            continue
        if len(parsed) >= MAX_RESULTS_PER_RESPONSE:
            break

    return SearchResponse(results=parsed, usd=usd)
