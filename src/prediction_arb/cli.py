from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from prediction_arb.collectors.runner import CollectorRunner, ScanStats
from prediction_arb.config import Settings, load_settings
from prediction_arb.database.connection import connect, initialize_database, journal_mode
from prediction_arb.database.repositories import SqliteRepositories
from prediction_arb.exchanges.kalshi import KalshiExchange
from prediction_arb.exchanges.polymarket import PolymarketExchange
from prediction_arb.http_client import HttpClient
from prediction_arb.logging_utils import setup_logging
from prediction_arb.metrics import MetricsRegistry, format_stats
from prediction_arb.money import format_cents, format_quantity
from prediction_arb.rate_limit import RateLimiter
from prediction_arb.reporting import opportunity_history_report

BANNER = """========================================================
PREDICTION MARKET ARBITRAGE SCANNER
========================================================"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="prediction_arb",
        description="Research scanner for prediction-market arbitrage. No live trading.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create/migrate the SQLite database")
    sub.add_parser("markets", help="Show active markets by exchange")
    sub.add_parser("scan", help="Run one collection + arbitrage scan")
    sub.add_parser("opportunities", help="Show currently open opportunities")
    sub.add_parser("history", help="Show historical opportunity statistics")
    sub.add_parser("stats", help="Show API / watchlist / WebSocket statistics")
    run_parser = sub.add_parser("run", help="Discover slowly; stream/poll the watchlist")
    run_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Discovery interval in seconds (overrides DISCOVERY_INTERVAL_SECONDS)",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_json)

    if args.command == "init":
        _init_db(settings)
        return
    if args.command == "markets":
        _cmd_markets(settings)
        return
    if args.command == "opportunities":
        _cmd_opportunities(settings)
        return
    if args.command == "history":
        _cmd_history(settings)
        return
    if args.command == "stats":
        _cmd_stats(settings)
        return
    if args.command == "scan":
        asyncio.run(_cmd_scan(settings))
        return
    if args.command == "run":
        interval = (
            args.interval if args.interval is not None else settings.discovery_interval_seconds
        )
        asyncio.run(_cmd_run(settings, interval))
        return
    parser.error(f"unknown command {args.command}")


def _init_db(settings: Settings) -> None:
    connection = connect(settings.database_path)
    initialize_database(connection)
    mode = journal_mode(connection)
    connection.close()
    print(f"Database ready at {settings.database_path} (journal_mode={mode})")


def _open_repos(settings: Settings) -> tuple[object, SqliteRepositories]:
    connection = connect(settings.database_path)
    initialize_database(connection)
    return connection, SqliteRepositories(connection)


def _cmd_markets(settings: Settings) -> None:
    connection, repos = _open_repos(settings)
    try:
        counts = repos.count_active_by_exchange()
        print(BANNER)
        print()
        print("Active markets")
        for name in settings.exchange_names:
            print(f"  {name.capitalize():<12} {counts.get(name, 0):,}")
        if not counts:
            print("  (none stored yet — run `python -m prediction_arb.cli scan`)")
            return
        print()
        for name in settings.exchange_names:
            rows = repos.list_active(name)
            if not rows:
                continue
            print(f"{name.capitalize()} sample")
            for row in rows[:10]:
                print(f"  {row['ticker'] or row['exchange_market_id']}: {row['title']}")
            if len(rows) > 10:
                print(f"  ... {len(rows) - 10:,} more")
            print()
    finally:
        connection.close()


def _cmd_opportunities(settings: Settings) -> None:
    connection, repos = _open_repos(settings)
    try:
        print(BANNER)
        print()
        rows = repos.open_opportunities()
        matches = repos.list_matches()
        high = [
            row
            for row in matches
            if row["match_type"] in {"EXACT", "LIKELY_EQUIVALENT"}
            and row["match_score"] >= settings.match_high_confidence_min_score
        ]
        print(f"Candidate matches:          {len(matches):,}")
        print(f"High-confidence matches:    {len(high):,}")
        print(f"Current opportunities:       {len(rows):,}")
        print()
        if not rows:
            print("No executable net-profitable opportunities are currently open.")
            return
        for row in rows:
            _print_opportunity_row(row)
    finally:
        connection.close()


def _cmd_history(settings: Settings) -> None:
    connection, repos = _open_repos(settings)
    try:
        print(opportunity_history_report(repos.all_opportunities()))
    finally:
        connection.close()


def _cmd_stats(settings: Settings) -> None:
    path = settings.metrics_path
    if not path.exists():
        print("No collector stats yet. Run `python -m prediction_arb.cli scan` or `run` first.")
        print(f"Expected file: {path}")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(format_stats(payload))


async def _cmd_scan(settings: Settings) -> None:
    connection, repos = _open_repos(settings)
    metrics = MetricsRegistry()
    http = _http(settings, metrics)
    try:
        runner = CollectorRunner(settings, repos, _build_exchanges(settings, http), metrics=metrics)
        stats = await runner.scan_once()
        _print_scan_stats(settings, repos, stats)
        print(format_stats(metrics.snapshot()))
    finally:
        await http.aclose()
        connection.close()


async def _cmd_run(settings: Settings, discovery_interval: float) -> None:
    settings.discovery_interval_seconds = discovery_interval
    connection, repos = _open_repos(settings)
    metrics = MetricsRegistry()
    http = _http(settings, metrics)
    runner = CollectorRunner(settings, repos, _build_exchanges(settings, http), metrics=metrics)
    log = logging.getLogger("prediction_arb.cli")
    log.info(
        "continuous_start discovery=%ss snapshot=%ss high_poll=%ss",
        settings.discovery_interval_seconds,
        settings.orderbook_snapshot_interval_seconds,
        settings.poll_interval_seconds,
    )
    try:
        await runner.run_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        print(format_stats(metrics.snapshot()))
    finally:
        await http.aclose()
        connection.close()


def _http(settings: Settings, metrics: MetricsRegistry) -> HttpClient:
    limiter = RateLimiter(metrics)
    return HttpClient(
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        limiter=limiter,
        metrics=metrics,
    )


def _build_exchanges(settings: Settings, http: HttpClient) -> list:
    exchanges = []
    names = set(settings.exchange_names)
    if "kalshi" in names:
        exchanges.append(
            KalshiExchange(
                http,
                base_url=settings.kalshi_base_url,
                max_markets=settings.max_markets_per_exchange,
            )
        )
    if "polymarket" in names:
        exchanges.append(
            PolymarketExchange(
                http,
                gamma_url=settings.polymarket_gamma_url,
                clob_url=settings.polymarket_clob_url,
                max_markets=settings.max_markets_per_exchange,
            )
        )
    if not exchanges:
        raise SystemExit("No enabled exchanges. Set ENABLED_EXCHANGES=kalshi,polymarket")
    return exchanges


def _print_scan_stats(settings: Settings, repos: SqliteRepositories, stats: ScanStats) -> None:
    print(BANNER)
    print()
    print("Active markets")
    for name in settings.exchange_names:
        print(f"  {name.capitalize():<12} {stats.markets_by_exchange.get(name, 0):,}")
    print()
    print(f"Candidate matches:          {stats.candidate_matches:,}")
    print(f"High-confidence matches:    {stats.high_confidence_matches:,}")
    print(f"Watchlist (high):           {stats.watchlist_high:,}")
    print(f"Watchlist (candidate):      {stats.watchlist_candidate:,}")
    print(f"Current opportunities:       {len(stats.current_opportunities):,}")
    print(f"Newly stored:                {stats.opportunities_detected:,}")
    print(f"Newly expired:               {stats.opportunities_expired:,}")
    print(f"Collection duration:         {stats.duration_seconds:.2f}s")
    print()
    print("--------------------------------------------------------")
    print()
    if not stats.current_opportunities:
        print("No executable net-profitable opportunities this scan.")
        print()
        return
    for opportunity in stats.current_opportunities:
        print(opportunity.details.get("yes_title") or "Matched markets")
        print()
        print(f"{opportunity.yes_exchange.capitalize()} YES")
        print(f"  Ask:       {format_cents(opportunity.yes_ask)}")
        print(f"  Available: {format_quantity(opportunity.yes_ask_size)}")
        print()
        print(f"{opportunity.no_exchange.capitalize()} NO")
        print(f"  Ask:       {format_cents(opportunity.no_ask)}")
        print(f"  Available: {format_quantity(opportunity.no_ask_size)}")
        print()
        print(f"Max quantity:          {format_quantity(opportunity.max_quantity)}")
        print(f"Capital required:     {format_cents(opportunity.capital_required)}")
        print(f"Guaranteed payout:    {format_cents(opportunity.guaranteed_payout)}")
        print(f"Gross profit:         {format_cents(opportunity.gross_profit)}")
        print(f"Estimated fees:       {format_cents(opportunity.estimated_fees)}")
        print(f"Net profit:           {format_cents(opportunity.net_profit)}")
        print(f"ROI:                  {opportunity.roi_bps / 100:.2f}%")
        if opportunity.match_score is not None:
            print(f"Match confidence:     {opportunity.match_score * 100:.1f}%")
        if opportunity.match_reason:
            print(f"Match reason:         {opportunity.match_reason}")
        print()
        print("--------------------------------------------------------")
        print()


def _print_opportunity_row(row: dict) -> None:
    details = {}
    if row.get("details_json"):
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            details = {}
    print(details.get("yes_title") or row.get("market_a_title") or "Matched markets")
    print()
    print(f"Strategy:              {row['strategy']}")
    print(f"YES market:            {row.get('market_a_exchange')} {row.get('market_a_title')}")
    print(f"NO market:             {row.get('market_b_exchange')} {row.get('market_b_title')}")
    print(f"YES ask:               {format_cents(row.get('yes_ask'))}")
    print(f"NO ask:                {format_cents(row.get('no_ask'))}")
    print(f"Max quantity:          {format_quantity(row['max_quantity'])}")
    print(f"Capital required:     {format_cents(row['capital_required'])}")
    print(f"Guaranteed payout:    {format_cents(row['guaranteed_payout'])}")
    print(f"Gross profit:         {format_cents(row['gross_profit'])}")
    print(f"Estimated fees:       {format_cents(row['estimated_fees'])}")
    print(f"Net profit:           {format_cents(row['net_profit'])}")
    print(f"ROI:                  {row['roi'] / 100:.2f}%")
    print(f"Detected at:           {row['detected_at']}")
    print()
    print("--------------------------------------------------------")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
