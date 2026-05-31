"""Gatekeeper: the only path to external LLM and search calls.

The Gatekeeper enforces four budget invariants on every external
call. Violations raise `BudgetExceededError`; usage is always
recorded in the `Ledger`.

Enforced policies:

- `max_tokens_per_turn`     - per-call cap on tokens_in + tokens_out.
                              Pre-checked against the requested
                              `max_tokens` argument; re-checked
                              against the actual response.
- `max_tokens_per_debate`   - cumulative cap on tokens across the
                              whole run.
- `max_usd_per_debate`      - cumulative USD cap.
- `max_requests_per_minute` - sliding-window rate limit on external
                              calls (LLM + search combined).

Search calls don't consume tokens, but they DO consume USD and a
rate-limit slot.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from debate.sdk.llm_client import LLMClient, LLMResponse
from debate.sdk.search_client import SearchClient, SearchResponse
from debate.shared.ledger import Ledger


class BudgetKind(StrEnum):
    TOKENS_PER_TURN = "tokens_per_turn"
    TOKENS_PER_DEBATE = "tokens_per_debate"
    USD_PER_DEBATE = "usd_per_debate"
    RATE_LIMIT = "rate_limit"


class BudgetExceededError(RuntimeError):
    """Raised when a Gatekeeper policy is violated.

    The `kind` attribute identifies which budget was exceeded so
    callers can branch on the specific failure mode.
    """

    def __init__(self, message: str, *, kind: BudgetKind) -> None:
        super().__init__(message)
        self.kind: BudgetKind = kind


class GatekeeperPolicy(BaseModel):
    """Immutable budget envelope enforced by the Gatekeeper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens_per_turn: Annotated[int, Field(ge=1, le=1_000_000)]
    max_tokens_per_debate: Annotated[int, Field(ge=1, le=100_000_000)]
    max_usd_per_debate: Annotated[float, Field(gt=0.0, le=1_000_000.0)]
    max_requests_per_minute: Annotated[int, Field(ge=1, le=100_000)]


_RATE_WINDOW_SECONDS: float = 60.0


class Gatekeeper:
    """Wrapper that enforces budgets around external calls."""

    def __init__(
        self,
        policy: GatekeeperPolicy,
        *,
        ledger: Ledger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.policy: GatekeeperPolicy = policy
        self.ledger: Ledger = ledger if ledger is not None else Ledger()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    def _enforce_rate_limit(self, now: float) -> None:
        in_window = self.ledger.requests_in_window(now, _RATE_WINDOW_SECONDS)
        if in_window >= self.policy.max_requests_per_minute:
            raise BudgetExceededError(
                f"rate limit hit: {in_window} requests in the last "
                f"{int(_RATE_WINDOW_SECONDS)}s (max "
                f"{self.policy.max_requests_per_minute})",
                kind=BudgetKind.RATE_LIMIT,
            )

    def _enforce_cumulative_budgets(self) -> None:
        if self.ledger.total_tokens > self.policy.max_tokens_per_debate:
            raise BudgetExceededError(
                f"debate token budget exceeded: "
                f"{self.ledger.total_tokens} > "
                f"{self.policy.max_tokens_per_debate}",
                kind=BudgetKind.TOKENS_PER_DEBATE,
            )
        if self.ledger.usd_spent > self.policy.max_usd_per_debate:
            raise BudgetExceededError(
                f"debate USD budget exceeded: "
                f"{self.ledger.usd_spent:.4f} > "
                f"{self.policy.max_usd_per_debate:.4f}",
                kind=BudgetKind.USD_PER_DEBATE,
            )

    def call_llm(
        self,
        client: LLMClient,
        *,
        prompt: str,
        max_tokens: int,
    ) -> LLMResponse:
        """Run an LLM completion under budget control."""
        if max_tokens > self.policy.max_tokens_per_turn:
            raise BudgetExceededError(
                f"requested max_tokens={max_tokens} exceeds per-turn cap "
                f"{self.policy.max_tokens_per_turn}",
                kind=BudgetKind.TOKENS_PER_TURN,
            )

        now = self._clock()
        self._enforce_rate_limit(now)
        self.ledger.reserve_request(now)

        response = client.complete(prompt=prompt, max_tokens=max_tokens)
        self.ledger.add_usage(
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            usd=response.usd,
        )

        if response.total_tokens > self.policy.max_tokens_per_turn:
            raise BudgetExceededError(
                f"turn token cap exceeded: "
                f"{response.total_tokens} > "
                f"{self.policy.max_tokens_per_turn}",
                kind=BudgetKind.TOKENS_PER_TURN,
            )
        self._enforce_cumulative_budgets()
        return response

    def call_search(
        self,
        client: SearchClient,
        *,
        query: str,
    ) -> SearchResponse:
        """Run a search under budget control. Search calls don't
        consume tokens, but they DO consume USD and a rate-limit
        slot.
        """
        now = self._clock()
        self._enforce_rate_limit(now)
        self.ledger.reserve_request(now)

        response = client.search(query)
        self.ledger.add_usage(usd=response.usd)

        self._enforce_cumulative_budgets()
        return response
