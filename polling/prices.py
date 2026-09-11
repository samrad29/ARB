"""Live price polling for matched moneylines."""

from __future__ import annotations

from util import KALSHI, POLY, POLY_CLOB, get_json, maybe_json, parse_price
from markets.nfl.fetch import team_id as nfl_team_id
from markets.cfp_moneyline.fetch import team_id as cfb_team_id
from markets.tennis import player_id as tennis_player_id


def team_id(sport: str, text: str | None) -> str | None:
    if sport == "nfl":
        return nfl_team_id(text)
    if sport == "tennis":
        return tennis_player_id(text)
    return cfb_team_id(text)


def kalshi_prices(event_id: str | None, ticker_a: str | None, ticker_b: str | None) -> dict:
    """Return yes asks for team A / B tickers. One event call when possible."""
    by_ticker: dict[str, dict] = {}
    if event_id:
        try:
            data = get_json(f"{KALSHI}/events/{event_id}", {"with_nested_markets": "true"})
        except Exception:
            data = {}
        event = data.get("event") if isinstance(data, dict) else None
        markets = (event or data or {}).get("markets") if isinstance(event or data, dict) else None
        if not markets and isinstance(data, dict):
            markets = data.get("markets")
        for market in markets or []:
            ticker = str(market.get("ticker") or "")
            if ticker:
                by_ticker[ticker] = market
    for ticker in (ticker_a, ticker_b):
        if not ticker or ticker in by_ticker:
            continue
        try:
            data = get_json(f"{KALSHI}/markets/{ticker}")
        except Exception:
            continue
        market = data.get("market") if isinstance(data, dict) else None
        if market:
            by_ticker[ticker] = market
    return {
        "yes_a": _kalshi_yes(by_ticker.get(ticker_a or "")),
        "yes_b": _kalshi_yes(by_ticker.get(ticker_b or "")),
        "closed": _kalshi_closed(by_ticker.get(ticker_a or "")) or _kalshi_closed(by_ticker.get(ticker_b or "")),
    }


def _kalshi_yes(market: dict | None) -> float | None:
    if not market:
        return None
    return parse_price(market.get("yes_ask_dollars") or market.get("last_price_dollars"))


def _kalshi_closed(market: dict | None) -> bool:
    if not market:
        return False
    status = str(market.get("status") or "").lower()
    return status not in {"", "active", "open", "initialized"}


def poly_prices(market_id: str | None, sport: str, team_a: str, team_b: str) -> dict:
    if not market_id:
        return {"yes_a": None, "yes_b": None, "closed": False}
    try:
        data = get_json(f"{POLY}/markets/{market_id}")
    except Exception:
        return {"yes_a": None, "yes_b": None, "closed": False}
    market = data
    if isinstance(data, list) and data:
        market = data[0]
    elif isinstance(data, dict) and data.get("id") is None and isinstance(data.get("market"), dict):
        market = data["market"]
    if not isinstance(market, dict):
        return {"yes_a": None, "yes_b": None, "closed": False}
    outcomes = maybe_json(market.get("outcomes")) or []
    prices = maybe_json(market.get("outcomePrices")) or []
    yes_a = yes_b = None
    for i, outcome in enumerate(outcomes):
        mapped = team_id(sport, str(outcome))
        price = parse_price(prices[i] if i < len(prices) else None)
        if mapped == team_a:
            yes_a = price
        elif mapped == team_b:
            yes_b = price
    closed = bool(market.get("closed")) or str(market.get("active")).lower() == "false"
    return {"yes_a": yes_a, "yes_b": yes_b, "closed": closed}


def kalshi_yes_asks(ticker: str | None) -> list[tuple[float, float]]:
    """YES asks for a Kalshi ticker: NO bid at P is a YES ask at $1 - P."""
    if not ticker:
        return []
    try:
        data = get_json(f"{KALSHI}/markets/{ticker}/orderbook")
    except Exception:
        return []
    no_bids = _kalshi_bid_levels(data, "no")
    asks = []
    for bid_price, qty in no_bids:
        ask_price = round(1.0 - bid_price, 4)
        if ask_price > 0 and qty > 0:
            asks.append((ask_price, qty))
    asks.sort(key=lambda level: level[0])
    return asks


def poly_token_asks(token_id: str | None) -> list[tuple[float, float]]:
    """Explicit asks from the Polymarket CLOB for one outcome token."""
    if not token_id:
        return []
    try:
        data = get_json(f"{POLY_CLOB}/book", {"token_id": token_id})
    except Exception:
        return []
    asks = []
    rows = data.get("asks") if isinstance(data, dict) else None
    for level in rows or []:
        price = parse_price(level.get("price") if isinstance(level, dict) else None)
        qty = parse_price(level.get("size") if isinstance(level, dict) else None)
        if price is None or qty is None or price <= 0 or qty <= 0:
            continue
        asks.append((price, qty))
    asks.sort(key=lambda level: level[0])
    return asks


def _kalshi_bid_levels(data: dict, side: str) -> list[tuple[float, float]]:
    fp = data.get("orderbook_fp") if isinstance(data, dict) else None
    raw = None
    dollars = False
    if isinstance(fp, dict):
        raw = fp.get(f"{side}_dollars")
        dollars = True
    if not raw:
        book = data.get("orderbook") if isinstance(data, dict) else None
        raw = book.get(side) if isinstance(book, dict) else None
        dollars = False
    levels = []
    for item in raw or []:
        if not item or len(item) < 2:
            continue
        price = parse_price(item[0])
        qty = parse_price(item[1])
        if price is None or qty is None or qty <= 0:
            continue
        if not dollars and price > 1:
            price = price / 100.0
        levels.append((price, qty))
    return levels
