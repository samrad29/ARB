from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from prediction_arb.money import complement_cents, dollars_to_cents, floor_quantity

Side = Literal["yes", "no"]
BookSide = Literal["bid", "ask"]


class OrderBookLevel(BaseModel):
    side: Side
    book_side: BookSide = "bid"
    price: int
    quantity: int
    level: int = 0


class OrderBook(BaseModel):
    """Multi-level order book. Prices are integer cents; quantities are contracts."""

    exchange: str
    exchange_market_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    levels: list[OrderBookLevel] = Field(default_factory=list)
    raw_data: dict[str, Any] = Field(default_factory=dict)

    def levels_for(self, side: Side, book_side: BookSide) -> list[OrderBookLevel]:
        selected = [level for level in self.levels if level.side == side and level.book_side == book_side]
        reverse = book_side == "bid"
        return sorted(selected, key=lambda item: item.price, reverse=reverse)

    def best(self, side: Side, book_side: BookSide) -> OrderBookLevel | None:
        levels = self.levels_for(side, book_side)
        return levels[0] if levels else None

    def best_price(self, side: Side, book_side: BookSide) -> int | None:
        level = self.best(side, book_side)
        return level.price if level else None

    def best_size(self, side: Side, book_side: BookSide) -> int | None:
        level = self.best(side, book_side)
        return level.quantity if level else None

    def top_of_book(self) -> dict[str, int | None]:
        yes_bid = self.best("yes", "bid")
        yes_ask = self.best("yes", "ask")
        no_bid = self.best("no", "bid")
        no_ask = self.best("no", "ask")
        return {
            "yes_bid": yes_bid.price if yes_bid else None,
            "yes_ask": yes_ask.price if yes_ask else None,
            "no_bid": no_bid.price if no_bid else None,
            "no_ask": no_ask.price if no_ask else None,
            "yes_bid_size": yes_bid.quantity if yes_bid else None,
            "yes_ask_size": yes_ask.quantity if yes_ask else None,
            "no_bid_size": no_bid.quantity if no_bid else None,
            "no_ask_size": no_ask.quantity if no_ask else None,
        }

    def apply_binary_complements(self) -> None:
        """Fill missing asks from opposite bids (Kalshi-style binary books)."""
        yes_bid = self.best("yes", "bid")
        no_bid = self.best("no", "bid")
        if yes_bid and not self.best("no", "ask"):
            complement = complement_cents(yes_bid.price)
            if complement is not None:
                self.levels.append(
                    OrderBookLevel(
                        side="no",
                        book_side="ask",
                        price=complement,
                        quantity=yes_bid.quantity,
                        level=0,
                    )
                )
        if no_bid and not self.best("yes", "ask"):
            complement = complement_cents(no_bid.price)
            if complement is not None:
                self.levels.append(
                    OrderBookLevel(
                        side="yes",
                        book_side="ask",
                        price=complement,
                        quantity=no_bid.quantity,
                        level=0,
                    )
                )


def levels_from_pairs(
    pairs: list[tuple[Any, Any]] | None,
    *,
    side: Side,
    book_side: BookSide,
    price_ascending: bool,
) -> list[OrderBookLevel]:
    """Convert [[price, size], ...] arrays into normalized levels.

    Kalshi bids are sorted ascending (best bid last). We re-number after sort
    so level 0 is always the best price for that side/book_side.
    """
    if not pairs:
        return []
    parsed: list[tuple[int, int]] = []
    for item in pairs:
        if not item or len(item) < 2:
            continue
        price = dollars_to_cents(item[0])
        quantity = floor_quantity(item[1])
        if price is None or quantity is None or quantity <= 0:
            continue
        parsed.append((price, quantity))

    # Bids: best = highest price. Asks: best = lowest price.
    # Callers may pass Kalshi's ascending bid arrays; we always re-sort best-first.
    _ = price_ascending
    if book_side == "bid":
        parsed.sort(key=lambda row: row[0], reverse=True)
    else:
        parsed.sort(key=lambda row: row[0])
    return [
        OrderBookLevel(side=side, book_side=book_side, price=price, quantity=quantity, level=index)
        for index, (price, quantity) in enumerate(parsed)
    ]
