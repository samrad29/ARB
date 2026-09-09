from __future__ import annotations

from types import SimpleNamespace

from prediction_arb.collectors.watchlist import (
    TIER_CANDIDATE,
    TIER_HIGH,
    build_watchlist,
    poll_interval_for_tier,
)
from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import MarketMatch


def _market(exchange: str, market_id: str, **kwargs) -> Market:
    defaults = dict(
        exchange=exchange,
        exchange_market_id=market_id,
        ticker=market_id,
        title=f"{exchange} {market_id}",
        status="open",
        yes_ask=47,
        no_ask=54,
        yes_ask_size=200,
        no_ask_size=200,
        volume=1000,
    )
    defaults.update(kwargs)
    return Market(**defaults)


def test_unrelated_markets_are_not_watched() -> None:
    markets = {
        ("kalshi", "A"): _market("kalshi", "A"),
        ("polymarket", "Z"): _market("polymarket", "Z"),
    }
    watch = build_watchlist(
        markets,
        matches=[],
        previous_prices={},
        high_confidence_min=0.82,
        candidate_min=0.55,
        max_high=80,
        max_candidate=120,
        min_liquidity=10,
    )
    assert watch.items == []


def test_high_confidence_liquid_pair_is_high_tier() -> None:
    markets = {
        ("kalshi", "A"): _market("kalshi", "A", yes_ask=47, yes_ask_size=500),
        ("polymarket", "B"): _market("polymarket", "B", no_ask=49, no_ask_size=400),
    }
    match = MarketMatch(
        market_a_exchange="kalshi",
        market_a_id="A",
        market_b_exchange="polymarket",
        market_b_id="B",
        match_type="LIKELY_EQUIVALENT",
        match_score=0.9,
        reason="test",
    )
    watch = build_watchlist(
        markets,
        [match],
        previous_prices={},
        high_confidence_min=0.82,
        candidate_min=0.55,
        max_high=80,
        max_candidate=120,
        min_liquidity=10,
    )
    assert {item.tier for item in watch.items} == {TIER_HIGH}
    assert watch.keys() == {("kalshi", "A"), ("polymarket", "B")}


def test_weak_match_is_candidate_not_high() -> None:
    markets = {
        ("kalshi", "A"): _market("kalshi", "A", yes_ask_size=1, no_ask_size=1, volume=0),
        ("polymarket", "B"): _market("polymarket", "B", yes_ask_size=1, no_ask_size=1, volume=0),
    }
    match = MarketMatch(
        market_a_exchange="kalshi",
        market_a_id="A",
        market_b_exchange="polymarket",
        market_b_id="B",
        match_type="POTENTIAL",
        match_score=0.6,
        reason="test",
    )
    watch = build_watchlist(
        markets,
        [match],
        previous_prices={},
        high_confidence_min=0.82,
        candidate_min=0.55,
        max_high=80,
        max_candidate=120,
        min_liquidity=50,
    )
    assert watch.items
    assert all(item.tier == TIER_CANDIDATE for item in watch.items)


def test_adaptive_intervals() -> None:
    settings = SimpleNamespace(
        poll_interval_seconds=2,
        poll_candidate_seconds=15,
        poll_inactive_seconds=300,
    )
    assert poll_interval_for_tier(TIER_HIGH, settings) == 2
    assert poll_interval_for_tier(TIER_CANDIDATE, settings) == 15
    assert poll_interval_for_tier("inactive", settings) == 300


def test_multiple_book_updates_mark_one_dirty_key() -> None:
    from prediction_arb.collectors.state import LiveState
    from prediction_arb.models.orderbook import OrderBook

    state = LiveState()
    book = OrderBook(exchange="kalshi", exchange_market_id="A")
    state.update_book(book)
    state.update_book(book)
    assert state.take_dirty() == [("kalshi", "A")]
    assert state.take_dirty() == []
