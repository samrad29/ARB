from __future__ import annotations

from prediction_arb.arbitrage.detector import ArbitrageDetector
from prediction_arb.exchanges.fees import FeeModel
from prediction_arb.models.market import Market


class FlatFee(FeeModel):
    estimated = False

    def __init__(self, cents_per_contract: int) -> None:
        self.cents_per_contract = cents_per_contract

    def calculate_fee(self, market: Market, side: str, price_cents: int, quantity: int) -> int:
        return self.cents_per_contract * quantity

    def notes(self, market: Market) -> str:
        return f"flat {self.cents_per_contract} cents/contract"


def _market(
    exchange: str,
    market_id: str,
    *,
    yes_ask: int | None,
    no_ask: int | None,
    yes_size: int = 100,
    no_size: int = 100,
    last_price: int | None = None,
    title: str = "Test",
) -> Market:
    return Market(
        exchange=exchange,
        exchange_market_id=market_id,
        ticker=market_id,
        title=title,
        status="open",
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_ask_size=yes_size,
        no_ask_size=no_size,
        last_price=last_price,
    )


def test_profitable_yes_47_no_49() -> None:
    detector = ArbitrageDetector()
    yes_market = _market("kalshi", "A", yes_ask=47, no_ask=60, yes_size=500, no_size=500)
    no_market = _market("polymarket", "B", yes_ask=60, no_ask=49, yes_size=500, no_size=200)
    results = detector.detect(yes_market, no_market)
    assert len(results) == 1
    opp = results[0]
    assert opp.max_quantity == 200
    assert opp.gross_profit == 200 * 4
    assert opp.capital_required == 200 * 96
    assert opp.guaranteed_payout == 200 * 100
    assert opp.net_profit == opp.gross_profit  # zero fees
    assert opp.is_executable


def test_no_profit_51_plus_50() -> None:
    detector = ArbitrageDetector()
    results = detector.detect(
        _market("kalshi", "A", yes_ask=51, no_ask=60),
        _market("polymarket", "B", yes_ask=60, no_ask=50),
    )
    assert results == []


def test_break_even_50_plus_50() -> None:
    detector = ArbitrageDetector()
    results = detector.detect(
        _market("kalshi", "A", yes_ask=50, no_ask=60),
        _market("polymarket", "B", yes_ask=60, no_ask=50),
    )
    assert results == []


def test_liquidity_uses_min_size() -> None:
    detector = ArbitrageDetector()
    results = detector.detect(
        _market("kalshi", "A", yes_ask=47, no_ask=60, yes_size=100, no_size=100),
        _market("polymarket", "B", yes_ask=60, no_ask=49, yes_size=100, no_size=20),
    )
    assert len(results) == 1
    assert results[0].max_quantity == 20
    assert results[0].gross_profit == 80


def test_fees_can_remove_executable_arbitrage() -> None:
    detector = ArbitrageDetector()
    results = detector.detect(
        _market("kalshi", "A", yes_ask=47, no_ask=60, yes_size=20, no_size=20),
        _market("polymarket", "B", yes_ask=60, no_ask=49, yes_size=20, no_size=20),
        fee_a=FlatFee(3),
        fee_b=FlatFee(3),
    )
    # Gross 4 cents/contract; fees 6 cents/contract → net negative → no executable arb.
    assert results == []


def test_last_traded_price_is_ignored() -> None:
    detector = ArbitrageDetector()
    results = detector.detect(
        _market("kalshi", "A", yes_ask=60, no_ask=60, last_price=10),
        _market("polymarket", "B", yes_ask=60, no_ask=60, last_price=10),
    )
    assert results == []
