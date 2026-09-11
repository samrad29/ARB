"""Tennis moneyline discovery: Kalshi match series + Polymarket tennis moneylines."""

from __future__ import annotations

import re
from datetime import datetime

from util import KALSHI, POLY, get_json, maybe_json, parse_dt, parse_price, poly_game_state

from .names import GAME_SLUG, KALSHI_DATE, KALSHI_SERIES, POLY_TAG, SPORT, player_id, poly_tour

__all__ = ["fetch_kalshi", "fetch_polymarket", "player_id"]


def _row(payload: dict, level: str) -> dict:
    payload.setdefault("live", 0)
    payload.setdefault("ended", 0)
    payload["sport"] = SPORT
    payload["level"] = level
    return payload


def fetch_kalshi() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for series, level in KALSHI_SERIES:
        cursor = None
        while True:
            data = get_json(
                f"{KALSHI}/events",
                {
                    "series_ticker": series,
                    "status": "open",
                    "with_nested_markets": "true",
                    "limit": 200,
                    "cursor": cursor,
                },
            )
            for event in data.get("events") or []:
                ticker = event.get("event_ticker") or ""
                if ticker and ticker in seen:
                    continue
                if ticker:
                    seen.add(ticker)
                rows.extend(_kalshi_event(event, series, level))
            cursor = data.get("cursor") or None
            if not cursor:
                break
    return rows


def _kalshi_event(event: dict, series: str, level: str) -> list[dict]:
    event_ticker = event.get("event_ticker") or ""
    title = event.get("title") or event_ticker
    markets = event.get("markets") or []
    names = [player_id(m.get("yes_sub_title")) for m in markets]
    date_raw = None
    match = KALSHI_DATE.match(event_ticker)
    if match:
        try:
            date_raw = datetime.strptime(match.group(1), "%y%b%d").date().isoformat()
        except ValueError:
            date_raw = None
    out = []
    for market in markets:
        team = player_id(market.get("yes_sub_title"))
        if not team:
            continue
        opponent = next((other for other in names if other and other != team), None)
        close = parse_dt(market.get("close_time") or market.get("expected_expiration_time"))
        out.append(
            _row(
                {
                    "exchange": "kalshi",
                    "market_id": market.get("ticker"),
                    "event_id": event_ticker,
                    "event_title": title,
                    "team": team,
                    "opponent": opponent or "",
                    "yes_price": parse_price(market.get("yes_ask_dollars") or market.get("last_price_dollars")),
                    "volume": parse_price(market.get("volume_fp")),
                    "close_time": close.isoformat() if close else "",
                    "game_date": date_raw or (close.date().isoformat() if close else ""),
                    "url": f"https://kalshi.com/markets/{series.lower()}/{str(event_ticker).lower()}",
                },
                level,
            )
        )
    return out


def fetch_polymarket() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while True:
        events = get_json(
            f"{POLY}/events",
            {
                "tag_id": POLY_TAG,
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
            },
        )
        if not isinstance(events, list) or not events:
            break
        for event in events:
            slug = event.get("slug") or ""
            event_key = str(event.get("id") or slug)
            meta = event.get("sport") or {}
            sport_slug = meta.get("sport") if isinstance(meta, dict) else meta
            if not GAME_SLUG.match(slug):
                continue
            level = poly_tour(sport_slug if isinstance(sport_slug, str) else None, event.get("title"))
            if not level:
                continue
            if event_key in seen:
                continue
            seen.add(event_key)
            rows.extend(_poly_event(event, level))
        offset += len(events)
        if len(events) < 100:
            break
    return rows


def _poly_event(event: dict, level: str) -> list[dict]:
    slug = event.get("slug") or ""
    title = event.get("title") or slug
    teams = []
    for item in event.get("teams") or []:
        mapped = player_id(item.get("name")) or player_id(item.get("alias")) or player_id(item.get("abbreviation"))
        if mapped:
            teams.append(mapped)
    moneyline = next(
        (m for m in (event.get("markets") or []) if m.get("sportsMarketType") == "moneyline" and not m.get("closed")),
        None,
    )
    if not moneyline:
        return []
    outcomes = maybe_json(moneyline.get("outcomes")) or []
    prices = maybe_json(moneyline.get("outcomePrices")) or []
    tokens = maybe_json(moneyline.get("clobTokenIds"))
    if not isinstance(tokens, list):
        tokens = []
    close = parse_dt(moneyline.get("endDate") or event.get("endDate") or event.get("startDate"))
    game_date = close.date().isoformat() if close else ""
    date_from_slug = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if date_from_slug:
        game_date = date_from_slug.group(1)
    volume = parse_price(moneyline.get("volume"))
    state = poly_game_state(event)
    out = []
    seen_teams: set[str] = set()
    for i, outcome in enumerate(outcomes):
        team = player_id(str(outcome))
        if not team or team in seen_teams:
            continue
        seen_teams.add(team)
        opponent = next((other for other in teams if other != team), "")
        if not opponent:
            others = [player_id(str(o)) for o in outcomes]
            opponent = next((other for other in others if other and other != team), "") or ""
        price = parse_price(prices[i] if i < len(prices) else None)
        out.append(
            _row(
                {
                    "exchange": "polymarket",
                    "market_id": moneyline.get("id") or moneyline.get("slug") or slug,
                    "token_id": str(tokens[i]) if i < len(tokens) and tokens[i] else None,
                    "event_id": slug,
                    "event_title": title,
                    "team": team,
                    "opponent": opponent,
                    "yes_price": price,
                    "volume": volume,
                    "close_time": close.isoformat() if close else "",
                    "game_date": game_date,
                    "url": f"https://polymarket.com/event/{slug}",
                    "live": state["live"],
                    "ended": state["ended"],
                },
                level,
            )
        )
    return out
