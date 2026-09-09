from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import ArbitrageOpportunity, MarketMatch
from prediction_arb.models.orderbook import OrderBook


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class MarketRepository(Protocol):
    def upsert_market(self, market: Market) -> int: ...
    def get_by_exchange_id(self, exchange: str, exchange_market_id: str) -> dict[str, Any] | None: ...
    def list_active(self, exchange: str | None = None) -> list[dict[str, Any]]: ...
    def count_active_by_exchange(self) -> dict[str, int]: ...
    def insert_snapshot(self, market_id: int, market: Market, timestamp: str | None = None) -> int: ...
    def insert_orderbook(self, market_id: int, book: OrderBook) -> None: ...


class SqliteRepositories:
    """SQLite implementation. Keep this as the only SQL-aware layer."""

    def __init__(self, connection) -> None:
        self.conn = connection

    def upsert_market(self, market: Market) -> int:
        now = _utcnow()
        existing = self.get_by_exchange_id(market.exchange, market.exchange_market_id)
        raw = json.dumps(market.raw_data, default=str)
        if existing:
            self.conn.execute(
                """
                UPDATE markets SET
                    ticker=?, title=?, description=?, category=?, status=?,
                    open_time=?, close_time=?, resolution_time=?,
                    resolution_source=?, raw_data=?, updated_at=?
                WHERE id=?
                """,
                (
                    market.ticker,
                    market.title,
                    market.description,
                    market.category,
                    market.status,
                    _dt(market.open_time),
                    _dt(market.close_time),
                    _dt(market.resolution_time),
                    market.resolution_source,
                    raw,
                    now,
                    existing["id"],
                ),
            )
            return int(existing["id"])
        cursor = self.conn.execute(
            """
            INSERT INTO markets (
                exchange, exchange_market_id, ticker, title, description,
                category, status, open_time, close_time, resolution_time,
                resolution_source, raw_data, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.exchange,
                market.exchange_market_id,
                market.ticker,
                market.title,
                market.description,
                market.category,
                market.status,
                _dt(market.open_time),
                _dt(market.close_time),
                _dt(market.resolution_time),
                market.resolution_source,
                raw,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def get_by_exchange_id(self, exchange: str, exchange_market_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM markets WHERE exchange=? AND exchange_market_id=?",
            (exchange, exchange_market_id),
        ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, market_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM markets WHERE id=?", (market_id,)).fetchone()
        return dict(row) if row else None

    def list_active(self, exchange: str | None = None) -> list[dict[str, Any]]:
        if exchange:
            rows = self.conn.execute(
                """
                SELECT * FROM markets
                WHERE exchange=? AND lower(status) IN ('open', 'active')
                ORDER BY title
                """,
                (exchange,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM markets
                WHERE lower(status) IN ('open', 'active')
                ORDER BY exchange, title
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def count_active_by_exchange(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT exchange, COUNT(*) AS n
            FROM markets
            WHERE lower(status) IN ('open', 'active')
            GROUP BY exchange
            """
        ).fetchall()
        return {row["exchange"]: int(row["n"]) for row in rows}

    def insert_snapshot(self, market_id: int, market: Market, timestamp: str | None = None) -> int:
        ts = timestamp or _utcnow()
        cursor = self.conn.execute(
            """
            INSERT INTO market_snapshots (
                market_id, timestamp, yes_bid, yes_ask, no_bid, no_ask,
                yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,
                last_price, volume, open_interest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                ts,
                market.yes_bid,
                market.yes_ask,
                market.no_bid,
                market.no_ask,
                market.yes_bid_size,
                market.yes_ask_size,
                market.no_bid_size,
                market.no_ask_size,
                market.last_price,
                market.volume,
                market.open_interest,
            ),
        )
        return int(cursor.lastrowid)

    def insert_orderbook(self, market_id: int, book: OrderBook) -> None:
        ts = _dt(book.timestamp) or _utcnow()
        rows = [
            (
                market_id,
                ts,
                f"{level.side}_{level.book_side}",
                level.price,
                level.quantity,
                level.level,
            )
            for level in book.levels
        ]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO orderbook_snapshots (
                market_id, timestamp, side, price, quantity, level
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def upsert_match(
        self,
        market_a_id: int,
        market_b_id: int,
        match: MarketMatch,
        canonical_event_id: int | None = None,
    ) -> int:
        now = _utcnow()
        a_id, b_id = sorted((market_a_id, market_b_id))
        existing = self.conn.execute(
            "SELECT id FROM market_matches WHERE market_a_id=? AND market_b_id=?",
            (a_id, b_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                """
                UPDATE market_matches SET
                    match_type=?, match_score=?, verified=?, reason=?,
                    canonical_event_id=?, updated_at=?
                WHERE id=?
                """,
                (
                    match.match_type,
                    match.match_score,
                    1 if match.verified else 0,
                    match.reason,
                    canonical_event_id,
                    now,
                    existing["id"],
                ),
            )
            return int(existing["id"])
        cursor = self.conn.execute(
            """
            INSERT INTO market_matches (
                market_a_id, market_b_id, match_type, match_score, verified,
                reason, canonical_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a_id,
                b_id,
                match.match_type,
                match.match_score,
                1 if match.verified else 0,
                match.reason,
                canonical_event_id,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def list_matches(self, min_score: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM market_matches"
        params: tuple[Any, ...] = ()
        if min_score is not None:
            sql += " WHERE match_score >= ?"
            params = (min_score,)
        sql += " ORDER BY match_score DESC"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def high_confidence_matches(self, min_score: float) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM market_matches
            WHERE match_type IN ('EXACT', 'LIKELY_EQUIVALENT')
              AND match_score >= ?
              AND verified = 0
            ORDER BY match_score DESC
            """,
            (min_score,),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_canonical_event(
        self,
        question: str,
        category: str | None,
        event_date: str | None,
        resolution_date: str | None,
        resolution_source: str | None,
    ) -> int:
        now = _utcnow()
        cursor = self.conn.execute(
            """
            INSERT INTO canonical_events (
                canonical_question, category, event_date, resolution_date,
                resolution_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (question, category, event_date, resolution_date, resolution_source, now, now),
        )
        return int(cursor.lastrowid)

    def open_opportunities(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT o.*,
                   ma.exchange AS market_a_exchange,
                   ma.exchange_market_id AS market_a_exchange_id,
                   ma.title AS market_a_title,
                   mb.exchange AS market_b_exchange,
                   mb.exchange_market_id AS market_b_exchange_id,
                   mb.title AS market_b_title
            FROM arbitrage_opportunities o
            JOIN markets ma ON ma.id = o.market_a_id
            JOIN markets mb ON mb.id = o.market_b_id
            WHERE o.status = 'OPEN'
            ORDER BY o.net_profit DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def all_opportunities(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT o.*,
                   ma.exchange AS market_a_exchange,
                   ma.title AS market_a_title,
                   ma.category AS market_a_category,
                   mb.exchange AS market_b_exchange,
                   mb.title AS market_b_title
            FROM arbitrage_opportunities o
            JOIN markets ma ON ma.id = o.market_a_id
            JOIN markets mb ON mb.id = o.market_b_id
            ORDER BY o.detected_at
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def find_open_opportunity(
        self, market_a_id: int, market_b_id: int, strategy: str
    ) -> dict[str, Any] | None:
        a_id, b_id = sorted((market_a_id, market_b_id))
        row = self.conn.execute(
            """
            SELECT * FROM arbitrage_opportunities
            WHERE market_a_id=? AND market_b_id=? AND strategy=? AND status='OPEN'
            """,
            (a_id, b_id, strategy),
        ).fetchone()
        return dict(row) if row else None

    def insert_opportunity(
        self,
        market_a_id: int,
        market_b_id: int,
        opportunity: ArbitrageOpportunity,
        match_id: int | None,
    ) -> int:
        now = _utcnow()
        a_id, b_id = sorted((market_a_id, market_b_id))
        cursor = self.conn.execute(
            """
            INSERT INTO arbitrage_opportunities (
                detected_at, type, market_a_id, market_b_id, strategy,
                capital_required, guaranteed_payout, gross_profit,
                estimated_fees, net_profit, roi, max_quantity, status,
                expired_at, duration_seconds, last_seen_at, match_id,
                yes_ask, no_ask, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                now,
                opportunity.type,
                a_id,
                b_id,
                opportunity.strategy,
                opportunity.capital_required,
                opportunity.guaranteed_payout,
                opportunity.gross_profit,
                opportunity.estimated_fees,
                opportunity.net_profit,
                opportunity.roi_bps,
                opportunity.max_quantity,
                now,
                match_id,
                opportunity.yes_ask,
                opportunity.no_ask,
                json.dumps(opportunity.details, default=str),
            ),
        )
        return int(cursor.lastrowid)

    def touch_opportunity(self, opportunity_id: int) -> None:
        self.conn.execute(
            "UPDATE arbitrage_opportunities SET last_seen_at=? WHERE id=?",
            (_utcnow(), opportunity_id),
        )

    def expire_opportunity(self, opportunity_id: int, detected_at: str) -> None:
        now = datetime.now(timezone.utc)
        expired_at = now.isoformat()
        try:
            start = datetime.fromisoformat(detected_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            duration = (now - start).total_seconds()
        except ValueError:
            duration = None
        self.conn.execute(
            """
            UPDATE arbitrage_opportunities
            SET status='EXPIRED', expired_at=?, duration_seconds=?
            WHERE id=? AND status='OPEN'
            """,
            (expired_at, duration, opportunity_id),
        )

    def commit(self) -> None:
        self.conn.commit()
