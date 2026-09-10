from __future__ import annotations

from pathlib import Path

from prediction_arb.database.connection import connect, initialize_database, journal_mode
from prediction_arb.database.repositories import SqliteRepositories
from prediction_arb.models.market import Market


def test_wal_and_snapshot_append(tmp_path: Path) -> None:
    db_path = tmp_path / "prediction_markets.db"
    connection = connect(db_path)
    initialize_database(connection)
    assert journal_mode(connection) == "WAL"

    repos = SqliteRepositories(connection)
    market = Market(
        exchange="kalshi",
        exchange_market_id="ABC",
        ticker="ABC",
        title="Test market",
        status="open",
        yes_ask=47,
        no_ask=54,
        yes_ask_size=10,
        no_ask_size=10,
    )
    market_id = repos.upsert_market(market)
    repos.insert_snapshot(market_id, market)
    market.yes_ask = 48
    repos.upsert_market(market)
    repos.insert_snapshot(market_id, market)
    repos.commit()

    snapshots = connection.execute(
        "SELECT yes_ask FROM market_snapshots WHERE market_id=? ORDER BY id",
        (market_id,),
    ).fetchall()
    assert [row["yes_ask"] for row in snapshots] == [47, 48]

    unique = connection.execute(
        "SELECT COUNT(*) AS n FROM markets WHERE exchange='kalshi' AND exchange_market_id='ABC'"
    ).fetchone()
    assert unique["n"] == 1

    from prediction_arb.matching.taxonomy import classify_market
    from prediction_arb.models.opportunity import MarketMatch

    classify_market(market)
    repos.upsert_market(market)
    poly = Market(
        exchange="polymarket",
        exchange_market_id="XYZ",
        ticker="xyz",
        title="Test poly",
        status="open",
    )
    poly_id = repos.upsert_market(poly)
    match = MarketMatch(
        market_a_exchange="kalshi",
        market_a_id="ABC",
        market_b_exchange="polymarket",
        market_b_id="XYZ",
        match_type="POTENTIAL",
        match_score=0.4,
        reason="test",
    )
    repos.replace_candidate_pairs(
        [
            {
                "market_a_id": market_id,
                "market_b_id": poly_id,
                "candidate_score": 0.61,
                "reasons_json": '["same topic", "Fed"]',
                "signals_json": '["same_topic"]',
                "generated_at": "2026-09-09T00:00:00+00:00",
                "matcher_result": match.match_type,
                "matcher_score": match.match_score,
                "matcher_reason": match.reason,
            }
        ]
    )
    repos.commit()
    stats = repos.candidate_pair_stats()
    assert stats["generated"] == 1
    assert stats["by_signal"]["same_topic"] == 1
    row = connection.execute("SELECT topics_json FROM markets WHERE id=?", (market_id,)).fetchone()
    assert row["topics_json"]
    connection.close()


def test_migrate_adds_metadata_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = connect(db_path)
    connection.executescript(
        """
        CREATE TABLE markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            exchange_market_id TEXT NOT NULL,
            ticker TEXT,
            title TEXT NOT NULL,
            description TEXT,
            category TEXT,
            status TEXT,
            open_time TEXT,
            close_time TEXT,
            resolution_time TEXT,
            resolution_source TEXT,
            raw_data TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (exchange, exchange_market_id)
        );
        """
    )
    connection.commit()
    initialize_database(connection)
    cols = {row[1] for row in connection.execute("PRAGMA table_info(markets)")}
    assert "series_ticker" in cols
    assert "topics_json" in cols
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "candidate_pairs" in tables
    connection.close()
