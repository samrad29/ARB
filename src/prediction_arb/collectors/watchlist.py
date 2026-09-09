from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import MarketMatch
from prediction_arb.money import CONTRACT_PAYOUT_CENTS

HIGH_CONFIDENCE_TYPES = {"EXACT", "LIKELY_EQUIVALENT"}
TIER_HIGH = "high"
TIER_CANDIDATE = "candidate"
TIER_INACTIVE = "inactive"


@dataclass
class WatchItem:
    exchange: str
    market_id: str
    match_score: float
    match_type: str
    counterpart_exchange: str
    counterpart_market_id: str
    priority: float
    tier: str
    liquidity: int = 0
    spread_cents: int | None = None
    edge_cents: int | None = None
    volume: int = 0
    last_moved: bool = False
    next_poll_at: float = 0.0


@dataclass
class Watchlist:
    items: list[WatchItem] = field(default_factory=list)

    def high(self) -> list[WatchItem]:
        return [item for item in self.items if item.tier == TIER_HIGH]

    def candidate(self) -> list[WatchItem]:
        return [item for item in self.items if item.tier == TIER_CANDIDATE]

    def by_exchange(self, name: str) -> list[WatchItem]:
        return [item for item in self.items if item.exchange == name]

    def keys(self) -> set[tuple[str, str]]:
        return {(item.exchange, item.market_id) for item in self.items}

    def interval_for(self, item: WatchItem, settings) -> float:
        return poll_interval_for_tier(item.tier, settings)


def poll_interval_for_tier(tier: str, settings) -> float:
    if tier == TIER_HIGH:
        return settings.poll_interval_seconds
    if tier == TIER_CANDIDATE:
        return settings.poll_candidate_seconds
    return settings.poll_inactive_seconds


def build_watchlist(
    markets: dict[tuple[str, str], Market],
    matches: list[MarketMatch],
    previous_prices: dict[tuple[str, str], int] | None,
    *,
    high_confidence_min: float,
    candidate_min: float,
    max_high: int,
    max_candidate: int,
    min_liquidity: int,
) -> Watchlist:
    """Only matched markets are eligible. Unrelated markets are not monitored."""
    previous_prices = previous_prices or {}
    scored: list[WatchItem] = []
    seen: set[tuple[str, str]] = set()

    for match in matches:
        if match.match_score < candidate_min:
            continue
        left = markets.get((match.market_a_exchange, match.market_a_id))
        right = markets.get((match.market_b_exchange, match.market_b_id))
        if left is None or right is None:
            continue
        for market, other in ((left, right), (right, left)):
            key = (market.exchange, market.exchange_market_id)
            if key in seen:
                continue
            seen.add(key)
            item = _score_item(
                market,
                other,
                match,
                previous_prices,
                high_confidence_min=high_confidence_min,
                min_liquidity=min_liquidity,
            )
            scored.append(item)

    scored.sort(key=lambda item: item.priority, reverse=True)
    high = [item for item in scored if item.tier == TIER_HIGH][:max_high]
    high_keys = {(item.exchange, item.market_id) for item in high}
    candidates = [
        item for item in scored if item.tier == TIER_CANDIDATE and (item.exchange, item.market_id) not in high_keys
    ][:max_candidate]
    return Watchlist(items=high + candidates)


def _score_item(
    market: Market,
    other: Market,
    match: MarketMatch,
    previous_prices: dict[tuple[str, str], int],
    *,
    high_confidence_min: float,
    min_liquidity: int,
) -> WatchItem:
    liquidity = min(market.yes_ask_size or 0, other.no_ask_size or 0)
    liquidity = max(liquidity, min(market.no_ask_size or 0, other.yes_ask_size or 0))
    volume = market.volume or 0
    yes = market.yes_ask
    no = other.no_ask
    edge = None
    spread = None
    if yes is not None and no is not None:
        spread = yes + no
        edge = CONTRACT_PAYOUT_CENTS - spread
    last_price = market.last_price if market.last_price is not None else market.yes_ask
    prev = previous_prices.get((market.exchange, market.exchange_market_id))
    moved = bool(last_price is not None and prev is not None and last_price != prev)

    liq_score = min(liquidity / 500, 1.0)
    activity_score = min(volume / 10_000, 1.0) if volume else (0.4 if (market.yes_ask_size or 0) > 0 else 0.1)
    edge_score = 0.0
    if edge is not None:
        # Closer to (or through) 100¢ cost gets more attention.
        edge_score = min(max(edge + 10, 0) / 20, 1.0)
    move_score = 1.0 if moved else 0.2

    priority = (
        0.40 * match.match_score
        + 0.20 * liq_score
        + 0.15 * activity_score
        + 0.15 * edge_score
        + 0.10 * move_score
    )
    high_match = match.match_type in HIGH_CONFIDENCE_TYPES and match.match_score >= high_confidence_min
    meaningful = liquidity >= min_liquidity or (edge is not None and edge > 0) or moved
    if high_match and meaningful:
        tier = TIER_HIGH
    else:
        tier = TIER_CANDIDATE

    return WatchItem(
        exchange=market.exchange,
        market_id=market.exchange_market_id,
        match_score=match.match_score,
        match_type=match.match_type,
        counterpart_exchange=other.exchange,
        counterpart_market_id=other.exchange_market_id,
        priority=round(priority, 4),
        tier=tier,
        liquidity=liquidity,
        spread_cents=spread,
        edge_cents=edge,
        volume=volume,
        last_moved=moved,
    )


def apply_book_to_market(market: Market) -> None:
    """No-op placeholder imported by tests that patch books onto Market fields."""
    return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
