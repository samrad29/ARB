from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_MARKET_COLUMNS = (
    ("series_ticker", "TEXT"),
    ("series_title", "TEXT"),
    ("event_ticker", "TEXT"),
    ("event_title", "TEXT"),
    ("subcategory", "TEXT"),
    ("tags_json", "TEXT"),
    ("topics_json", "TEXT"),
    ("entities_json", "TEXT"),
    ("settlement_sources_json", "TEXT"),
    ("liquidity", "INTEGER"),
    ("classified_at", "TEXT"),
)


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")
    connection.execute("PRAGMA busy_timeout=5000;")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "markets" in tables:
        _migrate_markets(connection)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection.executescript(schema)
    _migrate_markets(connection)
    connection.commit()


def _migrate_markets(connection: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in connection.execute("PRAGMA table_info(markets)").fetchall()
    }
    for name, ddl in _MARKET_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE markets ADD COLUMN {name} {ddl}")


def journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode;").fetchone()
    return str(row[0]).upper()
