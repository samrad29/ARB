from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import ROUND_CEILING, Decimal

from prediction_arb.models.market import Market
from prediction_arb.money import dollars_to_cents_ceiling


class FeeModel(ABC):
    """Exchange-specific taker fee. Returns integer cents.

    Fees are marked estimated when series/category multipliers are uncertain.
    """

    estimated: bool = True

    @abstractmethod
    def calculate_fee(self, market: Market, side: str, price_cents: int, quantity: int) -> int:
        """Taker fee in integer cents for buying `quantity` contracts at `price_cents`."""

    def notes(self, market: Market) -> str:
        return "Fees are estimated from the published schedule and may differ at match time."


class ZeroFeeModel(FeeModel):
    estimated = False

    def calculate_fee(self, market: Market, side: str, price_cents: int, quantity: int) -> int:
        return 0

    def notes(self, market: Market) -> str:
        return "No fee model configured."


class KalshiFeeModel(FeeModel):
    """Kalshi general taker fee (July 7, 2026 schedule).

    fees = round_up(M * 0.07 * C * P * (1 - P))
    P = price in dollars, C = contracts, M = series multiplier (default 1).

    Series-specific multipliers and maker fees are not applied in V1, so
    results are marked estimated.
    Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf
    """

    estimated = True
    TAKER_COEFFICIENT = Decimal("0.07")

    def calculate_fee(self, market: Market, side: str, price_cents: int, quantity: int) -> int:
        if quantity <= 0 or price_cents <= 0:
            return 0
        price = Decimal(price_cents) / Decimal("100")
        if price >= 1:
            return 0
        raw = self.TAKER_COEFFICIENT * Decimal(quantity) * price * (Decimal("1") - price)
        # Official schedule rounds so fee+positionCost is a centicent ($0.0001).
        centicents = raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
        return dollars_to_cents_ceiling(centicents)

    def notes(self, market: Market) -> str:
        return (
            "Kalshi taker fee ≈ round_up(0.07 * C * P * (1-P)); "
            "series multiplier assumed 1 (estimated)."
        )


POLYMARKET_TAKER_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "general": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": Decimal("0"),
    "geopolitical": Decimal("0"),
    "world": Decimal("0"),
}


class PolymarketFeeModel(FeeModel):
    """Polymarket taker fee: fee = C * feeRate * p * (1 - p).

    Makers pay 0. Arbitrage that lifts asks is a taker.
    Source: https://docs.polymarket.com/trading/fees
    Category mapping is best-effort, so fees are estimated.
    """

    estimated = True
    DEFAULT_RATE = Decimal("0.05")

    def rate_for(self, market: Market) -> Decimal:
        category = (market.fee_category or market.category or "").strip().lower()
        if not category:
            return self.DEFAULT_RATE
        for key, rate in POLYMARKET_TAKER_RATES.items():
            if key in category:
                return rate
        return self.DEFAULT_RATE

    def calculate_fee(self, market: Market, side: str, price_cents: int, quantity: int) -> int:
        if quantity <= 0 or price_cents <= 0:
            return 0
        price = Decimal(price_cents) / Decimal("100")
        if price >= 1:
            return 0
        rate = self.rate_for(market)
        raw = Decimal(quantity) * rate * price * (Decimal("1") - price)
        rounded = raw.quantize(Decimal("0.00001"))
        return dollars_to_cents_ceiling(rounded)

    def notes(self, market: Market) -> str:
        rate = self.rate_for(market)
        return (
            f"Polymarket taker fee = C * {rate} * p * (1-p) "
            f"(category={market.fee_category or market.category or 'unknown'}; estimated)."
        )
