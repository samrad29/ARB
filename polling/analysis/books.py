"""Summarize executable-at-snapshot opportunities from `book_ticks`.

Gross dollars use each match's best snapshot (peak exec_profit), not the sum
of every poll. That is "fill once at the fattest print," before fees.

Run:  python -m polling.analysis.books
      python -m polling.analysis.books path/to/moneyline.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from statistics import median

from main import DB_PATH, connect
from polling.analysis.stats import (
    CENT,
    CENT_THRESHOLDS,
    LARGEST_N,
    SPORT_ORDER,
    TENNIS_LEVELS,
    duration_stats,
    find_episodes,
    format_cents,
    format_duration,
    format_ts,
    gap_break_seconds,
    poll_gaps,
    typical_poll_seconds,
    unique_matches,
)
from util import game_status, parse_dt

PROFIT_BUCKETS = (
    ("under $1", None, 1.0),
    ("$1-$10", 1.0, 10.0),
    ("$10-$100", 10.0, 100.0),
    ("$100+", 100.0, None),
)


def load_book_ticks(conn: sqlite3.Connection) -> list[dict]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "book_ticks" not in tables:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT observed_at, sport, level, game_date, team_a, team_b,
               best_cost, best_edge, is_exec, best_trade, exec_qty, exec_profit,
               live, ended
        FROM book_ticks
        ORDER BY observed_at, sport, team_a, team_b
        """
    ).fetchall()
    ticks = []
    for row in rows:
        item = dict(row)
        ts = parse_dt(item["observed_at"])
        if ts is None:
            continue
        item["_ts"] = ts
        item["closed"] = 0
        item["is_arb"] = 1 if item.get("is_exec") else 0
        ticks.append(item)
    return ticks


def _qualifies(tick: dict, min_edge: float, inclusive: bool) -> bool:
    if not tick.get("is_exec"):
        return False
    edge = tick.get("best_edge")
    if edge is None:
        return False
    return edge >= min_edge if inclusive else edge > min_edge


def _match_key(tick: dict) -> tuple:
    return (tick.get("sport"), tick.get("game_date"), tick.get("team_a"), tick.get("team_b"))


def best_snapshots(ticks: list[dict], min_edge: float, inclusive: bool = True) -> list[dict]:
    """One row per game: fattest executable print at this edge threshold."""
    best: dict[tuple, dict] = {}
    for tick in ticks:
        if not _qualifies(tick, min_edge, inclusive):
            continue
        key = _match_key(tick)
        profit = float(tick.get("exec_profit") or 0)
        qty = float(tick.get("exec_qty") or 0)
        edge = float(tick.get("best_edge") or 0)
        row = best.get(key)
        if row is None:
            best[key] = {
                "sport": tick.get("sport"),
                "level": tick.get("level"),
                "game_date": tick.get("game_date"),
                "team_a": tick.get("team_a"),
                "team_b": tick.get("team_b"),
                "best_trade": tick.get("best_trade"),
                "peak_profit": profit,
                "peak_qty": qty,
                "peak_edge": edge,
                "exec_ticks": 1,
                "live": tick.get("live"),
                "ended": tick.get("ended"),
                "status": game_status(tick),
            }
            continue
        row["exec_ticks"] += 1
        if profit > row["peak_profit"] or (profit == row["peak_profit"] and edge > row["peak_edge"]):
            row["peak_profit"] = profit
            row["peak_qty"] = qty
            row["peak_edge"] = edge
            row["best_trade"] = tick.get("best_trade")
        if game_status(tick) == "live":
            row["live"] = 1
            row["status"] = "live"
    rows = list(best.values())
    rows.sort(key=lambda item: (-item["peak_profit"], -item["peak_edge"], item["team_a"] or ""))
    return rows


def summarize_snapshots(rows: list[dict]) -> dict:
    profits = [row["peak_profit"] for row in rows]
    qtys = [row["peak_qty"] for row in rows]
    buckets = []
    for label, low, high in PROFIT_BUCKETS:
        group = [
            row
            for row in rows
            if (low is None or row["peak_profit"] >= low) and (high is None or row["peak_profit"] < high)
        ]
        buckets.append(
            {
                "label": label,
                "matches": len(group),
                "gross_profit": sum(item["peak_profit"] for item in group),
                "gross_qty": sum(item["peak_qty"] for item in group),
            }
        )
    return {
        "matches": len(rows),
        "gross_profit": sum(profits) if profits else 0.0,
        "gross_qty": sum(qtys) if qtys else 0.0,
        "median_profit": float(median(profits)) if profits else None,
        "median_qty": float(median(qtys)) if qtys else None,
        "max_profit": max(profits) if profits else None,
        "by_profit": buckets,
        "rows": rows,
    }


def _edge_block(ticks: list[dict], min_edge: float, inclusive: bool) -> dict:
    snaps = summarize_snapshots(best_snapshots(ticks, min_edge, inclusive))
    episodes = find_episodes(ticks, min_edge, inclusive=inclusive)
    snaps["episodes"] = episodes
    snaps["duration"] = duration_stats(episodes)
    snaps["exec_ticks"] = sum(1 for tick in ticks if _qualifies(tick, min_edge, inclusive))
    return snaps


def sport_slices(ticks: list[dict]) -> list[tuple[str, list[dict]]]:
    sports = {tick.get("sport") for tick in ticks if tick.get("sport")}
    ordered = [sport for sport in SPORT_ORDER if sport in sports]
    ordered.extend(sorted(sports - set(SPORT_ORDER)))
    slices: list[tuple[str, list[dict]]] = []
    for sport in ordered:
        st_ticks = [tick for tick in ticks if tick.get("sport") == sport]
        slices.append((sport, st_ticks))
        if sport != "tennis":
            continue
        levels = {tick.get("level") for tick in st_ticks if tick.get("level")}
        level_order = [level for level in TENNIS_LEVELS if level in levels]
        level_order.extend(sorted(levels - set(TENNIS_LEVELS)))
        for level in level_order:
            lv_ticks = [tick for tick in st_ticks if tick.get("level") == level]
            if lv_ticks:
                slices.append((f"tennis/{level}", lv_ticks))
    return slices


def analyze(db_path: Path | None = None) -> dict:
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(path) if db_path else connect()
    try:
        ticks = load_book_ticks(conn)
    finally:
        conn.close()

    times = [tick["_ts"] for tick in ticks]
    gaps = poll_gaps(ticks)
    quoted = unique_matches(ticks)
    all_exec = _edge_block(ticks, 0.0, inclusive=False)
    by_cents = {cents: _edge_block(ticks, cents * CENT, inclusive=True) for cents in CENT_THRESHOLDS}
    by_sport = {}
    for label, st_ticks in sport_slices(ticks):
        by_sport[label] = {
            "quoted_matches": len(unique_matches(st_ticks)),
            "ticks": len(st_ticks),
            "all": _edge_block(st_ticks, 0.0, inclusive=False),
            "by_cents": {
                cents: _edge_block(st_ticks, cents * CENT, inclusive=True) for cents in CENT_THRESHOLDS
            },
        }
    return {
        "db_path": str(path),
        "ticks": len(ticks),
        "quoted_matches": len(quoted),
        "window_start": min(times) if times else None,
        "window_end": max(times) if times else None,
        "poll_cycles": len({tick["observed_at"] for tick in ticks}),
        "typical_poll_seconds": typical_poll_seconds(ticks) if ticks else None,
        "gap_break_seconds": gap_break_seconds(ticks) if ticks else None,
        "max_poll_gap": max(gaps) if gaps else None,
        "all": all_exec,
        "by_cents": by_cents,
        "by_sport": by_sport,
    }


def format_dollars(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def format_qty(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value - round(value)) < 1e-6:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def _game(row: dict) -> str:
    sport = row.get("sport") or "?"
    if sport == "tennis" and row.get("level"):
        sport = f"tennis/{row['level']}"
    date = row.get("game_date") or "?"
    return f"[{sport}] {date} {row.get('team_a')} vs {row.get('team_b')}"


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    if n == 1:
        return singular
    return plural or singular + "s"


def _print_money_line(label: str, block: dict, indent: str = "    ") -> None:
    print(
        f"{indent}{label:<10}  {block['matches']} {_plural(block['matches'], 'match', 'matches')}  "
        f"{format_qty(block['gross_qty'])} contracts  {format_dollars(block['gross_profit'])} gross"
    )


def print_report(report: dict) -> None:
    print(f"Book polling analysis  ({report['db_path']})")
    if not report["ticks"]:
        print("No book_ticks yet. Run `python -m polling.books` first.")
        return
    window = (report["window_end"] - report["window_start"]).total_seconds() if report["window_start"] else 0
    print(
        f"Window: {format_ts(report['window_start'])} -> {format_ts(report['window_end'])}  "
        f"({format_duration(window)}, {report['poll_cycles']} cycles, "
        f"~{report['typical_poll_seconds']:.1f}s poll)"
    )
    print(
        f"Quoted matches: {report['quoted_matches']}   ticks: {report['ticks']}   "
        f"exec ticks: {report['all']['exec_ticks']}"
    )
    print(
        "Gross $ = sum of each match's best snapshot (peak exec_profit). "
        "Assumes one fill at that print. No fees."
    )
    print()

    print("By sport / league, then by edge:")
    for sport, bucket in report["by_sport"].items():
        indent = "    " if sport.startswith("tennis/") else "  "
        print(
            f"{indent}{sport:<16}  {bucket['quoted_matches']} "
            f"{_plural(bucket['quoted_matches'], 'match', 'matches')} quoted, "
            f"{bucket['all']['matches']} with exec"
        )
        _print_money_line("all exec", bucket["all"], indent + "  ")
        for cents, block in bucket["by_cents"].items():
            _print_money_line(f">= {cents}c", block, indent + "  ")
        if not sport.startswith("tennis/"):
            filled = [row for row in bucket["all"]["by_profit"] if row["matches"]]
            if filled:
                print(f"{indent}  Profit buckets (best snapshot, all exec):")
                for row in filled:
                    print(
                        f"{indent}    {row['label']:<10}  {row['matches']} "
                        f"{_plural(row['matches'], 'match', 'matches')}  "
                        f"{format_dollars(row['gross_profit'])}"
                    )
        print()

    print("Edge size (all sports, best snapshot per match):")
    _print_money_line("all exec", report["all"], "  ")
    for cents, block in report["by_cents"].items():
        _print_money_line(f">= {cents}c", block, "  ")
    print()

    print("Profit buckets by edge (all sports):")
    print(f"  {'bucket':<10}  {'all exec':>22}  {'>= 1c':>22}  {'>= 2c':>22}  {'>= 3c':>22}")
    for i, label in enumerate([row[0] for row in PROFIT_BUCKETS]):
        cells = []
        for block in (report["all"], report["by_cents"][1], report["by_cents"][2], report["by_cents"][3]):
            row = block["by_profit"][i]
            cells.append(f"{row['matches']:>3}  {format_dollars(row['gross_profit']):>9}")
        print(f"  {label:<10}  {cells[0]:>22}  {cells[1]:>22}  {cells[2]:>22}  {cells[3]:>22}")
    print()

    largest = report["all"]["rows"][:LARGEST_N]
    print(f"Largest by gross $ (top {len(largest)}):")
    if not largest:
        print("  none")
    for row in largest:
        live_tag = "  LIVE" if row.get("status") == "live" else ""
        print(
            f"  {format_dollars(row['peak_profit']):>10}  {format_qty(row['peak_qty']):>10} qty  "
            f"{format_cents(row['peak_edge']):>7}  {_game(row)}{live_tag}"
        )
    print()
    stats = report["all"]["duration"]
    print("Executable episode duration (edge > 0):")
    if not stats["n"]:
        print("  none")
    else:
        print(
            f"  n={stats['n']}  mean={format_duration(stats['mean'])}  "
            f"median={format_duration(stats['median'])}  "
            f"min={format_duration(stats['min'])}  max={format_duration(stats['max'])}"
        )


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print_report(analyze(db_path))


if __name__ == "__main__":
    main()
