"""Pull college football moneyline markets from Kalshi and Polymarket, match them, write CSVs.

Run:  python main.py
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
POLY = "https://gamma-api.polymarket.com"
KALSHI_SERIES = "KXNCAAFGAME"
POLY_CFB_TAG = 100351
OUT_DIR = Path(__file__).resolve().parent

GAME_SLUG = re.compile(r"^cfb-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")

# Short / alternate school names -> one id. Generic "St." -> "State" is handled in team_id().
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


def get_json(url: str, params: dict | None = None) -> dict | list:
    if params:
        url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"User-Agent": "cfb-moneyline-scanner", "Accept": "application/json"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed {url}")


def team_id(text: str | None) -> str | None:
    if not text:
        return None
    raw = text.lower().replace("&", " and ").replace("st.", " state ")
    raw = re.sub(r"[^a-z0-9 ]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    return ALIASES.get(raw, raw)


def parse_price(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
                "url": f"https://kalshi.com/markets/{KALSHI_SERIES.lower()}/{str(event_ticker).lower()}",
            }
        )
    return out


def fetch_polymarket() -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        events = get_json(
            f"{POLY}/events",
            {
                "tag_id": POLY_CFB_TAG,
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
            sport = event.get("sport") or {}
            sport_slug = sport.get("sport") if isinstance(sport, dict) else sport
            if sport_slug != "cfb" or not GAME_SLUG.match(slug):
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
        mapped = team_id(item.get("alias"))
        if mapped:
            teams.append(mapped)
    moneyline = next(
        (m for m in (event.get("markets") or []) if m.get("sportsMarketType") == "moneyline" and not m.get("closed")),
        None,
    )
    if not moneyline:
        return []
    outcomes = _maybe_json(moneyline.get("outcomes")) or []
    prices = _maybe_json(moneyline.get("outcomePrices")) or []
    close = parse_dt(moneyline.get("endDate") or event.get("endDate") or event.get("startDate"))
    game_date = close.date().isoformat() if close else ""
    date_from_slug = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if date_from_slug:
        game_date = date_from_slug.group(1)
    volume = parse_price(moneyline.get("volume"))
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
            }
        )
    return out


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def match_markets(kalshi: list[dict], poly: list[dict]) -> list[dict]:
    def by_game(rows: list[dict]) -> dict[frozenset, dict]:
        games: dict[frozenset, dict] = {}
        for row in rows:
            if not row["team"] or not row["opponent"]:
                continue
            key = frozenset({row["team"], row["opponent"]})
            game = games.setdefault(key, {"prices": {}, "meta": row})
            game["prices"][row["team"]] = row
        return games

    kalshi_games = by_game(kalshi)
    poly_games = by_game(poly)
    matches = []
    for key, k_game in kalshi_games.items():
        p_game = poly_games.get(key)
        if not p_game:
            continue
        teams = sorted(key)
        k_a = k_game["prices"].get(teams[0], {})
        k_b = k_game["prices"].get(teams[1], {})
        p_a = p_game["prices"].get(teams[0], {})
        p_b = p_game["prices"].get(teams[1], {})
        matches.append(
            {
                "team_a": teams[0],
                "team_b": teams[1],
                "kalshi_event": k_game["meta"].get("event_title"),
                "polymarket_event": p_game["meta"].get("event_title"),
                "kalshi_yes_a": k_a.get("yes_price"),
                "poly_yes_a": p_a.get("yes_price"),
                "kalshi_yes_b": k_b.get("yes_price"),
                "poly_yes_b": p_b.get("yes_price"),
                "kalshi_url": k_game["meta"].get("url"),
                "polymarket_url": p_game["meta"].get("url"),
            }
        )
    matches.sort(key=lambda row: (row["team_a"], row["team_b"]))
    return matches


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    print("Fetching Kalshi CFB moneylines (KXNCAAFGAME)...")
    kalshi = fetch_kalshi()
    print(f"  {len(kalshi)} team-win contracts")

    print("Fetching Polymarket CFB moneylines...")
    poly = fetch_polymarket()
    print(f"  {len(poly)} team-win outcomes")

    markets = kalshi + poly
    matches = match_markets(kalshi, poly)

    write_csv(
        OUT_DIR / "markets.csv",
        markets,
        [
            "exchange",
            "market_id",
            "event_id",
            "event_title",
            "team",
            "opponent",
            "yes_price",
            "volume",
            "game_date",
            "close_time",
            "url",
        ],
    )
    write_csv(
        OUT_DIR / "matches.csv",
        matches,
        [
            "team_a",
            "team_b",
            "kalshi_event",
            "polymarket_event",
            "kalshi_yes_a",
            "poly_yes_a",
            "kalshi_yes_b",
            "poly_yes_b",
            "kalshi_url",
            "polymarket_url",
        ],
    )
    print(f"Wrote markets.csv ({len(markets)} rows) and matches.csv ({len(matches)} matched games)")
    for row in matches[:12]:
        print(
            f"  {row['team_a']} vs {row['team_b']}: "
            f"Kalshi {row['kalshi_yes_a']}/{row['kalshi_yes_b']}  "
            f"Poly {row['poly_yes_a']}/{row['poly_yes_b']}"
        )
    if len(matches) > 12:
        print(f"  ... {len(matches) - 12} more")


if __name__ == "__main__":
    main()
