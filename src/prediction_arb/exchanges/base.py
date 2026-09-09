from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from prediction_arb.models.market import Market
from prediction_arb.models.orderbook import OrderBook
from prediction_arb.exchanges.fees import FeeModel, ZeroFeeModel


class PredictionMarketExchange(ABC):
    """Normalized exchange adapter. The arb engine talks only to this interface.

    WebSocket streaming is used for the active watchlist when the exchange
    publishes an official public (or optionally authenticated) market stream.
    """

    name: str
    fee_model: FeeModel = ZeroFeeModel()

    @abstractmethod
    async def get_markets(self) -> list[Market]:
        """Return currently active/open markets."""

    @abstractmethod
    async def get_market(self, market_id: str) -> Market:
        """Return a single market by exchange-native ID."""

    @abstractmethod
    async def get_orderbook(self, market_id: str) -> OrderBook:
        """Return the current multi-level order book."""

    async def get_orderbooks(self, market_ids: list[str]) -> dict[str, OrderBook]:
        """Batch helper. Default implementation fetches sequentially."""
        books: dict[str, OrderBook] = {}
        for market_id in market_ids:
            try:
                books[market_id] = await self.get_orderbook(market_id)
            except Exception:
                continue
        return books

    async def get_trades(self, market_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Optional. V1 does not use last-traded prices for arbitrage."""
        return []

    async def stream_orderbooks(self) -> AsyncIterator[OrderBook]:
        """Optional streaming hook. The collector uses dedicated stream classes."""
        raise NotImplementedError
        yield OrderBook(exchange=self.name, exchange_market_id="")  # pragma: no cover

    async def aclose(self) -> None:
        return None
