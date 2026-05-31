"""Search client protocol and an offline fake implementation.

A `SearchResult` is the canonical shape returned to debate agents.
Sanitization is enforced on construction:

- text fields have ASCII control characters stripped (tab / newline
  are preserved);
- text fields are truncated to safe size caps;
- `url` must be an absolute `http://` or `https://` URL;
- the `MAX_RESULTS_PER_RESPONSE` cap is enforced on the list level
  by `SearchResponse`.

The Gatekeeper (`debate.shared.gatekeeper.Gatekeeper`) wraps every
external search call. The provider itself is plug-able through the
`SearchClient` Protocol. Stage 4 ships only the fake client; no
test ever needs a real network connection.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TITLE_CHARS: int = 256
MAX_URL_CHARS: int = 2048
MAX_SNIPPET_CHARS: int = 1024
MAX_RESULTS_PER_RESPONSE: int = 10


def _strip_controls(s: str) -> str:
    """Remove ASCII control characters except `\\t` and `\\n`."""
    return "".join(c for c in s if c in ("\t", "\n") or ord(c) >= 32)


def _sanitize_text(s: str, max_len: int) -> str:
    return _strip_controls(s).strip()[:max_len]


class SearchResult(BaseModel):
    """One search hit. Sanitized and size-limited on construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
    url: Annotated[str, Field(min_length=7, max_length=MAX_URL_CHARS)]
    snippet: Annotated[str, Field(min_length=0, max_length=MAX_SNIPPET_CHARS)]

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if isinstance(out.get("title"), str):
            out["title"] = _sanitize_text(out["title"], MAX_TITLE_CHARS)
        if isinstance(out.get("snippet"), str):
            out["snippet"] = _sanitize_text(out["snippet"], MAX_SNIPPET_CHARS)
        if isinstance(out.get("url"), str):
            # URLs are NOT truncated - truncation would silently change
            # the resource. Oversized URLs are rejected by the length
            # validator below.
            out["url"] = out["url"].strip()
        return out

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"url must start with http:// or https://: {v!r}")
        return v


class SearchResponse(BaseModel):
    """Wrapper for a list of results plus the USD cost of the call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: Annotated[
        list[SearchResult],
        Field(max_length=MAX_RESULTS_PER_RESPONSE),
    ]
    usd: Annotated[float, Field(ge=0.0)]


@runtime_checkable
class SearchClient(Protocol):
    """Minimal search interface used by the debate."""

    def search(self, query: str) -> SearchResponse: ...


class FakeSearchClient:
    """Offline, deterministic search client.

    Produces a small, configurable number of synthetic results
    derived from the query string. Always cheap (no network, no
    keys), and the results pass through the same `SearchResult`
    sanitization as any real provider.
    """

    def __init__(
        self,
        *,
        results_per_query: int = 3,
        usd_per_query: float = 0.005,
    ) -> None:
        if results_per_query < 0:
            raise ValueError("results_per_query must be >= 0")
        if results_per_query > MAX_RESULTS_PER_RESPONSE:
            raise ValueError(f"results_per_query must be <= {MAX_RESULTS_PER_RESPONSE}")
        if usd_per_query < 0:
            raise ValueError("usd_per_query must be >= 0")
        self._n: int = results_per_query
        self._usd: float = usd_per_query

    def search(self, query: str) -> SearchResponse:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        q = query.strip()
        results = [
            SearchResult(
                title=f"Fake result {i + 1} for {q!r}",
                url=f"https://example.com/q/{i + 1}",
                snippet=(
                    f"Synthetic snippet #{i + 1} discussing {q!r}. "
                    "This is a fake offline result for testing."
                ),
            )
            for i in range(self._n)
        ]
        return SearchResponse(results=results, usd=self._usd)
