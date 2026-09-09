from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from prediction_arb.exchanges.base import PredictionMarketExchange
from prediction_arb.exchanges.fees import KalshiFeeModel
from prediction_arb.http_client import HttpClient
from prediction_arb.logging_utils import log_event
from prediction_arb.models.market import Market
from prediction_arb.models.orderbook import OrderBook, levels_from_pairs
from prediction_arb.money import complement_cents, dollars_to_cents, floor_quantity

logger = logging.getLogger("prediction_arb.exchanges.kalshi")

# Official Trade API. Public market data does not require authentication.
# Docs: https://docs.kalshi.com/getting_started/quick_start_market_data
DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

_STATUS_MAP = {
    "initialized": "unopened",
    "inactive": "unopened",
    "active": "open",
    "open": "open",
    "paused": "paused",
    "closed": "closed",
    "determined": "closed",
    "disputed": "closed",
    "amended": "closed",
    "finalized": "settled",
    "settled": "settled",
}


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_kalshi_market(raw: dict[str, Any], event_meta: dict[str, Any] | None = None) -> Market:
    event_meta = event_meta or {}
    yes_bid = dollars_to_cents(raw.get("yes_bid_dollars")) or None
    yes_ask = dollars_to_cents(raw.get("yes_ask_dollars")) or None
    no_bid = dollars_to_cents(raw.get("no_bid_dollars")) or None
    no_ask = dollars_to_cents(raw.get("no_ask_dollars")) or None
    yes_bid_size = floor_quantity(raw.get("yes_bid_size_fp"))
    yes_ask_size = floor_quantity(raw.get("yes_ask_size_fp"))

    if no_ask is None and yes_bid is not None:
        no_ask = complement_cents(yes_bid)
    if yes_ask is None and no_bid is not None:
        yes_ask = complement_cents(no_bid)

    def _nonzero(value: int | None) -> int | None:
        return value if value and value > 0 else None

    yes_bid = _nonzero(yes_bid)
    yes_ask = _nonzero(yes_ask)
    no_bid = _nonzero(no_bid)
    no_ask = _nonzero(no_ask)

    no_ask_size = yes_bid_size
    no_bid_size = yes_ask_size

    event_title = event_meta.get("title")
    yes_title = raw.get("yes_sub_title") or raw.get("title") or raw.get("ticker")
    if event_title and yes_title and event_title not in str(yes_title):
        title = f"{event_title}: {yes_title}"
    else:
        title = str(yes_title or raw.get("ticker") or raw.get("event_ticker") or "Kalshi market")

    description_parts = [
        part
        for part in (
            raw.get("rules_primary"),
            raw.get("rules_secondary"),
            event_meta.get("sub_title"),
        )
        if part
    ]
    native_status = str(raw.get("status") or "")
    return Market(
        exchange="kalshi",
        exchange_market_id=str(raw.get("ticker")),
        ticker=raw.get("ticker"),
        title=title,
        description="\n".join(description_parts) or None,
        category=event_meta.get("category") or raw.get("category"),
        status=_STATUS_MAP.get(native_status.lower(), native_status or "unknown"),
        open_time=parse_dt(raw.get("open_time")),
        close_time=parse_dt(raw.get("close_time")),
        resolution_time=parse_dt(
            raw.get("expected_expiration_time") or raw.get("latest_expiration_time")
        ),
        resolution_source=raw.get("rules_primary") or event_meta.get("settlement_source"),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        no_bid_size=no_bid_size,
        no_ask_size=no_ask_size,
        last_price=dollars_to_cents(raw.get("last_price_dollars")) or None,
        volume=floor_quantity(raw.get("volume_fp")),
        open_interest=floor_quantity(raw.get("open_interest_fp")),
        raw_data=raw,
        updated_at=parse_dt(raw.get("updated_time")),
        event_date=parse_dt(raw.get("occurrence_datetime") or raw.get("close_time")),
        rules_text="\n".join(description_parts) or None,
        fee_category=event_meta.get("category"),
    )


def normalize_kalshi_orderbook(ticker: str, payload: dict[str, Any]) -> OrderBook:
    book_fp = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    yes_dollars = book_fp.get("yes_dollars") or book_fp.get("yes") or []
    no_dollars = book_fp.get("no_dollars") or book_fp.get("no") or []

    levels = []
    # Official docs: arrays are ascending; highest bid is last.
    levels.extend(
        levels_from_pairs(yes_dollars, side="yes", book_side="bid", price_ascending=True)
    )
    levels.extend(
        levels_from_pairs(no_dollars, side="no", book_side="bid", price_ascending=True)
    )
    book = OrderBook(
        exchange="kalshi",
        exchange_market_id=ticker,
        levels=levels,
        raw_data=payload,
    )
    book.apply_binary_complements()
    return book


class KalshiExchange(PredictionMarketExchange):
    name = "kalshi"
    fee_model = KalshiFeeModel()

    def __init__(
        self,
        http: HttpClient,
        base_url: str = DEFAULT_BASE_URL,
        max_markets: int = 0,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.max_markets = max_markets

    async def get_markets(self) -> list[Market]:
        raw_markets = await self._paginate_markets()
        event_tickers = {
            str(raw.get("event_ticker"))
            for raw in raw_markets
            if raw.get("event_ticker")
        }
        events = await self._get_events_by_tickers(event_tickers)
        markets: list[Market] = []
        for raw in raw_markets:
            ticker = raw.get("ticker")
            if not ticker:
                continue
            if str(raw.get("market_type") or "binary").lower() not in {"binary", ""}:
                continue
            if str(ticker).upper().startswith("KXMVE"):
                continue
            try:
                event_meta = events.get(str(raw.get("event_ticker") or ""), {})
                markets.append(normalize_kalshi_market(raw, event_meta))
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "kalshi_market_normalize_failed",
                    ticker=ticker,
                    error=type(exc).__name__,
                )
            if self.max_markets and len(markets) >= self.max_markets:
                break
        log_event(logger, logging.INFO, "kalshi_markets_collected", count=len(markets))
        return markets

    async def get_market(self, market_id: str) -> Market:
        data = await self.http.get_json(f"{self.base_url}/markets/{market_id}")
        raw = data.get("market", data)
        event_ticker = raw.get("event_ticker")
        event_meta = {}
        if event_ticker:
            try:
                event_data = await self.http.get_json(f"{self.base_url}/events/{event_ticker}")
                event = event_data.get("event", event_data)
                event_meta = {
                    "title": event.get("title"),
                    "category": event.get("category"),
                    "sub_title": event.get("sub_title"),
                    "settlement_source": event.get("settlement_source"),
                }
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "kalshi_event_fetch_failed",
                    event_ticker=event_ticker,
                    error=type(exc).__name__,
                )
        return normalize_kalshi_market(raw, event_meta)

    async def get_orderbook(self, market_id: str) -> OrderBook:
        data = await self.http.get_json(f"{self.base_url}/markets/{market_id}/orderbook")
        return normalize_kalshi_orderbook(market_id, data)

    async def get_orderbooks(self, market_ids: list[str]) -> dict[str, OrderBook]:
        books: dict[str, OrderBook] = {}
        chunk_size = 100
        for i in range(0, len(market_ids), chunk_size):
            chunk = market_ids[i : i + chunk_size]
            try:
                params = [("tickers", ticker) for ticker in chunk]
                data = await self.http.get_json(f"{self.base_url}/markets/orderbooks", params=params)
                parsed = _parse_orderbooks_payload(data)
                for ticker, payload in parsed.items():
                    books[ticker] = normalize_kalshi_orderbook(ticker, payload)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "kalshi_batch_orderbook_failed",
                    error=type(exc).__name__,
                    chunk_size=len(chunk),
                )
                for ticker in chunk:
                    try:
                        books[ticker] = await self.get_orderbook(ticker)
                    except Exception as inner:
                        log_event(
                            logger,
                            logging.WARNING,
                            "kalshi_orderbook_failed",
                            ticker=ticker,
                            error=type(inner).__name__,
                        )
        log_event(logger, logging.INFO, "kalshi_orderbooks_collected", count=len(books))
        return books

    async def get_trades(self, market_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        limit = int(kwargs.get("limit") or 100)
        try:
            data = await self.http.get_json(
                f"{self.base_url}/markets/trades",
                params={"ticker": market_id, "limit": limit},
            )
            return list(data.get("trades") or [])
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "kalshi_trades_failed",
                ticker=market_id,
                error=type(exc).__name__,
            )
            return []

    async def _paginate_markets(self) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            # Exclude multivariate/combo contracts; they are not binary YES/NO markets.
            params: dict[str, Any] = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self.http.get_json(f"{self.base_url}/markets", params=params)
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "kalshi_markets_page_failed",
                    error=type(exc).__name__,
                    cursor=bool(cursor),
                )
                break
            page = list(data.get("markets") or [])
            for raw in page:
                ticker = str(raw.get("ticker") or "")
                if not ticker or ticker.upper().startswith("KXMVE"):
                    continue
                if str(raw.get("market_type") or "binary").lower() not in {"binary", ""}:
                    continue
                markets.append(raw)
            cursor = data.get("cursor") or None
            if self.max_markets and len(markets) >= self.max_markets:
                return markets[: self.max_markets]
            if not cursor or not page:
                break
            await asyncio.sleep(0.2)
        return markets

    async def _get_events_by_tickers(self, tickers: set[str]) -> dict[str, dict[str, Any]]:
        """Fetch event metadata only for markets we actually collected.

        Public unauthenticated traffic is easy to 429 if we paginate every
        open event. Cap and serialize a bit so one slow endpoint cannot
        stall the rest of the collector.
        """
        selected = sorted(tickers)[:150]
        semaphore = asyncio.Semaphore(3)
        results: dict[str, dict[str, Any]] = {}

        async def fetch(ticker: str) -> None:
            async with semaphore:
                try:
                    data = await self.http.get_json(f"{self.base_url}/events/{ticker}")
                    event = data.get("event", data)
                    results[ticker] = event
                except Exception as exc:
                    log_event(
                        logger,
                        logging.WARNING,
                        "kalshi_event_fetch_failed",
                        event_ticker=ticker,
                        error=type(exc).__name__,
                    )

        await asyncio.gather(*(fetch(ticker) for ticker in selected))
        return results


def _parse_orderbooks_payload(data: Any) -> dict[str, dict[str, Any]]:
    if not data:
        return {}
    if isinstance(data, dict):
        if "orderbooks" in data:
            items = data["orderbooks"]
            if isinstance(items, dict):
                return {str(k): v for k, v in items.items()}
            parsed: dict[str, dict[str, Any]] = {}
            for item in items or []:
                ticker = item.get("ticker") or item.get("market_ticker")
                if ticker:
                    parsed[str(ticker)] = item
            return parsed
        if "orderbook_fp" in data:
            ticker = data.get("ticker")
            return {str(ticker): data} if ticker else {}
    if isinstance(data, list):
        parsed = {}
        for item in data:
            ticker = item.get("ticker") or item.get("market_ticker")
            if ticker:
                parsed[str(ticker)] = item
        return parsed
    return {}
