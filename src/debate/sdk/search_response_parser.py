"""Parse Tavily search API JSON responses."""

from __future__ import annotations

from typing import Any

import httpx

from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    SearchResponse,
    SearchResult,
)


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
    from debate.sdk.real_search_client import SearchProviderResponseError

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
            continue
        if len(parsed) >= MAX_RESULTS_PER_RESPONSE:
            break

    return SearchResponse(results=parsed, usd=usd)
