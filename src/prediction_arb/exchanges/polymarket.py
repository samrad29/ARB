from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from prediction_arb.exchanges.base import PredictionMarketExchange
from prediction_arb.exchanges.fees import PolymarketFeeModel
from prediction_arb.http_client import HttpClient
from prediction_arb.logging_utils import log_event
from prediction_arb.models.market import Market
from prediction_arb.models.orderbook import OrderBook, OrderBookLevel
from prediction_arb.money import dollars_to_cents, floor_quantity

logger = logging.getLogger("prediction_arb.exchanges.polymarket")

# Official public APIs. No trading / wallet code in V1.
# Gamma (discovery): https://docs.polymarket.com/market-data/discover-markets
# CLOB (books):     https://docs.polymarket.com/market-data/prices-order-books
DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_URL = "https://clob.polymarket.com"


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


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _category_from_raw(raw: dict[str, Any]) -> str | None:
    tags = raw.get("tags") or raw.get("events") or []
    names: list[str] = []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                names.append(str(tag.get("label") or tag.get("slug") or tag.get("title") or ""))
            else:
                names.append(str(tag))
    for key in ("category", "groupItemTitle"):
        if raw.get(key):
            names.append(str(raw[key]))
    names = [name for name in names if name]
    return names[0] if names else None


def normalize_polymarket_market(raw: dict[str, Any]) -> Market | None:
    market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("condition_id") or "")
    if not market_id:
        return None
    question = raw.get("question") or raw.get("title") or raw.get("slug") or "Polymarket market"
    outcomes = [str(item).lower() for item in _parse_json_list(raw.get("outcomes"))]
    token_ids = [str(item) for item in _parse_json_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))]
    prices = _parse_json_list(raw.get("outcomePrices") or raw.get("outcome_prices"))

    yes_token = None
    no_token = None
    if token_ids:
        if outcomes and "yes" in outcomes and "no" in outcomes:
            yes_token = token_ids[outcomes.index("yes")] if outcomes.index("yes") < len(token_ids) else token_ids[0]
            no_token = token_ids[outcomes.index("no")] if outcomes.index("no") < len(token_ids) else (
                token_ids[1] if len(token_ids) > 1 else None
            )
        else:
            yes_token = token_ids[0]
            no_token = token_ids[1] if len(token_ids) > 1 else None

    last_price = dollars_to_cents(prices[0]) if prices else None
    closed = bool(raw.get("closed"))
    active = raw.get("active")
    if closed:
        status = "closed"
    elif active is False:
        status = "inactive"
    else:
        status = "open"

    events = raw.get("events") or []
    event0 = events[0] if events and isinstance(events[0], dict) else {}
    resolution = (
        raw.get("resolutionSource")
        or raw.get("resolution_source")
        or event0.get("resolutionSource")
        or raw.get("description")
    )
    volume = floor_quantity(raw.get("volumeNum") or raw.get("volume"))
    liquidity = floor_quantity(raw.get("liquidityNum") or raw.get("liquidity"))

    return Market(
        exchange="polymarket",
        exchange_market_id=market_id,
        ticker=raw.get("slug") or raw.get("conditionId") or market_id,
        title=str(question),
        description=raw.get("description"),
        category=_category_from_raw(raw),
        status=status,
        open_time=parse_dt(raw.get("startDate") or raw.get("start_date") or raw.get("createdAt")),
        close_time=parse_dt(raw.get("endDate") or raw.get("end_date") or event0.get("endDate")),
        resolution_time=parse_dt(raw.get("endDate") or raw.get("umaEndDate") or event0.get("endDate")),
        resolution_source=str(resolution) if resolution else None,
        last_price=last_price,
        volume=volume,
        open_interest=liquidity,
        raw_data=raw,
        updated_at=parse_dt(raw.get("updatedAt") or raw.get("updated_at")),
        event_date=parse_dt(raw.get("endDate") or event0.get("endDate")),
        rules_text=raw.get("description"),
        fee_category=_category_from_raw(raw),
        yes_token_id=yes_token,
        no_token_id=no_token,
    )


def normalize_clob_book(
    market_id: str,
    yes_book: dict[str, Any] | None,
    no_book: dict[str, Any] | None,
) -> OrderBook:
    levels: list[OrderBookLevel] = []
    levels.extend(_clob_levels(yes_book, side="yes"))
    levels.extend(_clob_levels(no_book, side="no"))
    return OrderBook(
        exchange="polymarket",
        exchange_market_id=market_id,
        levels=levels,
        raw_data={"yes": yes_book, "no": no_book},
    )


def _clob_levels(book: dict[str, Any] | None, *, side: str) -> list[OrderBookLevel]:
    if not book:
        return []
    levels: list[OrderBookLevel] = []
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    parsed_bids: list[tuple[int, int]] = []
    parsed_asks: list[tuple[int, int]] = []
    for item in bids:
        price = dollars_to_cents(item.get("price"))
        size = floor_quantity(item.get("size"))
        if price is not None and size and size > 0:
            parsed_bids.append((price, size))
    for item in asks:
        price = dollars_to_cents(item.get("price"))
        size = floor_quantity(item.get("size"))
        if price is not None and size and size > 0:
            parsed_asks.append((price, size))
    # Official CLOB docs: bids ascending, asks descending; best is last.
    parsed_bids.sort(key=lambda row: row[0], reverse=True)
    parsed_asks.sort(key=lambda row: row[0])
    for index, (price, size) in enumerate(parsed_bids):
        levels.append(OrderBookLevel(side=side, book_side="bid", price=price, quantity=size, level=index))
    for index, (price, size) in enumerate(parsed_asks):
        levels.append(OrderBookLevel(side=side, book_side="ask", price=price, quantity=size, level=index))
    return levels


class PolymarketExchange(PredictionMarketExchange):
    name = "polymarket"
    fee_model = PolymarketFeeModel()

    def __init__(
        self,
        http: HttpClient,
        gamma_url: str = DEFAULT_GAMMA_URL,
        clob_url: str = DEFAULT_CLOB_URL,
        max_markets: int = 0,
    ) -> None:
        self.http = http
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.max_markets = max_markets
        self._token_index: dict[str, tuple[str | None, str | None]] = {}

    async def get_markets(self) -> list[Market]:
        raw_markets = await self._paginate_markets()
        markets: list[Market] = []
        for raw in raw_markets:
            try:
                market = normalize_polymarket_market(raw)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "polymarket_market_normalize_failed",
                    market_id=raw.get("id"),
                    error=type(exc).__name__,
                )
                continue
            if market is None:
                continue
            if not market.yes_token_id:
                continue
            self._token_index[market.exchange_market_id] = (market.yes_token_id, market.no_token_id)
            markets.append(market)
            if self.max_markets and len(markets) >= self.max_markets:
                break
        log_event(logger, logging.INFO, "polymarket_markets_collected", count=len(markets))
        return markets

    async def get_market(self, market_id: str) -> Market:
        data = await self.http.get_json(f"{self.gamma_url}/markets/{market_id}")
        market = normalize_polymarket_market(data)
        if market is None:
            raise ValueError(f"Polymarket market not found: {market_id}")
        self._token_index[market.exchange_market_id] = (market.yes_token_id, market.no_token_id)
        return market

    async def get_orderbook(self, market_id: str) -> OrderBook:
        yes_token, no_token = await self._tokens_for(market_id)
        yes_book = await self._fetch_clob_book(yes_token) if yes_token else None
        no_book = await self._fetch_clob_book(no_token) if no_token else None
        return normalize_clob_book(market_id, yes_book, no_book)

    async def get_orderbooks(self, market_ids: list[str]) -> dict[str, OrderBook]:
        token_to_market: dict[str, tuple[str, str]] = {}
        payload: list[dict[str, str]] = []
        for market_id in market_ids:
            try:
                yes_token, no_token = await self._tokens_for(market_id)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "polymarket_token_lookup_failed",
                    market_id=market_id,
                    error=type(exc).__name__,
                )
                continue
            if yes_token:
                token_to_market[yes_token] = (market_id, "yes")
                payload.append({"token_id": yes_token})
            if no_token:
                token_to_market[no_token] = (market_id, "no")
                payload.append({"token_id": no_token})

        by_market: dict[str, dict[str, dict[str, Any]]] = {}
        chunk_size = 500
        for i in range(0, len(payload), chunk_size):
            chunk = payload[i : i + chunk_size]
            try:
                data = await self.http.post_json(f"{self.clob_url}/books", json=chunk)
            except Exception as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "polymarket_batch_books_failed",
                    error=type(exc).__name__,
                    chunk_size=len(chunk),
                )
                continue
            for book in data or []:
                token_id = str(book.get("asset_id") or book.get("token_id") or "")
                if token_id not in token_to_market:
                    continue
                market_id, side = token_to_market[token_id]
                by_market.setdefault(market_id, {})[side] = book

        result = {
            market_id: normalize_clob_book(market_id, books.get("yes"), books.get("no"))
            for market_id, books in by_market.items()
        }
        log_event(logger, logging.INFO, "polymarket_orderbooks_collected", count=len(result))
        return result

    async def get_trades(self, market_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        # Last-traded prices are not used for arb. Provided for completeness.
        return []

    async def _paginate_markets(self) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None
        offset = 0
        page_size = 100
        use_keyset = True
        while True:
            try:
                if use_keyset:
                    params: dict[str, Any] = {"closed": "false", "limit": page_size}
                    if cursor:
                        params["after_cursor"] = cursor
                    data = await self.http.get_json(f"{self.gamma_url}/markets/keyset", params=params)
                    page, next_cursor = _extract_keyset_page(data)
                    if not page and offset == 0 and cursor is None:
                        use_keyset = False
                        continue
                    markets.extend(page)
                    cursor = next_cursor
                    if self.max_markets and len(markets) >= self.max_markets:
                        return markets[: self.max_markets]
                    if not cursor or not page:
                        break
                    await asyncio.sleep(0.05)
                else:
                    params = {
                        "closed": "false",
                        "limit": page_size,
                        "offset": offset,
                    }
                    data = await self.http.get_json(f"{self.gamma_url}/markets", params=params)
                    page = data if isinstance(data, list) else list(data.get("markets") or data.get("data") or [])
                    markets.extend(page)
                    if self.max_markets and len(markets) >= self.max_markets:
                        return markets[: self.max_markets]
                    if not page:
                        break
                    offset += len(page)
                    await asyncio.sleep(0.05)
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "polymarket_markets_page_failed",
                    error=type(exc).__name__,
                    offset=offset,
                    keyset=use_keyset,
                )
                if use_keyset:
                    use_keyset = False
                    continue
                break
        return markets

    async def _tokens_for(self, market_id: str) -> tuple[str | None, str | None]:
        if market_id in self._token_index:
            return self._token_index[market_id]
        market = await self.get_market(market_id)
        return market.yes_token_id, market.no_token_id

    async def _fetch_clob_book(self, token_id: str) -> dict[str, Any] | None:
        try:
            return await self.http.get_json(f"{self.clob_url}/book", params={"token_id": token_id})
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "polymarket_book_failed",
                error=type(exc).__name__,
            )
            return None


def _extract_keyset_page(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        return [], None
    page = list(data.get("markets") or data.get("data") or data.get("items") or [])
    cursor = data.get("next_cursor") or data.get("nextCursor")
    return page, cursor
