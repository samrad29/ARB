"""Poll matched moneylines for live prices; rediscover markets about every 10 minutes.

Run:  python -m polling
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from main import connect, discover, find_arbs
from polling.prices import kalshi_prices, poly_prices

PRICE_POLL_SECONDS = 30
DISCOVERY_SECONDS = 600


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_matches(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sport, level, game_date, poly_game_date, team_a, team_b,
               kalshi_event, polymarket_event, kalshi_url, polymarket_url,
               kalshi_event_id, kalshi_id_a, kalshi_id_b, poly_id, poly_event_id
        FROM matches
        """
    ).fetchall()
    return [dict(row) for row in rows]


def quote_match(match: dict) -> dict | None:
    kalshi = kalshi_prices(match.get("kalshi_event_id"), match.get("kalshi_id_a"), match.get("kalshi_id_b"))
    poly = poly_prices(match.get("poly_id"), match["sport"], match["team_a"], match["team_b"])
    quoted = {
        **match,
        "kalshi_yes_a": kalshi["yes_a"],
        "kalshi_yes_b": kalshi["yes_b"],
        "poly_yes_a": poly["yes_a"],
        "poly_yes_b": poly["yes_b"],
        "closed": 1 if kalshi["closed"] or poly["closed"] else 0,
    }
    if None in (quoted["kalshi_yes_a"], quoted["kalshi_yes_b"], quoted["poly_yes_a"], quoted["poly_yes_b"]):
        return quoted if quoted["closed"] else None
    scored = find_arbs([quoted])
    if not scored:
        return quoted
    row = scored[0]
    row["closed"] = quoted["closed"]
    return row


def save_tick(conn: sqlite3.Connection, row: dict, observed_at: str) -> None:
    conn.execute(
        """
        INSERT INTO price_ticks (
            observed_at, sport, level, game_date, team_a, team_b,
            kalshi_yes_a, poly_yes_a, kalshi_yes_b, poly_yes_b,
            best_cost, best_edge, is_arb, best_trade, closed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_at,
            row.get("sport"),
            row.get("level"),
            row.get("game_date"),
            row.get("team_a"),
            row.get("team_b"),
            row.get("kalshi_yes_a"),
            row.get("poly_yes_a"),
            row.get("kalshi_yes_b"),
            row.get("poly_yes_b"),
            row.get("best_cost"),
            row.get("best_edge"),
            row.get("is_arb") or 0,
            row.get("best_trade"),
            row.get("closed") or 0,
        ),
    )
    if row.get("is_arb") and not row.get("closed"):
        conn.execute(
            """
            INSERT INTO arb_history (
                observed_at, source, sport, level, game_date, poly_game_date, team_a, team_b,
                kalshi_event, polymarket_event, kalshi_yes_a, poly_yes_a, team_a_price_diff,
                kalshi_yes_b, poly_yes_b, team_b_price_diff, kalshi_a_plus_poly_b, poly_a_plus_kalshi_b,
                best_cost, best_edge, is_arb, best_trade, kalshi_url, polymarket_url
            ) VALUES (
                :observed_at, :source, :sport, :level, :game_date, :poly_game_date, :team_a, :team_b,
                :kalshi_event, :polymarket_event, :kalshi_yes_a, :poly_yes_a, :team_a_price_diff,
                :kalshi_yes_b, :poly_yes_b, :team_b_price_diff, :kalshi_a_plus_poly_b, :poly_a_plus_kalshi_b,
                :best_cost, :best_edge, :is_arb, :best_trade, :kalshi_url, :polymarket_url
            )
            """,
            {**row, "observed_at": observed_at, "source": "poll", "is_arb": 1},
        )


def poll_once(conn: sqlite3.Connection) -> None:
    matches = load_matches(conn)
    observed_at = utc_now()
    hits = 0
    closed = 0
    quoted = 0
    print(f"Polling {len(matches)} matches at {observed_at}...")
    for match in matches:
        row = quote_match(match)
        if row is None:
            continue
        quoted += 1
        if row.get("closed"):
            closed += 1
        if row.get("is_arb") and not row.get("closed"):
            hits += 1
            print(
                f"  ARB [{row['sport']}] {row['team_a']} vs {row['team_b']}  "
                f"{row.get('best_trade')}  cost={row.get('best_cost')} edge={row.get('best_edge')}"
            )
        save_tick(conn, row, observed_at)
    conn.commit()
    print(f"  quoted {quoted}, closed {closed}, arbs {hits}")


def main() -> None:
    conn = connect()
    try:
        print("Initial discovery...")
        discover()
        last_discovery = time.monotonic()
        while True:
            poll_once(conn)
            if time.monotonic() - last_discovery >= DISCOVERY_SECONDS:
                print("Rediscovering markets...")
                discover()
                last_discovery = time.monotonic()
            time.sleep(PRICE_POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
