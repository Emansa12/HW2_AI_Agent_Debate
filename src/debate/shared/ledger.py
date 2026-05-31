"""Cumulative-usage ledger.

Pure accounting, no policy. The Gatekeeper layers policy on top.

Tracked counters:
    requests    - total successful API calls (LLM + search)
    tokens_in   - cumulative input tokens
    tokens_out  - cumulative output tokens
    usd_spent   - cumulative USD cost

Rate-limit windows are supported via `requests_in_window(now, sec)`,
which is backed by a deque of monotonic timestamps and prunes
expired entries lazily.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Ledger:
    """In-memory usage counter."""

    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd_spent: float = 0.0
    _times: deque[float] = field(default_factory=deque, repr=False, compare=False)

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def reserve_request(self, now: float) -> None:
        """Bump request count and record the timestamp (rate limit).

        Use this *before* the upstream call so the rate-limit slot is
        held even if the call later raises.
        """
        self.requests += 1
        self._times.append(now)

    def add_usage(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        usd: float = 0.0,
    ) -> None:
        """Add to cumulative usage counters. Does NOT touch requests."""
        if tokens_in < 0 or tokens_out < 0 or usd < 0:
            raise ValueError("usage deltas must be non-negative")
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.usd_spent += usd

    def record(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        usd: float = 0.0,
        now: float,
    ) -> None:
        """Convenience: `reserve_request(now)` + `add_usage(...)`."""
        self.reserve_request(now)
        self.add_usage(tokens_in=tokens_in, tokens_out=tokens_out, usd=usd)

    def requests_in_window(self, now: float, window_seconds: float) -> int:
        """Return the number of reserved requests in the last
        `window_seconds`. Old entries are pruned in-place.
        """
        if window_seconds <= 0:
            return 0
        cutoff = now - window_seconds
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        return len(self._times)

    def snapshot(self) -> dict[str, float | int]:
        """Return a flat dict of the counters, safe to log."""
        return {
            "requests": self.requests,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "usd_spent": self.usd_spent,
        }
