"""College football moneyline discovery: Kalshi KXNCAAFGAME + Polymarket CFB game moneylines."""

from __future__ import annotations

import re
from datetime import datetime

from util import KALSHI, POLY, get_json, maybe_json, parse_dt, parse_price, poly_game_state

SPORT = "cfb"
LEVEL = "college"
KALSHI_SERIES = "KXNCAAFGAME"
POLY_TAG = 100351
GAME_SLUG = re.compile(r"^cfb-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")

ALIASES = {
    "albany": "albany",
    "university at albany": "albany",
    "ualbany": "albany",
    "app state": "appalachian state",
    "appalachian st": "appalachian state",
    "boston college": "boston college",
    "cal": "california",
    "central florida": "ucf",
    "ucf": "ucf",
    "connecticut": "uconn",
    "uconn": "uconn",
    "florida a and m": "florida am",
    "florida a m": "florida am",
    "florida am": "florida am",
    "florida atlantic": "florida atlantic",
    "fau": "florida atlantic",
    "florida international": "fiu",
    "fiu": "fiu",
    "lsu": "lsu",
    "miami fl": "miami fl",
    "miami (fl)": "miami fl",
    "miami florida": "miami fl",
    "miami oh": "miami oh",
    "miami (oh)": "miami oh",
    "miami ohio": "miami oh",
    "miami (ohio)": "miami oh",
    "nc state": "north carolina state",
    "n c state": "north carolina state",
    "ole miss": "mississippi",
    "pitt": "pittsburgh",
    "southern cal": "usc",
    "southern california": "usc",
    "usc": "usc",
    "south florida": "usf",
    "usf": "usf",
    "southern miss": "southern miss",
    "southern mississippi": "southern miss",
    "tcu": "tcu",
    "texas a and m": "texas am",
    "texas a m": "texas am",
    "texas am": "texas am",
    "ucla": "ucla",
    "unlv": "unlv",
    "utep": "utep",
    "utsa": "utsa",
    "uab": "uab",
    "smu": "smu",
    "byu": "byu",
}


def team_id(text: str | None) -> str | None:
    if not text:
        return None
    raw = text.lower().replace("&", " and ").replace("st.", " state ")
    raw = re.sub(r"[^a-z0-9 ]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    return ALIASES.get(raw, raw)


def _row(payload: dict) -> dict:
    payload.setdefault("live", 0)
    payload.setdefault("ended", 0)
    payload["sport"] = SPORT
    payload["level"] = LEVEL
    return payload


def fetch_kalshi() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = None
    while True:
        data = get_json(
            f"{KALSHI}/events",
            {
                "series_ticker": KALSHI_SERIES,
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
            rows.extend(_kalshi_event(event))
        cursor = data.get("cursor") or None
        if not cursor:
            break
    return rows


def _kalshi_event(event: dict) -> list[dict]:
    event_ticker = event.get("event_ticker") or ""
    title = event.get("title") or event_ticker
    markets = event.get("markets") or []
    names = [team_id(m.get("yes_sub_title")) for m in markets]
    date_raw = None
    match = re.match(r"^KXNCAAFGAME-(\d{2}[A-Z]{3}\d{2})", event_ticker)
    if match:
        try:
            date_raw = datetime.strptime(match.group(1), "%y%b%d").date().isoformat()
        except ValueError:
            date_raw = None
    out = []
    for market in markets:
        team = team_id(market.get("yes_sub_title"))
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
                    "url": f"https://kalshi.com/markets/kxncaafgame/{str(event_ticker).lower()}",
                }
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
            if sport_slug != SPORT or not GAME_SLUG.match(slug):
                continue
            if event_key in seen:
                continue
            seen.add(event_key)
            rows.extend(_poly_event(event))
        offset += len(events)
        if len(events) < 100:
            break
    return rows


def _poly_event(event: dict) -> list[dict]:
    slug = event.get("slug") or ""
    title = event.get("title") or slug
    teams = []
    for item in event.get("teams") or []:
        mapped = team_id(item.get("alias"))
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
        team = team_id(str(outcome))
        if not team or team in seen_teams:
            continue
        seen_teams.add(team)
        opponent = next((other for other in teams if other != team), "")
        if not opponent:
            others = [team_id(str(o)) for o in outcomes]
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
                }
            )
        )
    return out
