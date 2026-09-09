from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prediction_arb.exchanges.kalshi import normalize_kalshi_orderbook
from prediction_arb.exchanges.polymarket import normalize_clob_book
from prediction_arb.logging_utils import log_event
from prediction_arb.metrics import MetricsRegistry
from prediction_arb.models.orderbook import OrderBook, OrderBookLevel
from prediction_arb.money import dollars_to_cents, floor_quantity

logger = logging.getLogger("prediction_arb.streams")

POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KALSHI_MARKET_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"


BookHandler = Callable[[OrderBook], Awaitable[None]]


class PolymarketMarketStream:
    """Public CLOB market channel. No auth.

    Docs: https://docs.polymarket.com/market-data/websocket/market-channel
    Heartbeat: send text frame PING every 10 seconds.
    Subscribe: {"assets_ids": [...], "type": "market"}
    """

    def __init__(
        self,
        token_to_market: dict[str, tuple[str, str]] | None = None,
        on_book: BookHandler | None = None,
        metrics: MetricsRegistry | None = None,
        url: str = POLYMARKET_MARKET_WS,
        tokens_provider: Callable[[], dict[str, tuple[str, str]]] | None = None,
    ) -> None:
        self.token_to_market = token_to_market or {}
        self.on_book = on_book
        self.metrics = metrics
        self.url = url
        self.tokens_provider = tokens_provider
        self.connected = False
        self._buffers: dict[str, dict[str, dict[str, Any]]] = {}
        self._stop = asyncio.Event()

    def set_tokens(self, token_to_market: dict[str, tuple[str, str]]) -> None:
        self.token_to_market = token_to_market

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            tokens_map = self.tokens_provider() if self.tokens_provider else self.token_to_market
            self.token_to_market = tokens_map
            tokens = [token for token in tokens_map if token]
            if not tokens:
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
                    self.connected = True
                    if self.metrics:
                        self.metrics.add_ws_connection(1)
                    backoff = 1.0
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    log_event(
                        logger,
                        logging.INFO,
                        "polymarket_ws_connected",
                        tokens=len(tokens),
                    )
                    ping = asyncio.create_task(self._ping(ws))
                    refresh = asyncio.create_task(self._refresh_if_changed(ws, set(tokens)))
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                break
                            await self._handle_message(raw)
                    finally:
                        ping.cancel()
                        refresh.cancel()
                        self.connected = False
                        if self.metrics:
                            self.metrics.add_ws_connection(-1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.metrics:
                    self.metrics.add_ws_reconnect()
                log_event(
                    logger,
                    logging.WARNING,
                    "polymarket_ws_reconnect",
                    error=type(exc).__name__,
                    sleep_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        self._stop.set()

    async def _refresh_if_changed(self, ws: Any, current: set[str]) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            latest = set((self.tokens_provider() if self.tokens_provider else self.token_to_market).keys())
            if latest != current:
                log_event(logger, logging.INFO, "polymarket_ws_resubscribe", previous=len(current), next=len(latest))
                await ws.close()
                return

    async def _ping(self, ws: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(10)
            try:
                await ws.send("PING")
            except Exception:
                return

    async def _handle_message(self, raw: str | bytes) -> None:
        if raw in {"PONG", b"PONG"}:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        events = data if isinstance(data, list) else [data]
        for event in events:
            event_type = event.get("event_type") or event.get("type")
            if event_type == "book":
                await self._apply_book(event)
            elif event_type == "price_change":
                await self._apply_price_change(event)

    async def _apply_book(self, event: dict[str, Any]) -> None:
        token_id = str(event.get("asset_id") or event.get("payload", {}).get("tokenId") or "")
        payload = event.get("payload") or event
        if not token_id and payload.get("tokenId"):
            token_id = str(payload["tokenId"])
        mapping = self.token_to_market.get(token_id)
        if not mapping:
            return
        market_id, side = mapping
        books = self._buffers.setdefault(market_id, {})
        books[side] = {
            "bids": payload.get("bids") or [],
            "asks": payload.get("asks") or [],
        }
        book = normalize_clob_book(market_id, books.get("yes"), books.get("no"))
        await self.on_book(book)

    async def _apply_price_change(self, event: dict[str, Any]) -> None:
        changes = event.get("price_changes") or event.get("payload", {}).get("priceChanges") or []
        for change in changes:
            token_id = str(change.get("asset_id") or change.get("tokenId") or "")
            mapping = self.token_to_market.get(token_id)
            if not mapping:
                continue
            market_id, side = mapping
            best_bid = dollars_to_cents(change.get("best_bid") or change.get("bestBid"))
            best_ask = dollars_to_cents(change.get("best_ask") or change.get("bestAsk"))
            size = floor_quantity(change.get("size")) or 0
            levels: list[OrderBookLevel] = []
            if best_bid:
                levels.append(OrderBookLevel(side=side, book_side="bid", price=best_bid, quantity=max(size, 1), level=0))
            if best_ask:
                levels.append(OrderBookLevel(side=side, book_side="ask", price=best_ask, quantity=max(size, 1), level=0))
            existing = self._buffers.setdefault(market_id, {})
            # Keep the other side's last full book; overlay TOB for this side.
            if side == "yes":
                yes = {"bids": _levels_to_raw(levels, "bid"), "asks": _levels_to_raw(levels, "ask")}
                book = normalize_clob_book(market_id, yes, existing.get("no"))
            else:
                no = {"bids": _levels_to_raw(levels, "bid"), "asks": _levels_to_raw(levels, "ask")}
                book = normalize_clob_book(market_id, existing.get("yes"), no)
            await self.on_book(book)


def _levels_to_raw(levels: list[OrderBookLevel], book_side: str) -> list[dict[str, str]]:
    return [
        {"price": f"{level.price / 100:.4f}", "size": str(level.quantity)}
        for level in levels
        if level.book_side == book_side
    ]


class KalshiOrderbookStream:
    """Authenticated Kalshi orderbook_delta channel.

    Official docs require API-key handshake headers. Public REST market data
    does not; WebSockets do.
    https://docs.kalshi.com/getting_started/quick_start_websockets
    """

    def __init__(
        self,
        tickers: list[str],
        on_book: BookHandler,
        api_key: str,
        private_key_pem: str,
        metrics: MetricsRegistry | None = None,
        url: str = KALSHI_MARKET_WS,
    ) -> None:
        self.tickers = tickers
        self.on_book = on_book
        self.api_key = api_key
        self.private_key_pem = private_key_pem
        self.metrics = metrics
        self.url = url
        self.tickers_provider = None
        self.connected = False
        self._books: dict[str, dict[str, dict[int, int]]] = {}
        self._stop = asyncio.Event()
        self._sid = 1

    def set_tickers(self, tickers: list[str]) -> None:
        self.tickers = tickers

    async def run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            tickers = self.tickers_provider() if self.tickers_provider else self.tickers
            self.tickers = tickers
            if not tickers:
                await asyncio.sleep(1)
                continue
            try:
                headers = _kalshi_ws_headers(self.api_key, self.private_key_pem)
                async with websockets.connect(
                    self.url, additional_headers=headers, close_timeout=5
                ) as ws:
                    self.connected = True
                    if self.metrics:
                        self.metrics.add_ws_connection(1)
                    try:
                        backoff = 1.0
                        await ws.send(
                            json.dumps(
                                {
                                    "id": self._sid,
                                    "cmd": "subscribe",
                                    "params": {
                                        "channels": ["orderbook_delta"],
                                        "market_tickers": self.tickers,
                                    },
                                }
                            )
                        )
                        self._sid += 1
                        log_event(logger, logging.INFO, "kalshi_ws_connected", tickers=len(self.tickers))
                        async for raw in ws:
                            if self._stop.is_set():
                                break
                            await self._handle_message(raw)
                    finally:
                        self.connected = False
                        if self.metrics:
                            self.metrics.add_ws_connection(-1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.metrics:
                    self.metrics.add_ws_reconnect()
                log_event(
                    logger,
                    logging.WARNING,
                    "kalshi_ws_reconnect",
                    error=type(exc).__name__,
                    sleep_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        self._stop.set()

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")
        msg = data.get("msg") or {}
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if msg_type == "orderbook_snapshot" and ticker:
            book = normalize_kalshi_orderbook(ticker, {"orderbook_fp": msg} if "yes_dollars" in msg else msg)
            self._index_from_book(ticker, book)
            await self.on_book(book)
        elif msg_type == "orderbook_delta" and ticker:
            book = self._apply_delta(ticker, msg)
            if book:
                await self.on_book(book)

    def _index_from_book(self, ticker: str, book: OrderBook) -> None:
        levels: dict[str, dict[int, int]] = {"yes": {}, "no": {}}
        for level in book.levels:
            if level.book_side != "bid":
                continue
            levels[level.side][level.price] = level.quantity
        self._books[ticker] = levels

    def _apply_delta(self, ticker: str, msg: dict[str, Any]) -> OrderBook | None:
        side = str(msg.get("side") or msg.get("order_side") or "yes").lower()
        if side not in {"yes", "no"}:
            side = "yes"
        price = dollars_to_cents(msg.get("price_dollars") or msg.get("price"))
        if price is None:
            return None
        delta = floor_quantity(msg.get("delta_fp") or msg.get("delta") or msg.get("quantity")) or 0
        levels = self._books.setdefault(ticker, {"yes": {}, "no": {}})
        current = levels[side].get(price, 0) + delta
        if current <= 0:
            levels[side].pop(price, None)
        else:
            levels[side][price] = current
        from prediction_arb.models.orderbook import OrderBook as OB

        rebuilt_levels: list[OrderBookLevel] = []
        for book_side_name, qty_map in levels.items():
            ranked = sorted(qty_map.items(), key=lambda item: item[0], reverse=True)
            for index, (px, qty) in enumerate(ranked):
                rebuilt_levels.append(
                    OrderBookLevel(side=book_side_name, book_side="bid", price=px, quantity=qty, level=index)
                )
        book = OB(exchange="kalshi", exchange_market_id=ticker, levels=rebuilt_levels, raw_data=msg)
        book.apply_binary_complements()
        return book


def _kalshi_ws_headers(api_key: str, private_key_pem: str) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64

    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{KALSHI_WS_PATH}".encode()
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


def load_private_key_pem(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")
