PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS markets (
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
    series_ticker TEXT,
    series_title TEXT,
    event_ticker TEXT,
    event_title TEXT,
    subcategory TEXT,
    tags_json TEXT,
    topics_json TEXT,
    entities_json TEXT,
    settlement_sources_json TEXT,
    liquidity INTEGER,
    classified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (exchange, exchange_market_id)
);

CREATE INDEX IF NOT EXISTS idx_markets_series
    ON markets (exchange, series_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_event
    ON markets (exchange, event_ticker);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    yes_bid INTEGER,
    yes_ask INTEGER,
    no_bid INTEGER,
    no_ask INTEGER,
    yes_bid_size INTEGER,
    yes_ask_size INTEGER,
    no_bid_size INTEGER,
    no_ask_size INTEGER,
    last_price INTEGER,
    volume INTEGER,
    open_interest INTEGER,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_ts
    ON market_snapshots (market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_ts
    ON market_snapshots (timestamp);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    side TEXT NOT NULL,
    price INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    level INTEGER NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id)
);

CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_market_ts
    ON orderbook_snapshots (market_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_market_ts_side
    ON orderbook_snapshots (market_id, timestamp, side);

CREATE TABLE IF NOT EXISTS canonical_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_question TEXT NOT NULL,
    category TEXT,
    event_date TEXT,
    resolution_date TEXT,
    resolution_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_a_id INTEGER NOT NULL,
    market_b_id INTEGER NOT NULL,
    match_type TEXT NOT NULL,
    match_score REAL NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    canonical_event_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (market_a_id) REFERENCES markets(id),
    FOREIGN KEY (market_b_id) REFERENCES markets(id),
    FOREIGN KEY (canonical_event_id) REFERENCES canonical_events(id),
    UNIQUE (market_a_id, market_b_id)
);

CREATE INDEX IF NOT EXISTS idx_market_matches_type
    ON market_matches (match_type, match_score);

CREATE TABLE IF NOT EXISTS candidate_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_a_id INTEGER NOT NULL,
    market_b_id INTEGER NOT NULL,
    candidate_score REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    signals_json TEXT,
    generated_at TEXT NOT NULL,
    matcher_result TEXT,
    matcher_score REAL,
    matcher_reason TEXT,
    FOREIGN KEY (market_a_id) REFERENCES markets(id),
    FOREIGN KEY (market_b_id) REFERENCES markets(id),
    UNIQUE (market_a_id, market_b_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_pairs_score
    ON candidate_pairs (candidate_score DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_pairs_matcher
    ON candidate_pairs (matcher_result, matcher_score);

CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    type TEXT NOT NULL,
    market_a_id INTEGER NOT NULL,
    market_b_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    capital_required INTEGER NOT NULL,
    guaranteed_payout INTEGER NOT NULL,
    gross_profit INTEGER NOT NULL,
    estimated_fees INTEGER NOT NULL,
    net_profit INTEGER NOT NULL,
    roi INTEGER NOT NULL,
    max_quantity INTEGER NOT NULL,
    status TEXT NOT NULL,
    expired_at TEXT,
    duration_seconds REAL,
    last_seen_at TEXT,
    match_id INTEGER,
    yes_ask INTEGER,
    no_ask INTEGER,
    details_json TEXT,
    FOREIGN KEY (market_a_id) REFERENCES markets(id),
    FOREIGN KEY (market_b_id) REFERENCES markets(id),
    FOREIGN KEY (match_id) REFERENCES market_matches(id)
);

CREATE INDEX IF NOT EXISTS idx_arb_status
    ON arbitrage_opportunities (status, detected_at);
CREATE INDEX IF NOT EXISTS idx_arb_pair_strategy
    ON arbitrage_opportunities (market_a_id, market_b_id, strategy, status);
