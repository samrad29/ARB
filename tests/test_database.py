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
    connection.close()
