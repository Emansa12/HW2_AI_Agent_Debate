"""LLM client protocol and an offline fake implementation.

The Gatekeeper (`debate.shared.gatekeeper.Gatekeeper`) wraps every
external LLM call, but the LLM provider itself is plug-able through
the `LLMClient` Protocol below.

Stage 4 ships only the fake client. Real-provider implementations
(OpenAI / Anthropic / etc.) will be added in a later stage and can
be swapped in anywhere a `LLMClient` is accepted - no test ever
needs a real API key.
"""

from __future__ import annotations

from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMResponse(BaseModel):
    """One LLM completion, with usage and price metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Annotated[str, Field(min_length=0, max_length=1_000_000)]
    tokens_in: Annotated[int, Field(ge=0)]
    tokens_out: Annotated[int, Field(ge=0)]
    usd: Annotated[float, Field(ge=0.0)]

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@runtime_checkable
class LLMClient(Protocol):
    """Minimal LLM interface used by the debate."""

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
        """Run a single completion. Implementations must be sync."""
        ...


class FakeLLMClient:
    """Offline, deterministic LLM client.

    Returns the same canned text every time. Token counts are derived
    from a simple `len // 4` approximation so tests can reason about
    usage; USD cost is computed from a configurable per-1k-tokens
    price. No network calls, no API keys.
    """

    def __init__(
        self,
        *,
        response_text: str = "(fake LLM response)",
        price_per_1k_tokens: float = 0.002,
    ) -> None:
        if price_per_1k_tokens < 0:
            raise ValueError("price_per_1k_tokens must be >= 0")
        self._text: str = response_text
        self._price: float = price_per_1k_tokens

    @staticmethod
    def _approx_tokens(s: str) -> int:
        return max(1, len(s) // 4)

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        tokens_in = self._approx_tokens(prompt)
        tokens_out = min(max_tokens, self._approx_tokens(self._text))
        usd = (tokens_in + tokens_out) / 1000.0 * self._price
        return LLMResponse(
            text=self._text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=usd,
        )
