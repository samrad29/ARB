"""Summarize arb episodes from `price_ticks`.

An episode is a contiguous run for one matched game where edge stays at or
above a threshold. Short poll/discovery gaps stay in the same episode; a
gap of 15+ minutes starts a new one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from main import DB_PATH, connect
from util import game_status, parse_dt

CENT = 0.01
CENT_THRESHOLDS = (1, 2, 3)
LARGEST_N = 5
SESSION_GAP_SECONDS = 15 * 60
STATUS_ORDER = ("live", "pregame", "ended", "unknown")
SPORT_ORDER = ("nfl", "cfb", "tennis")
TENNIS_LEVELS = ("atp", "wta", "itf", "challenger")


def load_ticks(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cols = {row[1] for row in conn.execute("PRAGMA table_info(price_ticks)")}
    extra = ""
    if "live" in cols:
        extra += ", live"
    if "ended" in cols:
        extra += ", ended"
    rows = conn.execute(
        f"""
        SELECT observed_at, sport, level, game_date, team_a, team_b,
               best_cost, best_edge, is_arb, best_trade, closed{extra}
        FROM price_ticks
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
        item.setdefault("live", None)
        item.setdefault("ended", None)
        ticks.append(item)
    return ticks


def poll_gaps(ticks: list[dict]) -> list[float]:
    times = sorted({tick["_ts"] for tick in ticks})
    return [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]


def typical_poll_seconds(ticks: list[dict]) -> float:
    gaps = [gap for gap in poll_gaps(ticks) if gap < 60]
    if not gaps:
        return 8.0
    return float(median(gaps))


def gap_break_seconds(ticks: list[dict] | None = None) -> float:
    return float(SESSION_GAP_SECONDS)


def _active(tick: dict, min_edge: float, inclusive: bool) -> bool:
    if tick.get("closed"):
        return False
    edge = tick.get("best_edge")
    if edge is None:
        return False
    return edge >= min_edge if inclusive else edge > min_edge


def _match_key(tick: dict) -> tuple:
    return (tick.get("sport"), tick.get("game_date"), tick.get("team_a"), tick.get("team_b"))


def find_episodes(
    ticks: list[dict],
    min_edge: float,
    *,
    inclusive: bool = True,
    gap_break: float | None = None,
) -> list[dict]:
    """Contiguous runs where `best_edge` meets `min_edge`."""
    if not ticks:
        return []
    break_after = gap_break_seconds(ticks) if gap_break is None else gap_break
    global_end = max(tick["_ts"] for tick in ticks)
    grouped: dict[tuple, list[dict]] = {}
    for tick in ticks:
        grouped.setdefault(_match_key(tick), []).append(tick)

    episodes: list[dict] = []
    for _key, rows in grouped.items():
        rows = sorted(rows, key=lambda item: item["_ts"])
        current: dict | None = None
        prev: dict | None = None
        for row in rows:
            on = _active(row, min_edge, inclusive)
            row_status = game_status(row)
            gap = (row["_ts"] - prev["_ts"]).total_seconds() if prev else 0.0
            if current and (not on or gap > break_after or row_status != current["status"]):
                if not on and gap <= break_after and row_status == current["status"]:
                    end = row["_ts"]
                else:
                    end = current["last_ts"]
                episodes.append(_close_episode(current, end, still_open=False))
                current = None
            if on:
                edge = float(row["best_edge"])
                if current is None:
                    current = {
                        "sport": row.get("sport"),
                        "level": row.get("level"),
                        "game_date": row.get("game_date"),
                        "team_a": row.get("team_a"),
                        "team_b": row.get("team_b"),
                        "best_trade": row.get("best_trade"),
                        "start": row["_ts"],
                        "last_ts": row["_ts"],
                        "peak_edge": edge,
                        "ticks": 1,
                        "min_edge": min_edge,
                        "inclusive": inclusive,
                        "live": row.get("live"),
                        "ended": row.get("ended"),
                        "status": row_status,
                    }
                else:
                    current["last_ts"] = row["_ts"]
                    current["ticks"] += 1
                    if edge > current["peak_edge"]:
                        current["peak_edge"] = edge
                        current["best_trade"] = row.get("best_trade")
            prev = row
        if current:
            last_ts = current["last_ts"]
            still_open = (global_end - last_ts).total_seconds() <= break_after
            episodes.append(_close_episode(current, last_ts, still_open=still_open))
    episodes.sort(key=lambda item: (-item["peak_edge"], -item["duration_seconds"], item["start"]))
    return episodes


def _close_episode(current: dict, end: datetime, still_open: bool) -> dict:
    start = current["start"]
    duration = max(0.0, (end - start).total_seconds())
    return {
        "sport": current["sport"],
        "level": current["level"],
        "game_date": current["game_date"],
        "team_a": current["team_a"],
        "team_b": current["team_b"],
        "best_trade": current["best_trade"],
        "start": start,
        "end": end,
        "last_ts": current["last_ts"],
        "duration_seconds": duration,
        "peak_edge": current["peak_edge"],
        "ticks": current["ticks"],
        "still_open": still_open,
        "min_edge": current["min_edge"],
        "inclusive": current["inclusive"],
        "live": current.get("live"),
        "ended": current.get("ended"),
        "status": current.get("status") or game_status(current),
    }


def unique_matches(rows: list[dict]) -> set[tuple]:
    return {(row["sport"], row["game_date"], row["team_a"], row["team_b"]) for row in rows}


def match_rollups(episodes: list[dict]) -> list[dict]:
    """One row per game: peak edge and summed episode time."""
    by_match: dict[tuple, dict] = {}
    for episode in episodes:
        key = _match_key(episode)
        row = by_match.get(key)
        if row is None:
            by_match[key] = {
                "sport": episode["sport"],
                "level": episode["level"],
                "game_date": episode["game_date"],
                "team_a": episode["team_a"],
                "team_b": episode["team_b"],
                "best_trade": episode.get("best_trade"),
                "peak_edge": episode["peak_edge"],
                "duration_seconds": episode["duration_seconds"],
                "ticks": episode["ticks"],
                "episodes": 1,
                "still_open": episode["still_open"],
                "start": episode["start"],
                "end": episode["end"],
                "live": episode.get("live"),
                "ended": episode.get("ended"),
                "status": episode.get("status") or game_status(episode),
            }
            continue
        row["duration_seconds"] += episode["duration_seconds"]
        row["ticks"] += episode["ticks"]
        row["episodes"] += 1
        row["still_open"] = row["still_open"] or episode["still_open"]
        if episode["start"] < row["start"]:
            row["start"] = episode["start"]
        if episode["end"] > row["end"]:
            row["end"] = episode["end"]
        if episode["peak_edge"] > row["peak_edge"]:
            row["peak_edge"] = episode["peak_edge"]
            row["best_trade"] = episode.get("best_trade")
            row["live"] = episode.get("live")
            row["ended"] = episode.get("ended")
            row["status"] = episode.get("status") or game_status(episode)
        if (episode.get("status") or game_status(episode)) == "live":
            row["live"] = 1
            row["status"] = "live"
    rolled = list(by_match.values())
    rolled.sort(key=lambda item: (-item["peak_edge"], -item["duration_seconds"], item["start"]))
    return rolled


def duration_stats(episodes: list[dict]) -> dict:
    durations = [row["duration_seconds"] for row in episodes]
    open_n = sum(1 for row in episodes if row["still_open"])
    if not durations:
        return {
            "n": 0,
            "open_n": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
        }
    return {
        "n": len(durations),
        "open_n": open_n,
        "mean": sum(durations) / len(durations),
        "median": float(median(durations)),
        "min": min(durations),
        "max": max(durations),
    }


def status_breakdown(ticks: list[dict], arb_episodes: list[dict]) -> dict:
    """Compare arb frequency while games are live vs pregame."""
    by_status: dict[str, dict] = {}
    for status in STATUS_ORDER:
        st_ticks = [tick for tick in ticks if game_status(tick) == status]
        st_arbs = [tick for tick in st_ticks if tick.get("is_arb")]
        st_eps = [row for row in arb_episodes if (row.get("status") or game_status(row)) == status]
        if not st_ticks and not st_eps:
            continue
        by_status[status] = {
            "ticks": len(st_ticks),
            "arb_ticks": len(st_arbs),
            "arb_rate": (len(st_arbs) / len(st_ticks)) if st_ticks else None,
            "quoted_matches": len(unique_matches(st_ticks)),
            "arb_matches": len(unique_matches(st_arbs)),
            "episodes": st_eps,
            "duration": duration_stats(st_eps),
        }
    return by_status


def _bucket_for(ticks: list[dict], arb_episodes: list[dict]) -> dict:
    arb_ticks = [tick for tick in ticks if tick.get("is_arb")]
    return {
        "ticks": len(ticks),
        "arb_ticks": len(arb_ticks),
        "arb_rate": (len(arb_ticks) / len(ticks)) if ticks else None,
        "quoted_matches": len(unique_matches(ticks)),
        "arb_matches": len(unique_matches(arb_ticks)),
        "episodes": arb_episodes,
        "duration": duration_stats(arb_episodes),
    }


def sport_breakdown(ticks: list[dict], arb_episodes: list[dict]) -> dict:
    """Compare arb frequency by sport (and tennis tour)."""
    sports = {tick.get("sport") for tick in ticks if tick.get("sport")}
    ordered = [sport for sport in SPORT_ORDER if sport in sports]
    ordered.extend(sorted(sports - set(SPORT_ORDER)))
    by_sport: dict[str, dict] = {}
    for sport in ordered:
        st_ticks = [tick for tick in ticks if tick.get("sport") == sport]
        st_eps = [row for row in arb_episodes if row.get("sport") == sport]
        if not st_ticks and not st_eps:
            continue
        bucket = _bucket_for(st_ticks, st_eps)
        if sport == "tennis":
            levels = {tick.get("level") for tick in st_ticks if tick.get("level")}
            level_order = [level for level in TENNIS_LEVELS if level in levels]
            level_order.extend(sorted(levels - set(TENNIS_LEVELS)))
            by_level = {}
            for level in level_order:
                lv_ticks = [tick for tick in st_ticks if tick.get("level") == level]
                lv_eps = [row for row in st_eps if row.get("level") == level]
                if lv_ticks or lv_eps:
                    by_level[level] = _bucket_for(lv_ticks, lv_eps)
            bucket["by_level"] = by_level
        by_sport[sport] = bucket
    return by_sport


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total = int(round(max(0.0, seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_cents(edge: float | None) -> str:
    if edge is None:
        return "n/a"
    return f"{edge * 100:.2f}c"


def format_ts(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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


def analyze(db_path: Path | None = None) -> dict:
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(path) if db_path else connect()
    try:
        ticks = load_ticks(conn)
    finally:
        conn.close()

    arb_episodes = find_episodes(ticks, 0.0, inclusive=False)
    by_cents = {
        cents: find_episodes(ticks, cents * CENT, inclusive=True)
        for cents in CENT_THRESHOLDS
    }
    gaps = poll_gaps(ticks)
    times = [tick["_ts"] for tick in ticks]
    quoted = unique_matches(ticks)
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
        "arb_matches": len(unique_matches(arb_episodes)),
        "arb_episodes": arb_episodes,
        "arb_duration": duration_stats(arb_episodes),
        "by_cents": {
            cents: {
                "matches": len(unique_matches(episodes)),
                "episodes": episodes,
                "duration": duration_stats(episodes),
            }
            for cents, episodes in by_cents.items()
        },
        "largest": match_rollups(arb_episodes)[:LARGEST_N],
        "by_status": status_breakdown(ticks, arb_episodes),
        "by_sport": sport_breakdown(ticks, arb_episodes),
    }


def print_report(report: dict) -> None:
    print(f"Polling analysis  ({report['db_path']})")
    if not report["ticks"]:
        print("No price_ticks yet. Run `python -m polling` first.")
        return
    window = (report["window_end"] - report["window_start"]).total_seconds() if report["window_start"] else 0
    print(
        f"Window: {format_ts(report['window_start'])} -> {format_ts(report['window_end'])}  "
        f"({format_duration(window)}, {report['poll_cycles']} cycles, "
        f"~{report['typical_poll_seconds']:.1f}s poll)"
    )
    print(f"Quoted matches: {report['quoted_matches']}   ticks: {report['ticks']}")
    if report.get("by_status"):
        print("Live vs pregame (arb tick rate = arb quotes / all quotes in that state):")
        for status, bucket in report["by_status"].items():
            rate = f"{bucket['arb_rate'] * 100:.1f}%" if bucket["arb_rate"] is not None else "n/a"
            print(
                f"  {status:<8}  {bucket['quoted_matches']} {_plural(bucket['quoted_matches'], 'match', 'matches')} quoted, "
                f"{bucket['arb_matches']} with arbs, {bucket['arb_ticks']}/{bucket['ticks']} ticks ({rate})"
            )
            if bucket["duration"]["n"]:
                stats = bucket["duration"]
                print(
                    f"           episodes n={stats['n']}  mean={format_duration(stats['mean'])}  "
                    f"median={format_duration(stats['median'])}"
                )
    print()
    print(
        f"Total arbs: {report['arb_matches']} {_plural(report['arb_matches'], 'match', 'matches')}, "
        f"{len(report['arb_episodes'])} {_plural(len(report['arb_episodes']), 'episode')}  (edge > 0)"
    )
    if report.get("by_sport"):
        print("By sport:")
        for sport, bucket in report["by_sport"].items():
            _print_sport_bucket(sport, bucket)
            for level, sub in (bucket.get("by_level") or {}).items():
                _print_sport_bucket(level, sub, indent="    ")
        print()
    print("How many were >1 / >2 / >3 cents (peak edge, inclusive):")
    for cents, bucket in report["by_cents"].items():
        n_ep = len(bucket["episodes"])
        print(
            f"  >= {cents} cent   {bucket['matches']} {_plural(bucket['matches'], 'match', 'matches')}, "
            f"{n_ep} {_plural(n_ep, 'episode')}"
        )
    print()
    print(f"Largest arbs (top {len(report['largest'])}):")
    if not report["largest"]:
        print("  none")
    for row in report["largest"]:
        open_tag = "  still open" if row["still_open"] else ""
        extra = f", {row['episodes']} episodes" if row.get("episodes", 1) > 1 else ""
        status = row.get("status") or game_status(row)
        live_tag = "  LIVE" if status == "live" else ""
        print(
            f"  {format_cents(row['peak_edge']):>7}  {_game(row)}{live_tag}  "
            f"{format_duration(row['duration_seconds'])}  {row['ticks']} ticks{extra}{open_tag}"
        )
    print()
    _print_duration("Average arb duration (all edge > 0 episodes)", report["arb_duration"])
    for cents in (2, 3):
        episodes = report["by_cents"][cents]["episodes"]
        _print_duration(f">= {cents} cent arb duration", report["by_cents"][cents]["duration"], trailing_blank=False)
        for row in episodes[:LARGEST_N]:
            open_tag = "  still open" if row["still_open"] else ""
            live_tag = "  LIVE" if (row.get("status") or game_status(row)) == "live" else ""
            print(
                f"    {format_cents(row['peak_edge']):>7}  {_game(row)}{live_tag}  "
                f"{format_duration(row['duration_seconds'])}  "
                f"{format_ts(row['start'])} -> {format_ts(row['end'])}{open_tag}"
            )
        extra = len(episodes) - LARGEST_N
        if extra > 0:
            print(f"    ... {extra} more")
        print()


def _print_sport_bucket(label: str, bucket: dict, indent: str = "  ") -> None:
    rate = f"{bucket['arb_rate'] * 100:.1f}%" if bucket["arb_rate"] is not None else "n/a"
    print(
        f"{indent}{label:<12}  {bucket['quoted_matches']} {_plural(bucket['quoted_matches'], 'match', 'matches')} quoted, "
        f"{bucket['arb_matches']} with arbs, {bucket['arb_ticks']}/{bucket['ticks']} ticks ({rate})"
    )


def _print_duration(title: str, stats: dict, trailing_blank: bool = True) -> None:
    print(title + ":")
    if not stats["n"]:
        print("  none")
    else:
        open_note = f", {stats['open_n']} still open (duration is a lower bound)" if stats["open_n"] else ""
        print(
            f"  n={stats['n']}{open_note}  mean={format_duration(stats['mean'])}  "
            f"median={format_duration(stats['median'])}  "
            f"min={format_duration(stats['min'])}  max={format_duration(stats['max'])}"
        )
    if trailing_blank:
        print()
