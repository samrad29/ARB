"""Poll live Kalshi / Polymarket order books for executable-at-snapshot arbs.

Run:  python -m polling.books
      python -m polling.books --minutes 60

Last-price quotes stay on `python -m polling`.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from main import connect, discover, quote_executable
from util import game_status

TARGET_CYCLE_SECONDS = 8
DISCOVERY_SECONDS = 600
QUOTE_WORKERS = 16


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_matches(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sport, level, game_date, poly_game_date, team_a, team_b,
               kalshi_event, polymarket_event, kalshi_url, polymarket_url,
               kalshi_event_id, kalshi_id_a, kalshi_id_b, poly_id, poly_event_id,
               poly_token_a, poly_token_b,
               live, ended
        FROM matches
        """
    ).fetchall()
    return [dict(row) for row in rows]


def save_tick(conn: sqlite3.Connection, row: dict, observed_at: str) -> None:
    conn.execute(
        """
        INSERT INTO book_ticks (
            observed_at, sport, level, game_date, team_a, team_b,
            kalshi_yes_a, poly_yes_a, kalshi_yes_b, poly_yes_b,
            best_cost, best_edge, is_exec, best_trade, exec_qty, exec_profit,
            live, ended
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            row.get("is_exec") or 0,
            row.get("best_trade"),
            row.get("exec_qty") or 0,
            row.get("exec_profit") or 0,
            row.get("live") or 0,
            row.get("ended") or 0,
        ),
    )


def poll_once(conn: sqlite3.Connection) -> None:
    matches = [row for row in load_matches(conn) if game_status(row) != "ended"]
    observed_at = utc_now()
    started = time.monotonic()
    hits = 0
    live_hits = 0
    pre_hits = 0
    quoted = 0
    print(f"Polling books for {len(matches)} matches at {observed_at}...")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=QUOTE_WORKERS) as pool:
        futures = [pool.submit(quote_executable, match) for match in matches]
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                rows.append(row)
    misses = []
    for row in rows:
        quoted += 1
        save_tick(conn, row, observed_at)
        if row.get("is_exec"):
            hits += 1
            if game_status(row) == "live":
                live_hits += 1
            else:
                pre_hits += 1
            tag = " LIVE" if game_status(row) == "live" else ""
            label = row["sport"] if row.get("sport") != "tennis" else f"tennis/{row.get('level')}"
            print(
                f"  EXEC [{label}]{tag} {row['team_a']} vs {row['team_b']}  "
                f"{row.get('best_trade')}  cost={row.get('best_cost')} "
                f"size={row.get('exec_qty')} edge={row.get('best_edge')}  "
                f"(executable at snapshot)"
            )
        elif row.get("best_cost") is not None:
            misses.append(row)
    conn.commit()
    if not hits and misses:
        misses.sort(key=lambda row: row["best_cost"])
        print("  Closest (not executable at snapshot):")
        for row in misses[:3]:
            label = row["sport"] if row.get("sport") != "tennis" else f"tennis/{row.get('level')}"
            print(
                f"    [{label}] {row['team_a']} vs {row['team_b']}: "
                f"{row.get('best_trade')}  cost={row.get('best_cost')}"
            )
    print(
        f"  quoted {quoted}, exec {hits} "
        f"({live_hits} live / {pre_hits} pregame) in {time.monotonic() - started:.1f}s"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll live order books; rediscover about every 10 minutes.")
    parser.add_argument(
        "minutes",
        nargs="?",
        type=float,
        default=None,
        help="Stop after this many minutes. Default: run until Ctrl+C.",
    )
    parser.add_argument(
        "-m",
        "--minutes",
        dest="minutes_flag",
        type=float,
        default=None,
        metavar="MINUTES",
        help="Same as the positional minutes argument.",
    )
    args = parser.parse_args(argv)
    minutes = args.minutes_flag if args.minutes_flag is not None else args.minutes
    if minutes is not None and minutes <= 0:
        parser.error("minutes must be positive")
    args.minutes = minutes
    return args


def main() -> None:
    args = parse_args()
    deadline = None if args.minutes is None else time.monotonic() + args.minutes * 60
    conn = connect()
    try:
        if args.minutes is not None:
            print(f"Running book polling for {args.minutes:g} minutes...")
        print("Initial discovery...")
        discover(scan_books=False)
        last_discovery = time.monotonic()
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("Time limit reached.")
                break
            started = time.monotonic()
            poll_once(conn)
            if deadline is not None and time.monotonic() >= deadline:
                print("Time limit reached.")
                break
            if time.monotonic() - last_discovery >= DISCOVERY_SECONDS:
                print("Rediscovering markets...")
                try:
                    discover(scan_books=False)
                    last_discovery = time.monotonic()
                except Exception as exc:
                    last_discovery = time.monotonic()
                    print(f"Discovery failed ({exc}); continuing with existing matches")
                if deadline is not None and time.monotonic() >= deadline:
                    print("Time limit reached.")
                    break
            wait = TARGET_CYCLE_SECONDS - (time.monotonic() - started)
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
