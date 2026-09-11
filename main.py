"""Pull NFL, CFB, and tennis moneyline markets, match them, find arbs, store in SQLite.

Run:  python main.py
Poll: python -m polling
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from util import game_status, parse_game_date
from markets import cfp_moneyline, nfl, tennis
from polling.prices import kalshi_yes_asks, poly_token_asks

DB_PATH = Path(__file__).resolve().parent / "moneyline.db"
DATE_WINDOW_DAYS = 1
SPORTS = (
    ("nfl", nfl, "NFL", "pro"),
    ("cfb", cfp_moneyline, "CFB", "college"),
    ("tennis", tennis, "Tennis", "tour"),
)


def match_markets(
    kalshi: list[dict],
    poly: list[dict],
    sport: str,
    level: str,
    names_equal=None,
) -> list[dict]:
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

    def map_teams(k_teams: frozenset, p_teams: frozenset) -> dict | None:
        if k_teams == p_teams:
            return {name: name for name in k_teams}
        if names_equal is None or len(k_teams) != 2 or len(p_teams) != 2:
            return None
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for k_name in k_teams:
            hits = [p_name for p_name in p_teams if p_name not in used and names_equal(k_name, p_name)]
            if len(hits) != 1:
                return None
            mapping[k_name] = hits[0]
            used.add(hits[0])
        return mapping

    kalshi_games = by_game(kalshi)
    poly_games = by_game(poly)
    used_poly: set[tuple] = set()
    matches = []
    for (teams, k_date), k_game in kalshi_games.items():
        candidates = []
        for (p_teams, p_date), p_game in poly_games.items():
            mapped = map_teams(teams, p_teams)
            if not mapped:
                continue
            delta = abs((p_date - k_date).days)
            if delta <= DATE_WINDOW_DAYS:
                candidates.append((delta, p_date, p_game, p_teams, mapped))
        candidates.sort(key=lambda item: (item[0], item[1]))
        picked = None
        for _delta, p_date, p_game, p_teams, mapped in candidates:
            poly_key = (p_teams, p_date)
            if poly_key in used_poly:
                continue
            used_poly.add(poly_key)
            picked = (p_date, p_game, mapped)
            break
        if picked is None:
            continue
        p_date, p_game, mapped = picked
        names = sorted(teams)
        k_a = k_game["prices"].get(names[0], {})
        k_b = k_game["prices"].get(names[1], {})
        p_a = p_game["prices"].get(mapped[names[0]], {})
        p_b = p_game["prices"].get(mapped[names[1]], {})
        matches.append(
            {
                "sport": k_game["meta"].get("sport") or sport,
                "level": k_game["meta"].get("level") or level,
                "game_date": k_date.isoformat(),
                "poly_game_date": p_date.isoformat(),
                "team_a": names[0],
                "team_b": names[1],
                "kalshi_event": k_game["meta"].get("event_title"),
                "polymarket_event": p_game["meta"].get("event_title"),
                "kalshi_event_id": k_game["meta"].get("event_id"),
                "kalshi_id_a": k_a.get("market_id"),
                "kalshi_id_b": k_b.get("market_id"),
                "poly_id": p_a.get("market_id") or p_b.get("market_id"),
                "poly_event_id": p_game["meta"].get("event_id"),
                "poly_token_a": p_a.get("token_id"),
                "poly_token_b": p_b.get("token_id"),
                "kalshi_yes_a": k_a.get("yes_price"),
                "poly_yes_a": p_a.get("yes_price"),
                "kalshi_yes_b": k_b.get("yes_price"),
                "poly_yes_b": p_b.get("yes_price"),
                "kalshi_url": k_game["meta"].get("url"),
                "polymarket_url": p_game["meta"].get("url"),
                "live": 1 if p_game["meta"].get("live") else 0,
                "ended": 1 if p_game["meta"].get("ended") else 0,
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


def walk_asks(asks_left: list[tuple[float, float]], asks_right: list[tuple[float, float]]) -> dict | None:
    """Pair two ask books while the combined price stays under $1.00."""
    left = [[price, qty] for price, qty in asks_left]
    right = [[price, qty] for price, qty in asks_right]
    i = 0
    j = 0
    total_qty = 0.0
    total_cost = 0.0
    while i < len(left) and j < len(right):
        price_left, qty_left = left[i]
        price_right, qty_right = right[j]
        combined = round(price_left + price_right, 4)
        if combined >= 1.0:
            break
        take = min(qty_left, qty_right)
        total_qty += take
        total_cost += take * combined
        left[i][1] -= take
        right[j][1] -= take
        if left[i][1] <= 1e-9:
            i += 1
        if right[j][1] <= 1e-9:
            j += 1
    if total_qty <= 0:
        return None
    avg_cost = total_cost / total_qty
    return {
        "qty": total_qty,
        "total_cost": total_cost,
        "avg_cost": avg_cost,
        "profit": total_qty - total_cost,
        "edge": 1.0 - avg_cost,
    }


def scan_executable(matches: list[dict]) -> list[dict]:
    """Fetch books for each match and score both buy directions."""
    rows = []
    open_matches = [match for match in matches if game_status(match) != "ended"]
    print(f"Fetching order books for {len(open_matches)} matches...")
    for n, match in enumerate(open_matches, 1):
        kalshi_a = kalshi_yes_asks(match.get("kalshi_id_a"))
        kalshi_b = kalshi_yes_asks(match.get("kalshi_id_b"))
        poly_a = poly_token_asks(match.get("poly_token_a"))
        poly_b = poly_token_asks(match.get("poly_token_b"))
        rows.append(
            _executable_direction(
                match,
                match["team_a"],
                kalshi_a,
                match["team_b"],
                poly_b,
            )
        )
        rows.append(
            _executable_direction(
                match,
                match["team_b"],
                kalshi_b,
                match["team_a"],
                poly_a,
            )
        )
        if n % 25 == 0 or n == len(open_matches):
            print(f"  {n}/{len(open_matches)}")
    return [row for row in rows if row]


def _executable_direction(
    match: dict,
    kalshi_team: str,
    kalshi_asks: list[tuple[float, float]],
    poly_team: str,
    poly_asks: list[tuple[float, float]],
) -> dict | None:
    if not kalshi_asks or not poly_asks:
        return None
    walked = walk_asks(kalshi_asks, poly_asks)
    kalshi_ask = kalshi_asks[0][0]
    poly_ask = poly_asks[0][0]
    combined = round(kalshi_ask + poly_ask, 4)
    return {
        "sport": match.get("sport"),
        "level": match.get("level"),
        "game_date": match.get("game_date"),
        "team_a": match["team_a"],
        "team_b": match["team_b"],
        "live": match.get("live"),
        "ended": match.get("ended"),
        "direction": f"Buy {kalshi_team} on Kalshi + {poly_team} on Polymarket",
        "kalshi_ask": kalshi_ask,
        "poly_ask": poly_ask,
        "combined_best": combined,
        "qty": walked["qty"] if walked else 0.0,
        "avg_cost": walked["avg_cost"] if walked else combined,
        "profit": walked["profit"] if walked else 0.0,
        "edge": walked["edge"] if walked else round(1.0 - combined, 4),
        "is_exec": 1 if walked else 0,
    }


def quote_executable(match: dict) -> dict | None:
    """Fetch live books for one match and score both buy directions."""
    if game_status(match) == "ended":
        return None
    kalshi_a = kalshi_yes_asks(match.get("kalshi_id_a"))
    kalshi_b = kalshi_yes_asks(match.get("kalshi_id_b"))
    poly_a = poly_token_asks(match.get("poly_token_a"))
    poly_b = poly_token_asks(match.get("poly_token_b"))
    dir_a = _executable_direction(match, match["team_a"], kalshi_a, match["team_b"], poly_b)
    dir_b = _executable_direction(match, match["team_b"], kalshi_b, match["team_a"], poly_a)
    dirs = [row for row in (dir_a, dir_b) if row]
    quoted = {
        **match,
        "kalshi_yes_a": kalshi_a[0][0] if kalshi_a else None,
        "kalshi_yes_b": kalshi_b[0][0] if kalshi_b else None,
        "poly_yes_a": poly_a[0][0] if poly_a else None,
        "poly_yes_b": poly_b[0][0] if poly_b else None,
        "closed": 0,
        "is_exec": 0,
        "is_arb": 0,
        "exec_qty": 0.0,
        "exec_profit": 0.0,
        "best_cost": None,
        "best_edge": None,
        "best_trade": None,
    }
    if not dirs:
        if None in (quoted["kalshi_yes_a"], quoted["kalshi_yes_b"], quoted["poly_yes_a"], quoted["poly_yes_b"]):
            return None
        return quoted
    best = max(dirs, key=lambda row: (row["is_exec"], row["edge"]))
    quoted["best_trade"] = best["direction"]
    quoted["live"] = match.get("live") or 0
    quoted["ended"] = match.get("ended") or 0
    if best["is_exec"]:
        quoted["is_exec"] = 1
        quoted["is_arb"] = 1
        quoted["best_cost"] = round(best["avg_cost"], 4)
        quoted["best_edge"] = round(best["edge"], 4)
        quoted["exec_qty"] = best["qty"]
        quoted["exec_profit"] = best["profit"]
    else:
        quoted["best_cost"] = round(best["combined_best"], 4)
        quoted["best_edge"] = round(1.0 - best["combined_best"], 4)
    return quoted


def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, spec in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


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
            live INTEGER,
            ended INTEGER,
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
            live INTEGER,
            ended INTEGER,
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
            live INTEGER,
            ended INTEGER,
            PRIMARY KEY (sport, game_date, team_a, team_b)
        );
        CREATE TABLE IF NOT EXISTS price_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            sport TEXT,
            level TEXT,
            game_date TEXT,
            team_a TEXT,
            team_b TEXT,
            kalshi_yes_a REAL,
            poly_yes_a REAL,
            kalshi_yes_b REAL,
            poly_yes_b REAL,
            best_cost REAL,
            best_edge REAL,
            is_arb INTEGER,
            best_trade TEXT,
            closed INTEGER,
            live INTEGER,
            ended INTEGER
        );
        CREATE TABLE IF NOT EXISTS arb_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            source TEXT,
            sport TEXT,
            level TEXT,
            game_date TEXT,
            poly_game_date TEXT,
            team_a TEXT,
            team_b TEXT,
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
            live INTEGER,
            ended INTEGER
        );
        CREATE TABLE IF NOT EXISTS book_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            sport TEXT,
            level TEXT,
            game_date TEXT,
            team_a TEXT,
            team_b TEXT,
            kalshi_yes_a REAL,
            poly_yes_a REAL,
            kalshi_yes_b REAL,
            poly_yes_b REAL,
            best_cost REAL,
            best_edge REAL,
            is_exec INTEGER,
            best_trade TEXT,
            exec_qty REAL,
            exec_profit REAL,
            live INTEGER,
            ended INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_markets_sport ON markets (sport, level);
        CREATE INDEX IF NOT EXISTS idx_matches_sport ON matches (sport, level);
        CREATE INDEX IF NOT EXISTS idx_arbs_sport ON arbs (sport, level, is_arb, best_edge);
        CREATE INDEX IF NOT EXISTS idx_price_ticks_time ON price_ticks (observed_at);
        CREATE INDEX IF NOT EXISTS idx_arb_history_time ON arb_history (observed_at, is_arb);
        CREATE INDEX IF NOT EXISTS idx_book_ticks_time ON book_ticks (observed_at, is_exec);
        """
    )
    _add_columns(
        conn,
        "matches",
        [
            ("kalshi_event_id", "TEXT"),
            ("kalshi_id_a", "TEXT"),
            ("kalshi_id_b", "TEXT"),
            ("poly_id", "TEXT"),
            ("poly_event_id", "TEXT"),
            ("poly_token_a", "TEXT"),
            ("poly_token_b", "TEXT"),
        ],
    )
    for table in ("markets", "matches", "arbs", "price_ticks", "arb_history", "book_ticks"):
        _add_columns(conn, table, [("live", "INTEGER"), ("ended", "INTEGER")])
    return conn


def _dedupe(rows: list[dict], keys: tuple[str, ...], label: str) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for row in rows:
        unique[tuple(row.get(name) for name in keys)] = row
    dropped = len(rows) - len(unique)
    if dropped:
        print(f"Dropped {dropped} duplicate {label}")
    return list(unique.values())


def save_db(markets: list[dict], matches: list[dict], arbs: list[dict]) -> None:
    conn = connect()
    observed_at = datetime.now(timezone.utc).isoformat()
    for row in markets:
        row.setdefault("live", 0)
        row.setdefault("ended", 0)
        if row.get("market_id") is not None:
            row["market_id"] = str(row["market_id"])
    for row in matches:
        row.setdefault("live", 0)
        row.setdefault("ended", 0)
        row.setdefault("poly_token_a", None)
        row.setdefault("poly_token_b", None)
    for row in arbs:
        row.setdefault("live", 0)
        row.setdefault("ended", 0)
    markets = _dedupe(markets, ("sport", "exchange", "market_id", "team"), "markets")
    matches = _dedupe(matches, ("sport", "game_date", "team_a", "team_b"), "matches")
    arbs = _dedupe(arbs, ("sport", "game_date", "team_a", "team_b"), "arbs")
    try:
        conn.execute("DELETE FROM markets")
        conn.execute("DELETE FROM matches")
        conn.execute("DELETE FROM arbs")
        conn.executemany(
            """
            INSERT INTO markets (
                sport, level, exchange, market_id, event_id, event_title, team, opponent,
                yes_price, volume, game_date, close_time, url, live, ended
            ) VALUES (
                :sport, :level, :exchange, :market_id, :event_id, :event_title, :team, :opponent,
                :yes_price, :volume, :game_date, :close_time, :url, :live, :ended
            )
            """,
            markets,
        )
        conn.executemany(
            """
            INSERT INTO matches (
                sport, level, game_date, poly_game_date, team_a, team_b, kalshi_event, polymarket_event,
                kalshi_event_id, kalshi_id_a, kalshi_id_b, poly_id, poly_event_id,
                poly_token_a, poly_token_b,
                kalshi_yes_a, poly_yes_a, kalshi_yes_b, poly_yes_b, kalshi_url, polymarket_url,
                live, ended
            ) VALUES (
                :sport, :level, :game_date, :poly_game_date, :team_a, :team_b, :kalshi_event, :polymarket_event,
                :kalshi_event_id, :kalshi_id_a, :kalshi_id_b, :poly_id, :poly_event_id,
                :poly_token_a, :poly_token_b,
                :kalshi_yes_a, :poly_yes_a, :kalshi_yes_b, :poly_yes_b, :kalshi_url, :polymarket_url,
                :live, :ended
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
                kalshi_url, polymarket_url, live, ended
            ) VALUES (
                :sport, :level, :game_date, :poly_game_date, :team_a, :team_b, :kalshi_event, :polymarket_event,
                :kalshi_yes_a, :poly_yes_a, :team_a_price_diff, :kalshi_yes_b, :poly_yes_b, :team_b_price_diff,
                :kalshi_a_plus_poly_b, :poly_a_plus_kalshi_b, :best_cost, :best_edge, :is_arb, :best_trade,
                :kalshi_url, :polymarket_url, :live, :ended
            )
            """,
            arbs,
        )
        hits = [row for row in arbs if row.get("is_arb")]
        for row in hits:
            conn.execute(
                """
                INSERT INTO arb_history (
                    observed_at, source, sport, level, game_date, poly_game_date, team_a, team_b,
                    kalshi_event, polymarket_event, kalshi_yes_a, poly_yes_a, team_a_price_diff,
                    kalshi_yes_b, poly_yes_b, team_b_price_diff, kalshi_a_plus_poly_b, poly_a_plus_kalshi_b,
                    best_cost, best_edge, is_arb, best_trade, kalshi_url, polymarket_url, live, ended
                ) VALUES (
                    :observed_at, :source, :sport, :level, :game_date, :poly_game_date, :team_a, :team_b,
                    :kalshi_event, :polymarket_event, :kalshi_yes_a, :poly_yes_a, :team_a_price_diff,
                    :kalshi_yes_b, :poly_yes_b, :team_b_price_diff, :kalshi_a_plus_poly_b, :poly_a_plus_kalshi_b,
                    :best_cost, :best_edge, :is_arb, :best_trade, :kalshi_url, :polymarket_url, :live, :ended
                )
                """,
                {**row, "observed_at": observed_at, "source": "discovery"},
            )
        conn.commit()
    finally:
        conn.close()


def print_summary(markets: list[dict], matches: list[dict], arbs: list[dict]) -> None:
    hits = [row for row in arbs if row["is_arb"]]
    live_n = sum(1 for row in matches if game_status(row) == "live")
    ended_n = sum(1 for row in matches if game_status(row) == "ended")
    print(
        f"Wrote {DB_PATH.name}: {len(markets)} markets, {len(matches)} matches "
        f"({live_n} live, {ended_n} final), {len(hits)} arbs / {len(arbs)} compared"
    )
    for row in matches[:8]:
        tag = f" {game_status(row)}" if game_status(row) in {"live", "ended"} else ""
        label = row["sport"] if row["sport"] != "tennis" else f"tennis/{row['level']}"
        print(
            f"  [{label}]{tag} {row['game_date']} {row['team_a']} vs {row['team_b']}: "
            f"Kalshi {row['kalshi_yes_a']}/{row['kalshi_yes_b']}  "
            f"Poly {row['poly_yes_a']}/{row['poly_yes_b']}"
        )
    if len(matches) > 8:
        print(f"  ... {len(matches) - 8} more")
    if hits:
        print("Arbs (pre-fee):")
        for row in hits[:12]:
            tag = " LIVE" if game_status(row) == "live" else ""
            label = row["sport"] if row["sport"] != "tennis" else f"tennis/{row['level']}"
            print(
                f"  [{label}]{tag} {row['game_date']} {row['best_trade']}  "
                f"cost={row['best_cost']} edge={row['best_edge']}"
            )
        if len(hits) > 12:
            print(f"  ... {len(hits) - 12} more")
    else:
        print("No pre-fee arbs this run (best edges are in the arbs table).")


def _cents(price: float) -> str:
    cents = price * 100
    if abs(cents - round(cents)) < 0.05:
        return f"{int(round(cents))}c"
    return f"{cents:.1f}c"


def _size(qty: float) -> str:
    if abs(qty - round(qty)) < 1e-6:
        return str(int(round(qty)))
    return f"{qty:.2f}"


def print_executable(rows: list[dict]) -> None:
    hits = [row for row in rows if row.get("is_exec")]
    hits.sort(key=lambda row: row["edge"], reverse=True)
    print()
    print("Executable at snapshot (gross, no fees; not guaranteed arbitrage):")
    if not hits:
        print("  none")
    for row in hits:
        tag = " LIVE" if game_status(row) == "live" else ""
        print()
        print(f"GAME: {row['team_a']} vs {row['team_b']}{tag}")
        print(f"Direction: {row['direction']}")
        print()
        print(f"Kalshi ask: {_cents(row['kalshi_ask'])}")
        print(f"Polymarket ask: {_cents(row['poly_ask'])}")
        print(f"Combined cost: {_cents(row['combined_best'])}")
        if abs(row["avg_cost"] - row["combined_best"]) >= 0.0005:
            print(f"Avg combined cost: {_cents(row['avg_cost'])}")
        print(f"Max size: {_size(row['qty'])}")
        print(f"Gross edge: {_cents(row['edge'])}")
        print(f"Gross profit: ${row['profit']:.2f}")

    misses = [row for row in rows if not row.get("is_exec")]
    misses.sort(key=lambda row: row["combined_best"])
    print()
    print("Closest (not executable at snapshot):")
    if not misses:
        print("  none")
        return
    for row in misses[:5]:
        label = row["sport"] if row.get("sport") != "tennis" else f"tennis/{row.get('level')}"
        print(
            f"  [{label}] {row['team_a']} vs {row['team_b']}: "
            f"Kalshi {_cents(row['kalshi_ask'])} + Poly {_cents(row['poly_ask'])} "
            f"= {_cents(row['combined_best'])}"
        )


def discover(scan_books: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    all_markets: list[dict] = []
    all_matches: list[dict] = []
    all_arbs: list[dict] = []
    for sport, module, label, level in SPORTS:
        print(f"Fetching Kalshi {label} moneylines...")
        kalshi = module.fetch_kalshi()
        print(f"  {len(kalshi)} contracts{_level_counts(kalshi)}")
        print(f"Fetching Polymarket {label} moneylines...")
        poly = module.fetch_polymarket()
        print(f"  {len(poly)} outcomes{_level_counts(poly)}")
        matches = match_markets(
            kalshi,
            poly,
            sport,
            level,
            names_equal=getattr(module, "names_equal", None),
        )
        arbs = find_arbs(matches)
        all_markets.extend(kalshi + poly)
        all_matches.extend(matches)
        all_arbs.extend(arbs)
    save_db(all_markets, all_matches, all_arbs)
    print_summary(all_markets, all_matches, all_arbs)
    if scan_books:
        print_executable(scan_executable(all_matches))
    return all_markets, all_matches, all_arbs


def _level_counts(rows: list[dict]) -> str:
    levels = Counter(row.get("level") for row in rows if row.get("level"))
    if len(levels) <= 1:
        return ""
    detail = ", ".join(f"{name} {n}" for name, n in sorted(levels.items()))
    return f" ({detail})"


def main() -> None:
    discover()


if __name__ == "__main__":
    main()
