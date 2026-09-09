from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MatchType = Literal["EXACT", "LIKELY_EQUIVALENT", "LOGICAL_RELATION", "POTENTIAL"]
OpportunityStatus = Literal["OPEN", "EXPIRED"]


class MarketMatch(BaseModel):
    market_a_exchange: str
    market_a_id: str
    market_b_exchange: str
    market_b_id: str
    match_type: MatchType
    match_score: float
    verified: bool = False
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class ArbitrageOpportunity(BaseModel):
    """Binary YES/NO cross-market opportunity. Cash fields are integer cents."""

    type: str = "BINARY_YES_NO"
    strategy: str
    market_a_exchange: str
    market_a_id: str
    market_b_exchange: str
    market_b_id: str
    yes_exchange: str
    yes_market_id: str
    no_exchange: str
    no_market_id: str
    yes_ask: int
    no_ask: int
    yes_ask_size: int
    no_ask_size: int
    max_quantity: int
    capital_required: int
    guaranteed_payout: int
    gross_profit: int
    estimated_fees: int
    net_profit: int
    roi_bps: int
    fees_are_estimated: bool = True
    fee_notes: str = ""
    match_score: float | None = None
    match_type: str | None = None
    match_reason: str | None = None
    status: OpportunityStatus = "OPEN"
    detected_at: datetime | None = None
    expired_at: datetime | None = None
    duration_seconds: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_executable(self) -> bool:
        return self.net_profit > 0 and self.max_quantity > 0
