"""Live price polling for matched moneylines."""

from __future__ import annotations

from util import KALSHI, POLY, get_json, maybe_json, parse_price
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
