from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from prediction_arb.arbitrage.detector import ArbitrageDetector
from prediction_arb.collectors.state import LiveState
from prediction_arb.collectors.streams import KalshiOrderbookStream, PolymarketMarketStream, load_private_key_pem
from prediction_arb.collectors.watchlist import (
    HIGH_CONFIDENCE_TYPES,
    TIER_HIGH,
    build_watchlist,
    poll_interval_for_tier,
)
from prediction_arb.config import Settings
from prediction_arb.database.repositories import SqliteRepositories
from prediction_arb.exchanges.base import PredictionMarketExchange
from prediction_arb.logging_utils import log_event
from prediction_arb.matching.candidates import CandidateGenerator, CandidatePair
from prediction_arb.matching.market_matcher import MarketMatcher
from prediction_arb.matching.taxonomy import classify_market
from prediction_arb.metrics import MetricsRegistry
from prediction_arb.models.opportunity import ArbitrageOpportunity, MarketMatch
from prediction_arb.models.orderbook import OrderBook

logger = logging.getLogger("prediction_arb.collectors")


@dataclass
class ScanStats:
    markets_by_exchange: dict[str, int] = field(default_factory=dict)
    orderbooks_collected: int = 0
    generated_candidates: int = 0
    candidate_signals: dict[str, int] = field(default_factory=dict)
    candidate_matches: int = 0
    high_confidence_matches: int = 0
    opportunities_detected: int = 0
    opportunities_expired: int = 0
    opportunities_open: int = 0
    duration_seconds: float = 0.0
    watchlist_high: int = 0
    watchlist_candidate: int = 0
    current_opportunities: list[ArbitrageOpportunity] = field(default_factory=list)


class CollectorRunner:
    """Discovery (slow) + watchlist monitoring (fast). Never polls the whole universe."""

    def __init__(
        self,
        settings: Settings,
        repos: SqliteRepositories,
        exchanges: list[PredictionMarketExchange],
        matcher: MarketMatcher | None = None,
        detector: ArbitrageDetector | None = None,
        metrics: MetricsRegistry | None = None,
        candidate_generator: CandidateGenerator | None = None,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.exchanges = {exchange.name: exchange for exchange in exchanges}
        self.matcher = matcher or MarketMatcher(
            candidate_min_score=settings.match_candidate_min_score,
            high_confidence_min_score=settings.match_high_confidence_min_score,
        )
        self.candidate_generator = candidate_generator or CandidateGenerator(
            date_tolerance_days=settings.candidate_date_tolerance_days,
            lexical_min_score=settings.candidate_lexical_min_score,
            strong_lexical_min_score=settings.candidate_strong_lexical_min_score,
            min_candidate_score=settings.candidate_min_score,
            max_candidates_per_market=settings.max_candidates_per_market,
        )
        self.detector = detector or ArbitrageDetector()
        self.metrics = metrics or MetricsRegistry()
        self.state = LiveState()
        self._poly_stream: PolymarketMarketStream | None = None
        self._kalshi_stream: KalshiOrderbookStream | None = None

    async def scan_once(self) -> ScanStats:
        """One discovery pass, then a single watchlist book refresh. No WS loop."""
        started = time.perf_counter()
        stats = await self.discover()
        books = await self._refresh_watchlist_books()
        stats.orderbooks_collected = len(books)
        created, expired, opps = self._detect_and_persist()
        stats.opportunities_detected = created
        stats.opportunities_expired = expired
        stats.current_opportunities = opps
        stats.opportunities_open = len(self.repos.open_opportunities())
        stats.duration_seconds = time.perf_counter() - started
        self._persist_snapshots()
        self._write_metrics()
        log_event(
            logger,
            logging.INFO,
            "collection_end",
            markets=stats.markets_by_exchange,
            orderbooks_collected=stats.orderbooks_collected,
            generated_candidates=stats.generated_candidates,
            candidate_matches=stats.candidate_matches,
            high_confidence_matches=stats.high_confidence_matches,
            watchlist_high=stats.watchlist_high,
            watchlist_candidate=stats.watchlist_candidate,
            opportunities_detected=created,
            opportunities_expired=expired,
            collection_duration_seconds=round(stats.duration_seconds, 3),
        )
        return stats

    async def run_forever(self) -> None:
        await self.scan_once()
        tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._snapshot_loop(), name="snapshots"),
            asyncio.create_task(self._adaptive_poll_loop(), name="adaptive-poll"),
            asyncio.create_task(self._detect_loop(), name="detect"),
            asyncio.create_task(self._metrics_loop(), name="metrics"),
        ]
        if "polymarket" in self.exchanges:
            tasks.append(asyncio.create_task(self._run_polymarket_ws(), name="poly-ws"))
        if self._kalshi_credentials():
            tasks.append(asyncio.create_task(self._run_kalshi_ws(), name="kalshi-ws"))
        else:
            log_event(
                logger,
                logging.INFO,
                "kalshi_ws_disabled",
                reason="Official Kalshi WebSockets require API-key handshake; using adaptive HTTP for the watchlist.",
            )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    async def discover(self) -> ScanStats:
        started = time.perf_counter()
        log_event(logger, logging.INFO, "discovery_start", exchanges=list(self.exchanges))
        stats = ScanStats()
        for exchange in self.exchanges.values():
            try:
                markets = await exchange.get_markets()
            except Exception as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "exchange_markets_failed",
                    exchange=exchange.name,
                    error=type(exc).__name__,
                )
                continue
            stats.markets_by_exchange[exchange.name] = len(markets)
            for market in markets:
                classify_market(market)
                market_id = self.repos.upsert_market(market)
                self.state.upsert_market(market, market_id)
            self.repos.commit()

        candidates, candidate_stats = self.candidate_generator.generate(list(self.state.markets.values()))
        scored = self.matcher.classify_pairs(
            [(pair.kalshi_market, pair.polymarket_market) for pair in candidates]
        )
        scored_by_key = {
            (item.market_a_exchange, item.market_a_id, item.market_b_exchange, item.market_b_id): item
            for item in scored
        }
        matches = [
            item
            for item in scored
            if item.match_score >= self.settings.match_candidate_min_score
        ]
        self.state.matches = matches
        stats.generated_candidates = candidate_stats.unique_pairs
        stats.candidate_signals = dict(candidate_stats.by_signal)
        stats.candidate_matches = len(matches)
        stats.high_confidence_matches = sum(
            1
            for match in matches
            if match.match_type in HIGH_CONFIDENCE_TYPES
            and match.match_score >= self.settings.match_high_confidence_min_score
        )
        self.state.match_db_ids = {}
        for match in matches:
            a_id = self.state.db_ids.get((match.market_a_exchange, match.market_a_id))
            b_id = self.state.db_ids.get((match.market_b_exchange, match.market_b_id))
            if a_id is None or b_id is None:
                continue
            row_id = self.repos.upsert_match(a_id, b_id, match)
            self.state.match_db_ids[
                (match.market_a_exchange, match.market_a_id, match.market_b_exchange, match.market_b_id)
            ] = row_id
        self._persist_candidates(candidates, scored_by_key)
        self.repos.commit()

        self.state.watchlist = build_watchlist(
            self.state.markets,
            matches,
            self.state.previous_yes_ask,
            high_confidence_min=self.settings.match_high_confidence_min_score,
            candidate_min=self.settings.match_candidate_min_score,
            max_high=self.settings.max_high_watchlist,
            max_candidate=self.settings.max_candidate_watchlist,
            min_liquidity=self.settings.min_watch_liquidity,
        )
        stats.watchlist_high = len(self.state.watchlist.high())
        stats.watchlist_candidate = len(self.state.watchlist.candidate())
        self.metrics.set_discovered(len(self.state.markets))
        self.metrics.set_watchlist(stats.watchlist_high, stats.watchlist_candidate)
        self.state.last_discovery_at = datetime.now(timezone.utc)
        log_event(
            logger,
            logging.INFO,
            "discovery_end",
            duration_seconds=round(time.perf_counter() - started, 3),
            markets=stats.markets_by_exchange,
            generated_candidates=stats.generated_candidates,
            candidate_signals=stats.candidate_signals,
            candidate_matches=stats.candidate_matches,
            high_confidence_matches=stats.high_confidence_matches,
            watchlist_high=stats.watchlist_high,
            watchlist_candidate=stats.watchlist_candidate,
        )
        return stats

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.discovery_interval_seconds)
            try:
                await self.discover()
            except Exception:
                logger.exception("discovery_loop_failed")

    async def _adaptive_poll_loop(self) -> None:
        """HTTP fallback for markets not covered by a live WebSocket."""
        while True:
            now = time.monotonic()
            due_by_exchange: dict[str, list[str]] = {}
            for item in self.state.watchlist.items:
                if self._has_live_stream(item.exchange, item.tier):
                    continue
                if item.next_poll_at > now:
                    continue
                due_by_exchange.setdefault(item.exchange, []).append(item.market_id)
                item.next_poll_at = now + poll_interval_for_tier(item.tier, self.settings)
            for exchange_name, market_ids in due_by_exchange.items():
                await self._fetch_books(exchange_name, market_ids)
            await asyncio.sleep(0.25)

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.orderbook_snapshot_interval_seconds)
            try:
                self._persist_snapshots()
            except Exception:
                logger.exception("snapshot_loop_failed")

    async def _detect_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.orderbook_snapshot_interval_seconds)
            try:
                self._detect_and_persist()
            except Exception:
                logger.exception("detect_loop_failed")

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            self._write_metrics()
            snapshot = self.metrics.snapshot()
            log_event(
                logger,
                logging.INFO,
                "collector_metrics",
                requests_per_minute={
                    name: row.get("requests_per_minute")
                    for name, row in (snapshot.get("exchanges") or {}).items()
                },
                http_429={
                    name: row.get("http_429") for name, row in (snapshot.get("exchanges") or {}).items()
                },
                markets_monitored=snapshot.get("markets_monitored"),
                websocket_connections=snapshot.get("websocket_connections"),
            )

    async def _run_polymarket_ws(self) -> None:
        async def on_book(book: OrderBook) -> None:
            self.state.update_book(book)

        stream = PolymarketMarketStream(
            on_book=on_book,
            metrics=self.metrics,
            tokens_provider=self._polymarket_high_tokens,
        )
        self._poly_stream = stream
        await stream.run()

    async def _run_kalshi_ws(self) -> None:
        creds = self._kalshi_credentials()
        if not creds:
            return
        api_key, pem = creds

        async def on_book(book: OrderBook) -> None:
            self.state.update_book(book)

        def tickers() -> list[str]:
            return [item.market_id for item in self.state.watchlist.high() if item.exchange == "kalshi"]

        stream = KalshiOrderbookStream(tickers(), on_book, api_key, pem, metrics=self.metrics)
        stream.tickers_provider = tickers
        self._kalshi_stream = stream
        await stream.run()

    def _has_live_stream(self, exchange: str, tier: str) -> bool:
        if tier != TIER_HIGH:
            return False
        if exchange == "polymarket" and self._poly_stream is not None and self._poly_stream.connected:
            return True
        if exchange == "kalshi" and self._kalshi_stream is not None and self._kalshi_stream.connected:
            return True
        return False

    def _polymarket_high_tokens(self) -> dict[str, tuple[str, str]]:
        mapping: dict[str, tuple[str, str]] = {}
        poly = self.exchanges.get("polymarket")
        index = getattr(poly, "_token_index", {}) if poly else {}
        for item in self.state.watchlist.high():
            if item.exchange != "polymarket":
                continue
            yes_token, no_token = index.get(item.market_id, (None, None))
            if yes_token:
                mapping[yes_token] = (item.market_id, "yes")
            if no_token:
                mapping[no_token] = (item.market_id, "no")
        return mapping

    def _kalshi_credentials(self) -> tuple[str, str] | None:
        key = self.settings.kalshi_api_key
        path = self.settings.kalshi_private_key_path
        if not key or not path:
            return None
        try:
            return key, load_private_key_pem(path)
        except OSError as exc:
            log_event(logger, logging.WARNING, "kalshi_private_key_unreadable", error=type(exc).__name__)
            return None

    async def _refresh_watchlist_books(self) -> dict[tuple[str, str], OrderBook]:
        collected: dict[tuple[str, str], OrderBook] = {}
        by_exchange: dict[str, list[str]] = {}
        for item in self.state.watchlist.items:
            by_exchange.setdefault(item.exchange, []).append(item.market_id)
        for name, market_ids in by_exchange.items():
            books = await self._fetch_books(name, market_ids)
            collected.update(books)
        return collected

    async def _fetch_books(self, exchange_name: str, market_ids: list[str]) -> dict[tuple[str, str], OrderBook]:
        exchange = self.exchanges.get(exchange_name)
        if exchange is None or not market_ids:
            return {}
        unique = list(dict.fromkeys(market_ids))
        try:
            books = await exchange.get_orderbooks(unique)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "orderbooks_failed",
                exchange=exchange_name,
                error=type(exc).__name__,
                requested=len(unique),
            )
            return {}
        collected: dict[tuple[str, str], OrderBook] = {}
        for native_id, book in books.items():
            self.state.update_book(book)
            collected[(exchange_name, native_id)] = book
        return collected

    def _persist_snapshots(self) -> None:
        """Write current in-memory books, not every WebSocket tick."""
        keys = self.state.take_dirty()
        if not keys:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        for key in keys:
            db_id = self.state.db_ids.get(key)
            market = self.state.markets.get(key)
            book = self.state.books.get(key)
            if db_id is None or market is None:
                continue
            self.repos.insert_snapshot(db_id, market, timestamp=timestamp)
            if book is not None:
                self.repos.insert_orderbook(db_id, book)
        self.repos.commit()

    def _detect_and_persist(self) -> tuple[int, int, list[ArbitrageOpportunity]]:
        found: list[tuple[ArbitrageOpportunity, MarketMatch]] = []
        tradable = [
            match
            for match in self.state.matches
            if match.match_type in HIGH_CONFIDENCE_TYPES
            and match.match_score >= self.settings.match_high_confidence_min_score
        ]
        watch_keys = self.state.watchlist.keys()
        for match in tradable:
            key_a = (match.market_a_exchange, match.market_a_id)
            key_b = (match.market_b_exchange, match.market_b_id)
            if key_a not in watch_keys and key_b not in watch_keys:
                continue
            market_a = self.state.markets.get(key_a)
            market_b = self.state.markets.get(key_b)
            if market_a is None or market_b is None:
                continue
            fee_a = self.exchanges[market_a.exchange].fee_model
            fee_b = self.exchanges[market_b.exchange].fee_model
            for opportunity in self.detector.detect(
                market_a,
                market_b,
                match=match,
                book_a=self.state.books.get(key_a),
                book_b=self.state.books.get(key_b),
                fee_a=fee_a,
                fee_b=fee_b,
            ):
                found.append((opportunity, match))
        created, expired = self._persist_opportunities(found)
        return created, expired, [item[0] for item in found]

    def _persist_opportunities(
        self,
        found: list[tuple[ArbitrageOpportunity, MarketMatch]],
    ) -> tuple[int, int]:
        live_keys: set[tuple[int, int, str]] = set()
        created = 0
        for opportunity, match in found:
            a_id = self.state.db_ids.get((opportunity.market_a_exchange, opportunity.market_a_id))
            b_id = self.state.db_ids.get((opportunity.market_b_exchange, opportunity.market_b_id))
            if a_id is None or b_id is None:
                continue
            a_sorted, b_sorted = sorted((a_id, b_id))
            live_keys.add((a_sorted, b_sorted, opportunity.strategy))
            match_id = self.state.match_db_ids.get(
                (match.market_a_exchange, match.market_a_id, match.market_b_exchange, match.market_b_id)
            )
            existing = self.repos.find_open_opportunity(a_id, b_id, opportunity.strategy)
            if existing:
                self.repos.touch_opportunity(int(existing["id"]))
            else:
                self.repos.insert_opportunity(a_id, b_id, opportunity, match_id)
                created += 1
        expired = 0
        for row in self.repos.open_opportunities():
            key = (int(row["market_a_id"]), int(row["market_b_id"]), row["strategy"])
            if key not in live_keys:
                self.repos.expire_opportunity(int(row["id"]), row["detected_at"])
                expired += 1
        self.repos.commit()
        if created:
            log_event(logger, logging.INFO, "opportunities_detected", count=created)
        if expired:
            log_event(logger, logging.INFO, "opportunities_expired", count=expired)
        return created, expired

    def _persist_candidates(
        self,
        candidates: list[CandidatePair],
        scored_by_key: dict[tuple[str, str, str, str], MarketMatch],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows: list[dict] = []
        seen_ids: set[tuple[int, int]] = set()
        for pair in candidates:
            a_id = self.state.db_ids.get(("kalshi", pair.kalshi_market.exchange_market_id))
            b_id = self.state.db_ids.get(("polymarket", pair.polymarket_market.exchange_market_id))
            if a_id is None or b_id is None:
                continue
            low, high = sorted((a_id, b_id))
            if (low, high) in seen_ids:
                continue
            seen_ids.add((low, high))
            judged = scored_by_key.get(
                (
                    pair.kalshi_market.exchange,
                    pair.kalshi_market.exchange_market_id,
                    pair.polymarket_market.exchange,
                    pair.polymarket_market.exchange_market_id,
                )
            )
            rows.append(
                {
                    "market_a_id": low,
                    "market_b_id": high,
                    "candidate_score": pair.candidate_score,
                    "reasons_json": json.dumps(pair.reasons, default=str),
                    "signals_json": json.dumps(list(pair.signals)),
                    "generated_at": now,
                    "matcher_result": judged.match_type if judged else None,
                    "matcher_score": judged.match_score if judged else None,
                    "matcher_reason": judged.reason if judged else None,
                }
            )
        self.repos.replace_candidate_pairs(rows)

    def _write_metrics(self) -> None:
        path = Path(self.settings.metrics_path)
        self.metrics.write_json(path)
