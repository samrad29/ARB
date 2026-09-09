from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


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
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection.executescript(schema)
    connection.commit()


def journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode;").fetchone()
    return str(row[0]).upper()
