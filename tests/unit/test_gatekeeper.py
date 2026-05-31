"""Unit tests for `debate.shared.gatekeeper` and `debate.shared.ledger`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from debate.sdk.llm_client import FakeLLMClient, LLMResponse
from debate.sdk.search_client import FakeSearchClient, SearchResponse
from debate.shared.gatekeeper import (
    BudgetExceededError,
    BudgetKind,
    Gatekeeper,
    GatekeeperPolicy,
)
from debate.shared.ledger import Ledger


class FakeClock:
    """Hand-controllable monotonic clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t: float = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _generous_policy() -> GatekeeperPolicy:
    return GatekeeperPolicy(
        max_tokens_per_turn=1_000,
        max_tokens_per_debate=1_000_000,
        max_usd_per_debate=100.0,
        max_requests_per_minute=1000,
    )


def _make_gatekeeper(
    policy: GatekeeperPolicy | None = None,
    clock: FakeClock | None = None,
) -> tuple[Gatekeeper, FakeClock]:
    clk = clock if clock is not None else FakeClock()
    gk = Gatekeeper(policy or _generous_policy(), clock=clk)
    return gk, clk


class TestPolicy:
    def test_minimal_valid(self) -> None:
        p = GatekeeperPolicy(
            max_tokens_per_turn=10,
            max_tokens_per_debate=100,
            max_usd_per_debate=1.0,
            max_requests_per_minute=10,
        )
        assert p.max_tokens_per_turn == 10

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_tokens_per_turn", 0),
            ("max_tokens_per_turn", -1),
            ("max_tokens_per_debate", 0),
            ("max_usd_per_debate", 0.0),
            ("max_usd_per_debate", -0.1),
            ("max_requests_per_minute", 0),
        ],
    )
    def test_rejects_non_positive(self, field: str, value: float) -> None:
        kw = {
            "max_tokens_per_turn": 10,
            "max_tokens_per_debate": 100,
            "max_usd_per_debate": 1.0,
            "max_requests_per_minute": 10,
        }
        kw[field] = value
        with pytest.raises(ValidationError):
            GatekeeperPolicy(**kw)

    def test_frozen(self) -> None:
        p = _generous_policy()
        with pytest.raises(ValidationError):
            p.max_tokens_per_turn = 99  # type: ignore[misc]


class TestLedger:
    def test_starts_empty(self) -> None:
        led = Ledger()
        assert led.requests == 0
        assert led.tokens_in == 0
        assert led.tokens_out == 0
        assert led.usd_spent == 0.0
        assert led.total_tokens == 0

    def test_record_increments_all(self) -> None:
        led = Ledger()
        led.record(tokens_in=5, tokens_out=3, usd=0.01, now=0.0)
        assert led.requests == 1
        assert led.tokens_in == 5
        assert led.tokens_out == 3
        assert led.usd_spent == 0.01
        assert led.total_tokens == 8

    def test_requests_in_window_prunes(self) -> None:
        led = Ledger()
        for t in [0.0, 10.0, 20.0, 30.0, 40.0]:
            led.reserve_request(t)
        assert led.requests_in_window(40.0, 25.0) == 3
        assert led.requests_in_window(100.0, 25.0) == 0

    def test_snapshot_shape(self) -> None:
        led = Ledger()
        led.record(tokens_in=1, tokens_out=2, usd=0.5, now=0.0)
        snap = led.snapshot()
        assert snap == {
            "requests": 1,
            "tokens_in": 1,
            "tokens_out": 2,
            "total_tokens": 3,
            "usd_spent": 0.5,
        }

    def test_negative_usage_rejected(self) -> None:
        led = Ledger()
        with pytest.raises(ValueError):
            led.add_usage(tokens_in=-1)


class TestBudgetExceededError:
    def test_kind_propagated(self) -> None:
        e = BudgetExceededError("hi", kind=BudgetKind.RATE_LIMIT)
        assert e.kind is BudgetKind.RATE_LIMIT
        assert isinstance(e, RuntimeError)


class TestCallLLM:
    def test_happy_path_records_usage(self) -> None:
        gk, _ = _make_gatekeeper()
        client = FakeLLMClient(response_text="hello", price_per_1k_tokens=0.01)
        r = gk.call_llm(client, prompt="topic?", max_tokens=20)
        assert isinstance(r, LLMResponse)
        assert gk.ledger.requests == 1
        assert gk.ledger.tokens_in == r.tokens_in
        assert gk.ledger.tokens_out == r.tokens_out
        assert gk.ledger.usd_spent == pytest.approx(r.usd)

    def test_max_tokens_per_turn_pre_check(self) -> None:
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=10,
                max_tokens_per_debate=10_000,
                max_usd_per_debate=10.0,
                max_requests_per_minute=100,
            )
        )
        client = FakeLLMClient()
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_llm(client, prompt="hi", max_tokens=11)
        assert ei.value.kind is BudgetKind.TOKENS_PER_TURN
        assert gk.ledger.requests == 0

    def test_max_tokens_per_turn_post_check(self) -> None:
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=5,
                max_tokens_per_debate=10_000,
                max_usd_per_debate=10.0,
                max_requests_per_minute=100,
            )
        )
        client = FakeLLMClient(response_text="x" * 200)
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_llm(client, prompt="x" * 200, max_tokens=5)
        assert ei.value.kind is BudgetKind.TOKENS_PER_TURN

    def test_cumulative_tokens_per_debate_exceeded(self) -> None:
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=200,
                max_tokens_per_debate=30,
                max_usd_per_debate=10.0,
                max_requests_per_minute=100,
            )
        )
        client = FakeLLMClient(response_text="x" * 40)
        gk.call_llm(client, prompt="x" * 40, max_tokens=20)
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_llm(client, prompt="x" * 40, max_tokens=20)
        assert ei.value.kind is BudgetKind.TOKENS_PER_DEBATE

    def test_usd_per_debate_exceeded(self) -> None:
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=1000,
                max_tokens_per_debate=1_000_000,
                max_usd_per_debate=0.001,
                max_requests_per_minute=100,
            )
        )
        client = FakeLLMClient(response_text="x" * 1000, price_per_1k_tokens=1.0)
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_llm(client, prompt="x" * 1000, max_tokens=200)
        assert ei.value.kind is BudgetKind.USD_PER_DEBATE


class TestRateLimit:
    def test_rate_limit_enforced(self) -> None:
        clock = FakeClock()
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=1000,
                max_tokens_per_debate=1_000_000,
                max_usd_per_debate=100.0,
                max_requests_per_minute=3,
            ),
            clock=clock,
        )
        client = FakeLLMClient()
        for _ in range(3):
            gk.call_llm(client, prompt="hi", max_tokens=10)
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_llm(client, prompt="hi", max_tokens=10)
        assert ei.value.kind is BudgetKind.RATE_LIMIT
        assert gk.ledger.requests == 3

    def test_rate_limit_window_slides(self) -> None:
        clock = FakeClock()
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=1000,
                max_tokens_per_debate=1_000_000,
                max_usd_per_debate=100.0,
                max_requests_per_minute=2,
            ),
            clock=clock,
        )
        client = FakeLLMClient()
        gk.call_llm(client, prompt="hi", max_tokens=5)
        gk.call_llm(client, prompt="hi", max_tokens=5)
        clock.advance(61.0)
        gk.call_llm(client, prompt="hi", max_tokens=5)
        assert gk.ledger.requests == 3


class TestCallSearch:
    def test_happy_path_records_usd_no_tokens(self) -> None:
        gk, _ = _make_gatekeeper()
        client = FakeSearchClient(usd_per_query=0.002)
        r = gk.call_search(client, query="ai ethics")
        assert isinstance(r, SearchResponse)
        assert gk.ledger.requests == 1
        assert gk.ledger.tokens_in == 0
        assert gk.ledger.tokens_out == 0
        assert gk.ledger.usd_spent == pytest.approx(0.002)

    def test_search_counts_toward_rate_limit(self) -> None:
        clock = FakeClock()
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=100,
                max_tokens_per_debate=10_000,
                max_usd_per_debate=10.0,
                max_requests_per_minute=2,
            ),
            clock=clock,
        )
        search = FakeSearchClient()
        llm = FakeLLMClient()
        gk.call_search(search, query="a")
        gk.call_llm(llm, prompt="hi", max_tokens=5)
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_search(search, query="b")
        assert ei.value.kind is BudgetKind.RATE_LIMIT

    def test_search_usd_budget_enforced(self) -> None:
        gk, _ = _make_gatekeeper(
            policy=GatekeeperPolicy(
                max_tokens_per_turn=100,
                max_tokens_per_debate=10_000,
                max_usd_per_debate=0.003,
                max_requests_per_minute=100,
            )
        )
        client = FakeSearchClient(usd_per_query=0.002)
        gk.call_search(client, query="a")
        with pytest.raises(BudgetExceededError) as ei:
            gk.call_search(client, query="b")
        assert ei.value.kind is BudgetKind.USD_PER_DEBATE
