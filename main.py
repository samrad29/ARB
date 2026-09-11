"""Pull NFL and CFB moneyline markets, match them, find arbs, store in SQLite.

Run:  python main.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from util import parse_game_date
import cfp_moneyline
import nfl

DB_PATH = Path(__file__).resolve().parent / "moneyline.db"
DATE_WINDOW_DAYS = 1
SPORTS = (
    ("nfl", nfl, "NFL", "pro"),
    ("cfb", cfp_moneyline, "CFB", "college"),
)


def match_markets(kalshi: list[dict], poly: list[dict], sport: str, level: str) -> list[dict]:
    def by_game(rows: list[dict]) -> dict[tuple, dict]:
        games: dict[tuple, dict] = {}
        for row in rows:
            if not row["team"] or not row["opponent"]:
                continue
            game_date = parse_game_date(row.get("game_date"))
            if game_date is None:
                continue
            teams = frozenset({row["team"], row["opponent"]})
            key = (teams, game_date)
            game = games.setdefault(key, {"prices": {}, "meta": row, "date": game_date, "teams": teams})
            game["prices"][row["team"]] = row
        return games

    kalshi_games = by_game(kalshi)
    poly_games = by_game(poly)
    used_poly: set[tuple] = set()
    matches = []
    for (teams, k_date), k_game in kalshi_games.items():
        candidates = []
        for (p_teams, p_date), p_game in poly_games.items():
            if p_teams != teams:
                continue
            delta = abs((p_date - k_date).days)
            if delta <= DATE_WINDOW_DAYS:
                candidates.append((delta, p_date, p_game))
        candidates.sort(key=lambda item: (item[0], item[1]))
        picked = None
        for _delta, p_date, p_game in candidates:
            poly_key = (teams, p_date)
            if poly_key in used_poly:
                continue
            used_poly.add(poly_key)
            picked = (p_date, p_game)
            break
        if picked is None:
            continue
        p_date, p_game = picked
        names = sorted(teams)
        k_a = k_game["prices"].get(names[0], {})
        k_b = k_game["prices"].get(names[1], {})
        p_a = p_game["prices"].get(names[0], {})
        p_b = p_game["prices"].get(names[1], {})
        matches.append(
            {
                "sport": sport,
                "level": level,
                "game_date": k_date.isoformat(),
                "poly_game_date": p_date.isoformat(),
                "team_a": names[0],
                "team_b": names[1],
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
    matches.sort(key=lambda row: (row["game_date"], row["team_a"], row["team_b"]))
    return matches


def find_arbs(matches: list[dict]) -> list[dict]:
    """Buy YES on team A at one venue and YES on team B at the other. If that costs < $1, it is an arb."""
    rows = []
    for match in matches:
        kalshi_a = match.get("kalshi_yes_a")
        poly_a = match.get("poly_yes_a")
        kalshi_b = match.get("kalshi_yes_b")
        poly_b = match.get("poly_yes_b")
        if None in (kalshi_a, poly_a, kalshi_b, poly_b):
            continue
        kalshi_a_poly_b = kalshi_a + poly_b
        poly_a_kalshi_b = poly_a + kalshi_b
        if kalshi_a_poly_b <= poly_a_kalshi_b:
            best_cost = kalshi_a_poly_b
            best_trade = f"buy {match['team_a']} Kalshi YES + {match['team_b']} Polymarket YES"
        else:
            best_cost = poly_a_kalshi_b
            best_trade = f"buy {match['team_a']} Polymarket YES + {match['team_b']} Kalshi YES"
        edge = 1.0 - best_cost
        rows.append(
            {
                **match,
                "team_a_price_diff": round(poly_a - kalshi_a, 4),
                "team_b_price_diff": round(poly_b - kalshi_b, 4),
                "kalshi_a_plus_poly_b": round(kalshi_a_poly_b, 4),
                "poly_a_plus_kalshi_b": round(poly_a_kalshi_b, 4),
                "best_cost": round(best_cost, 4),
                "best_edge": round(edge, 4),
                "is_arb": 1 if edge > 0 else 0,
                "best_trade": best_trade,
            }
        )
    rows.sort(key=lambda row: row["best_edge"], reverse=True)
    return rows


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            sport TEXT NOT NULL,
            level TEXT NOT NULL,
            exchange TEXT NOT NULL,
            market_id TEXT NOT NULL,
            event_id TEXT,
            event_title TEXT,
            team TEXT NOT NULL,
            opponent TEXT,
            yes_price REAL,
            volume REAL,
            game_date TEXT,
            close_time TEXT,
            url TEXT,
            PRIMARY KEY (sport, exchange, market_id, team)
        );
        CREATE TABLE IF NOT EXISTS matches (
            sport TEXT NOT NULL,
            level TEXT NOT NULL,
            game_date TEXT NOT NULL,
            poly_game_date TEXT,
            team_a TEXT NOT NULL,
            team_b TEXT NOT NULL,
            kalshi_event TEXT,
            polymarket_event TEXT,
            kalshi_yes_a REAL,
            poly_yes_a REAL,
            kalshi_yes_b REAL,
            poly_yes_b REAL,
            kalshi_url TEXT,
            polymarket_url TEXT,
            PRIMARY KEY (sport, game_date, team_a, team_b)
        );
        CREATE TABLE IF NOT EXISTS arbs (
            sport TEXT NOT NULL,
            level TEXT NOT NULL,
            game_date TEXT NOT NULL,
            poly_game_date TEXT,
            team_a TEXT NOT NULL,
            team_b TEXT NOT NULL,
            kalshi_event TEXT,
            polymarket_event TEXT,
            kalshi_yes_a REAL,
            poly_yes_a REAL,
            team_a_price_diff REAL,
            kalshi_yes_b REAL,
            poly_yes_b REAL,
            team_b_price_diff REAL,
            kalshi_a_plus_poly_b REAL,
            poly_a_plus_kalshi_b REAL,
            best_cost REAL,
            best_edge REAL,
            is_arb INTEGER,
            best_trade TEXT,
            kalshi_url TEXT,
            polymarket_url TEXT,
            PRIMARY KEY (sport, game_date, team_a, team_b)
        );
        CREATE INDEX IF NOT EXISTS idx_markets_sport ON markets (sport, level);
        CREATE INDEX IF NOT EXISTS idx_matches_sport ON matches (sport, level);
        CREATE INDEX IF NOT EXISTS idx_arbs_sport ON arbs (sport, level, is_arb, best_edge);
        """
    )
    return conn


def save_db(markets: list[dict], matches: list[dict], arbs: list[dict]) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM markets")
        conn.execute("DELETE FROM matches")
        conn.execute("DELETE FROM arbs")
        conn.executemany(
            """
            INSERT INTO markets (
                sport, level, exchange, market_id, event_id, event_title, team, opponent,
                yes_price, volume, game_date, close_time, url
            ) VALUES (
                :sport, :level, :exchange, :market_id, :event_id, :event_title, :team, :opponent,
                :yes_price, :volume, :game_date, :close_time, :url
            )
            """,
            markets,
        )
        conn.executemany(
            """
            INSERT INTO matches (
                sport, level, game_date, poly_game_date, team_a, team_b, kalshi_event, polymarket_event,
                kalshi_yes_a, poly_yes_a, kalshi_yes_b, poly_yes_b, kalshi_url, polymarket_url
            ) VALUES (
                :sport, :level, :game_date, :poly_game_date, :team_a, :team_b, :kalshi_event, :polymarket_event,
                :kalshi_yes_a, :poly_yes_a, :kalshi_yes_b, :poly_yes_b, :kalshi_url, :polymarket_url
            )
            """,
            matches,
        )
        conn.executemany(
            """
            INSERT INTO arbs (
                sport, level, game_date, poly_game_date, team_a, team_b, kalshi_event, polymarket_event,
                kalshi_yes_a, poly_yes_a, team_a_price_diff, kalshi_yes_b, poly_yes_b, team_b_price_diff,
                kalshi_a_plus_poly_b, poly_a_plus_kalshi_b, best_cost, best_edge, is_arb, best_trade,
                kalshi_url, polymarket_url
            ) VALUES (
                :sport, :level, :game_date, :poly_game_date, :team_a, :team_b, :kalshi_event, :polymarket_event,
                :kalshi_yes_a, :poly_yes_a, :team_a_price_diff, :kalshi_yes_b, :poly_yes_b, :team_b_price_diff,
                :kalshi_a_plus_poly_b, :poly_a_plus_kalshi_b, :best_cost, :best_edge, :is_arb, :best_trade,
                :kalshi_url, :polymarket_url
            )
            """,
            arbs,
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    all_markets: list[dict] = []
    all_matches: list[dict] = []
    all_arbs: list[dict] = []

    for sport, module, label, level in SPORTS:
        print(f"Fetching Kalshi {label} moneylines...")
        kalshi = module.fetch_kalshi()
        print(f"  {len(kalshi)} team-win contracts")
        print(f"Fetching Polymarket {label} moneylines...")
        poly = module.fetch_polymarket()
        print(f"  {len(poly)} team-win outcomes")
        matches = match_markets(kalshi, poly, sport, level)
        arbs = find_arbs(matches)
        all_markets.extend(kalshi + poly)
        all_matches.extend(matches)
        all_arbs.extend(arbs)

    save_db(all_markets, all_matches, all_arbs)
    hits = [row for row in all_arbs if row["is_arb"]]
    print(f"Wrote {DB_PATH.name}: {len(all_markets)} markets, {len(all_matches)} matches, {len(hits)} arbs / {len(all_arbs)} compared")
    for row in all_matches[:8]:
        print(
            f"  [{row['sport']}] {row['game_date']} {row['team_a']} vs {row['team_b']}: "
            f"Kalshi {row['kalshi_yes_a']}/{row['kalshi_yes_b']}  "
            f"Poly {row['poly_yes_a']}/{row['poly_yes_b']}"
        )
    if len(all_matches) > 8:
        print(f"  ... {len(all_matches) - 8} more")
    if hits:
        print("Arbs (pre-fee):")
        for row in hits[:12]:
            print(f"  [{row['sport']}] {row['game_date']} {row['best_trade']}  cost={row['best_cost']} edge={row['best_edge']}")
        if len(hits) > 12:
            print(f"  ... {len(hits) - 12} more")
    else:
        print("No pre-fee arbs this run (best edges are in the arbs table).")


if __name__ == "__main__":
    main()
