from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import MarketMatch
from prediction_arb.models.orderbook import OrderBook
from prediction_arb.collectors.watchlist import Watchlist


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LiveState:
    """Current scanner view. WebSocket updates land here; SQLite is periodic."""

    markets: dict[tuple[str, str], Market] = field(default_factory=dict)
    books: dict[tuple[str, str], OrderBook] = field(default_factory=dict)
    db_ids: dict[tuple[str, str], int] = field(default_factory=dict)
    matches: list[MarketMatch] = field(default_factory=list)
    match_db_ids: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    watchlist: Watchlist = field(default_factory=Watchlist)
    previous_yes_ask: dict[tuple[str, str], int] = field(default_factory=dict)
    dirty_books: set[tuple[str, str]] = field(default_factory=set)
    last_discovery_at: datetime | None = None

    def upsert_market(self, market: Market, db_id: int) -> None:
        key = (market.exchange, market.exchange_market_id)
        if market.yes_ask is not None:
            self.previous_yes_ask[key] = market.yes_ask
        self.markets[key] = market
        self.db_ids[key] = db_id

    def update_book(self, book: OrderBook) -> None:
        key = (book.exchange, book.exchange_market_id)
        book.timestamp = _now()
        self.books[key] = book
        self.dirty_books.add(key)
        market = self.markets.get(key)
        if market is None:
            return
        tob = book.top_of_book()
        if market.yes_ask is not None:
            self.previous_yes_ask[key] = market.yes_ask
        for field_name, value in tob.items():
            setattr(market, field_name, value)

    def take_dirty(self) -> list[tuple[str, str]]:
        keys = list(self.dirty_books)
        self.dirty_books.clear()
        return keys
