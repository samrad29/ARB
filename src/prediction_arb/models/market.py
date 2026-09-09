from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Market(BaseModel):
    """Normalized market. Prices are integer cents; sizes are whole contracts."""

    exchange: str
    exchange_market_id: str
    ticker: str | None = None
    title: str
    description: str | None = None
    category: str | None = None
    status: str
    open_time: datetime | None = None
    close_time: datetime | None = None
    resolution_time: datetime | None = None
    resolution_source: str | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    yes_bid_size: int | None = None
    yes_ask_size: int | None = None
    no_bid_size: int | None = None
    no_ask_size: int | None = None
    last_price: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None

    # Matching / fee helpers (not necessarily persisted as first-class columns)
    event_date: datetime | None = None
    rules_text: str | None = None
    fee_category: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None

    @property
    def is_active(self) -> bool:
        return (self.status or "").lower() in {"open", "active"}
