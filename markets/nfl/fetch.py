"""NFL moneyline discovery: Kalshi KXNFLGAME + Polymarket NFL game moneylines."""

from __future__ import annotations

import re
from datetime import datetime

from util import KALSHI, POLY, get_json, maybe_json, parse_dt, parse_price, poly_game_state

SPORT = "nfl"
LEVEL = "pro"
KALSHI_SERIES = "KXNFLGAME"
POLY_TAG = 450
GAME_SLUG = re.compile(r"^nfl-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")
KALSHI_EVENT = re.compile(r"^KXNFLGAME-(\d{2}[A-Z]{3}\d{2})([A-Z]+)$")

TEAMS = {
    "ari": "cardinals", "arizona": "cardinals", "cardinals": "cardinals",
    "atl": "falcons", "atlanta": "falcons", "falcons": "falcons",
    "bal": "ravens", "baltimore": "ravens", "ravens": "ravens",
    "buf": "bills", "buffalo": "bills", "bills": "bills",
    "car": "panthers", "carolina": "panthers", "panthers": "panthers",
    "chi": "bears", "chicago": "bears", "bears": "bears",
    "cin": "bengals", "cincinnati": "bengals", "bengals": "bengals",
    "cle": "browns", "cleveland": "browns", "browns": "browns",
    "dal": "cowboys", "dallas": "cowboys", "cowboys": "cowboys",
    "den": "broncos", "denver": "broncos", "broncos": "broncos",
    "det": "lions", "detroit": "lions", "lions": "lions",
    "gb": "packers", "gnb": "packers", "green bay": "packers", "packers": "packers",
    "hou": "texans", "houston": "texans", "texans": "texans",
    "ind": "colts", "indianapolis": "colts", "colts": "colts",
    "jac": "jaguars", "jax": "jaguars", "jacksonville": "jaguars", "jaguars": "jaguars",
    "kc": "chiefs", "kan": "chiefs", "kansas city": "chiefs", "chiefs": "chiefs",
    "lv": "raiders", "lvr": "raiders", "las vegas": "raiders", "oakland": "raiders", "raiders": "raiders",
    "lac": "chargers", "chargers": "chargers", "los angeles chargers": "chargers",
    "lar": "rams", "rams": "rams", "los angeles rams": "rams", "los angeles r": "rams",
    "mia": "dolphins", "miami": "dolphins", "dolphins": "dolphins",
    "min": "vikings", "minnesota": "vikings", "vikings": "vikings",
    "ne": "patriots", "nwe": "patriots", "new england": "patriots", "patriots": "patriots",
    "no": "saints", "nor": "saints", "new orleans": "saints", "saints": "saints",
    "nyg": "giants", "new york g": "giants", "new york giants": "giants", "giants": "giants",
    "nyj": "jets", "new york j": "jets", "new york jets": "jets", "jets": "jets",
    "phi": "eagles", "philadelphia": "eagles", "eagles": "eagles",
    "pit": "steelers", "pittsburgh": "steelers", "steelers": "steelers",
    "sea": "seahawks", "seattle": "seahawks", "seahawks": "seahawks",
    "sf": "49ers", "sfo": "49ers", "san francisco": "49ers", "49ers": "49ers", "niners": "49ers",
    "tb": "buccaneers", "tam": "buccaneers", "tampa": "buccaneers", "tampa bay": "buccaneers",
    "buccaneers": "buccaneers", "bucs": "buccaneers",
    "ten": "titans", "tennessee": "titans", "titans": "titans",
    "was": "commanders", "wsh": "commanders", "washington": "commanders", "commanders": "commanders",
}
CODES = tuple(
    sorted(
        {token.upper() for token in TEAMS if token.isalpha() and 2 <= len(token) <= 3},
        key=len,
        reverse=True,
    )
)


def team_id(text: str | None) -> str | None:
    if not text:
        return None
    raw = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()
    if raw in TEAMS:
        return TEAMS[raw]
    raw = re.sub(r"\s+", " ", raw)
    if raw in TEAMS:
        return TEAMS[raw]
    return TEAMS.get(raw.replace(" ", ""))


def _row(payload: dict) -> dict:
    payload.setdefault("live", 0)
    payload.setdefault("ended", 0)
    payload["sport"] = SPORT
    payload["level"] = LEVEL
    return payload


def fetch_kalshi() -> list[dict]:
    rows: list[dict] = []
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
            rows.extend(_kalshi_event(event))
        cursor = data.get("cursor") or None
        if not cursor:
            break
    return rows


def _kalshi_event(event: dict) -> list[dict]:
    event_ticker = event.get("event_ticker") or ""
    title = event.get("title") or event_ticker
    parsed = _parse_event_ticker(event_ticker)
    event_teams = parsed["teams"] if parsed else []
    game_date = parsed["date"] if parsed else None
    markets = event.get("markets") or []
    out = []
    for market in markets:
        team = team_id(market.get("ticker", "").rsplit("-", 1)[-1]) or team_id(market.get("yes_sub_title"))
        if not team:
            continue
        opponent = next((other for other in event_teams if other != team), None)
        if opponent is None:
            others = [team_id(m.get("ticker", "").rsplit("-", 1)[-1]) for m in markets]
            opponent = next((other for other in others if other and other != team), None)
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
                    "url": f"https://kalshi.com/markets/kxnflgame/{str(event_ticker).lower()}",
                }
            )
        )
    if not game_date:
        first_close = next((parse_dt(m.get("close_time")) for m in markets if m.get("close_time")), None)
        game_date = first_close.date().isoformat() if first_close else ""
    for row in out:
        row["game_date"] = game_date or ""
    return out


def _parse_event_ticker(ticker: str) -> dict | None:
    match = KALSHI_EVENT.match(ticker or "")
    if not match:
        return None
    date_raw, rest = match.group(1), match.group(2)
    try:
        game_date = datetime.strptime(date_raw, "%y%b%d").date().isoformat()
    except ValueError:
        game_date = ""
    teams = []
    leftover = rest
    while leftover:
        code = next((c for c in CODES if leftover.startswith(c)), None)
        if not code:
            break
        mapped = team_id(code)
        if mapped:
            teams.append(mapped)
        leftover = leftover[len(code) :]
    return {"date": game_date, "teams": teams}


def fetch_polymarket() -> list[dict]:
    rows: list[dict] = []
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
            meta = event.get("sport") or {}
            sport_slug = meta.get("sport") if isinstance(meta, dict) else meta
            if sport_slug != SPORT or not GAME_SLUG.match(slug):
                continue
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
        mapped = team_id(item.get("alias")) or team_id(item.get("name")) or team_id(item.get("abbreviation"))
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
    close = parse_dt(moneyline.get("endDate") or event.get("endDate") or event.get("startDate"))
    game_date = close.date().isoformat() if close else ""
    date_from_slug = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if date_from_slug:
        game_date = date_from_slug.group(1)
    volume = parse_price(moneyline.get("volume"))
    state = poly_game_state(event)
    out = []
    for i, outcome in enumerate(outcomes):
        team = team_id(str(outcome))
        if not team:
            continue
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
