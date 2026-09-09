# Prediction Market Arbitrage Scanner

Local research tool for collecting Kalshi and Polymarket data, proposing
equivalent-market matches, and measuring whether binary YES/NO cross-exchange
arbitrage is actually executable after fees and liquidity.

**V1 does not place trades.** There is no order submission, wallet handling,
or position management.

The point of the project is to distinguish:

```text
PRICE DISCREPANCY
        ↓
POTENTIAL MATCH
        ↓
EXECUTABLE OPPORTUNITY
        ↓
NET PROFITABLE OPPORTUNITY
```

Displayed prices that look inconsistent are not an arbitrage. Last-traded
prices are never used for profit calculations.

## Requirements

- Python 3.12+
- SQLite (bundled with Python)
- Network access to the public Kalshi and Polymarket market-data APIs

No Docker, PostgreSQL, or API keys are required for V1 market data.

## Setup

```bash
cd prediction-arb
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_PATH` | `data/prediction_markets.db` | SQLite file (relative to project root) |
| `DISCOVERY_INTERVAL_SECONDS` | `300` | How often to refresh market metadata + matches |
| `POLL_INTERVAL_SECONDS` | `2` | High-interest HTTP poll when WebSockets are down |
| `POLL_CANDIDATE_SECONDS` | `15` | Candidate-match HTTP poll |
| `POLL_INACTIVE_SECONDS` | `300` | Unwatched markets are not polled; discovery covers them |
| `ORDERBOOK_SNAPSHOT_INTERVAL_SECONDS` | `2` | How often in-memory books are written to SQLite |
| `HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `HTTP_MAX_RETRIES` | `5` | Retries on 429 / 5xx / transport errors |
| `ENABLED_EXCHANGES` | `kalshi,polymarket` | Comma-separated adapters |
| `KALSHI_BASE_URL` | `https://external-api.kalshi.com/trade-api/v2` | Official Trade API |
| `KALSHI_API_KEY` | unset | Optional; required only for Kalshi WebSockets |
| `KALSHI_PRIVATE_KEY_PATH` | unset | PEM path for Kalshi WS handshake |
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` | Market discovery |
| `POLYMARKET_CLOB_URL` | `https://clob.polymarket.com` | Executable order books |
| `MAX_MARKETS_PER_EXCHANGE` | `0` (unlimited) | Cap discovery size for local testing |
| `MAX_HIGH_WATCHLIST` | `80` | High-frequency monitored markets |
| `MAX_CANDIDATE_WATCHLIST` | `120` | Moderately polled matched markets |
| `MIN_WATCH_LIQUIDITY` | `10` | Contracts needed to treat a match as high-interest |
| `MATCH_CANDIDATE_MIN_SCORE` | `0.55` | Store candidate matches |
| `MATCH_HIGH_CONFIDENCE_MIN_SCORE` | `0.82` | Run the detector / high watchlist |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `LOG_JSON` | `false` | Structured JSON logs |

## Database

SQLite with WAL mode and foreign keys enabled.

Default location:

```text
data/prediction_markets.db
```

Initialize (also happens automatically on `scan` / `run`):

```bash
python -m prediction_arb.cli init
```

Prices, capital, and P&amp;L are stored as **integer cents** (`47` = $0.47).
Quantities are whole contracts, rounded down. Historical `market_snapshots`
and `orderbook_snapshots` are append-only.

## Commands

```bash
python -m prediction_arb.cli init            # create schema
python -m prediction_arb.cli scan            # one discovery + watchlist book refresh
python -m prediction_arb.cli markets         # active markets currently in the DB
python -m prediction_arb.cli opportunities   # currently open net-profitable opps
python -m prediction_arb.cli history         # lifetime / edge / capital stats
python -m prediction_arb.cli stats           # API / watchlist / WebSocket metrics
python -m prediction_arb.cli run             # discover slowly; stream/poll the watchlist
```

`run` does **not** poll every active market every two seconds. Discovery is
infrequent. Only the watchlist is monitored at high frequency.

## Collection architecture

The goal is useful information per API request, not maximum request volume.

1. **Market discovery (HTTP, infrequent)** — active markets, metadata, resolution text.
2. **Candidate matching** — propose cross-exchange equivalents. Unrelated markets are ignored.
3. **Watchlist** — high-confidence matches with liquidity / activity / a possible edge.
4. **High-frequency monitoring** — official WebSockets when available; otherwise adaptive HTTP.
5. **Persistence** — current books live in memory; SQLite snapshots are periodic.

Priority for the watchlist:

```text
0.40 match confidence
0.20 liquidity
0.15 recent volume/activity
0.15 closeness to an executable YES+NO edge
0.10 recent price movement
```

| Tier | How it is monitored |
| --- | --- |
| High | Polymarket public market WebSocket; Kalshi WS if API keys are set; else HTTP every `POLL_INTERVAL_SECONDS` |
| Candidate | Batched HTTP every `POLL_CANDIDATE_SECONDS` |
| Inactive / unmatched | Discovery only |

**Polymarket** market stream is public: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
([docs](https://docs.polymarket.com/market-data/websocket/market-channel)). Heartbeat `PING` every 10s.

**Kalshi** WebSockets require an API-key handshake
([docs](https://docs.kalshi.com/getting_started/quick_start_websockets)). Without keys the
Kalshi watchlist uses batched `GET /markets/orderbooks` (up to 100 tickers per
documented request). REST market data stays unauthenticated.

Rate limits are configured from official docs only:

- Kalshi Basic read bucket: 200 tokens/sec, default cost 10, 2-second burst capacity
  ([rate limits](https://docs.kalshi.com/getting_started/rate_limits)).
- Polymarket Cloudflare windows, including `/markets` 300/10s, `/book` 1,500/10s,
  `/books` 500/10s ([rate limits](https://docs.polymarket.com/api-reference/rate-limits)).

HTTP 429s honor `Retry-After` when present, apply exponential backoff, and cool
the limiter so retries cannot storm.

## Collectors

- **Kalshi** — `GET /markets?status=open&mve_filter=exclude` (cursor pagination,
  limit 1000; multivariate combo contracts are skipped), event metadata from
  `GET /events/{ticker}` for collected markets only. Books for the watchlist
  come from batched `GET /markets/orderbooks` or the authenticated
  `orderbook_delta` WebSocket. Kalshi books are bids-only; YES/NO asks are
  complements (`ask_yes = 100¢ − bid_no`).
- **Polymarket** — Gamma `GET /markets/keyset?closed=false` for discovery.
  Watchlist books come from the public CLOB market WebSocket, with batched
  `POST /books` as HTTP fallback. Gamma `outcomePrices` are **not** executable.

One failed market/order-book request is logged and skipped. API keys and
signatures are never logged.

## How arbitrage is calculated

Only **binary cross-market YES/NO** on high-confidence matches:

1. Take the executable YES ask on market A and NO ask on market B (and the reverse).
2. Ignore the opportunity unless `YES ask + NO ask < 100¢`.
3. `max_quantity = min(YES ask size, NO ask size)` at the top of book.
4. `capital = qty × (yes_ask + no_ask)` (cents)
5. `payout = qty × 100`
6. `gross_profit = payout − capital`
7. Subtract estimated taker fees for both legs.
8. Store only if `net_profit > 0`.

V1 does **not** blend deeper book levels. That is intentional until the basic
detector is proven.

## Market matching assumptions

Matches are **candidates**, never auto-verified (`verified = 0`).

- Cross-exchange only.
- Candidate filter: shared significant tokens.
- Score: token Jaccard + title sequence ratio + token-set ratio, with
  bonuses for aligned dates/geography/thresholds.
- **Structured contradictions always win.** Different years, months, numeric
  thresholds, geography, or settlement phrases cap the score at 0.49 and
  prevent `EXACT` / `LIKELY_EQUIVALENT`.
- The detector runs only on `EXACT` or `LIKELY_EQUIVALENT` at or above
  `MATCH_HIGH_CONFIDENCE_MIN_SCORE`.

False positives are treated as worse than false negatives.

## Fee assumptions (estimated)

Fees are exchange-specific. Because series/category multipliers can differ
from the published default, every opportunity marks fees as **estimated**.

**Kalshi** (taker, general schedule dated 7 July 2026):

```text
fee ≈ round_up(0.07 × C × P × (1 − P))
```

Series multiplier assumed `1`. Maker fees are not used because lifting an
ask is a taker. Source: [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf).

**Polymarket** (taker):

```text
fee = C × feeRate × p × (1 − p)
```

Category rates from [Polymarket fee docs](https://docs.polymarket.com/trading/fees)
(crypto 0.07, sports/economics/culture/weather/other 0.05, politics/finance/tech/mentions 0.04,
geopolitics 0). Unknown category → 0.05. Makers pay 0; this scanner assumes taker execution.

## API documentation used

- Kalshi market data: https://docs.kalshi.com/getting_started/quick_start_market_data
- Kalshi markets: https://docs.kalshi.com/api-reference/market/get-markets
- Kalshi order books: https://docs.kalshi.com/getting_started/orderbook_responses
- Kalshi pagination: https://docs.kalshi.com/getting_started/pagination
- Kalshi rate limits: https://docs.kalshi.com/getting_started/rate_limits
- Kalshi WebSockets: https://docs.kalshi.com/getting_started/quick_start_websockets
- Polymarket overview: https://docs.polymarket.com/api-reference/predictions/overview
- Polymarket discovery: https://docs.polymarket.com/market-data/discover-markets
- Polymarket books: https://docs.polymarket.com/market-data/prices-order-books
- Polymarket market WebSocket: https://docs.polymarket.com/market-data/websocket/market-channel
- Polymarket fees: https://docs.polymarket.com/trading/fees
- Polymarket rate limits: https://docs.polymarket.com/api-reference/rate-limits

## Tests

```bash
pytest
```

## Current limitations

- No live trading, and none will be added until history shows opportunities
  last long enough to matter.
- Binary YES/NO only. Multi-outcome / scalar / combinatorial markets are stored
  but not arb'd.
- Top-of-book liquidity only (no multi-level blended fills yet).
- Matching is lexical/structured, not an LLM. Ambiguous events stay `POTENTIAL`.
- Kalshi/Polymarket fee multipliers by series/category are approximate.
- Kalshi WebSockets are used only when `KALSHI_API_KEY` + private key are set;
  otherwise the Kalshi watchlist is polled with batched HTTP.
- Unauthenticated Kalshi REST has no separately published limit table; the
  limiter uses the documented Basic read bucket and still honors 429s.
- Snapshot interval is not the same as WebSocket tick rate. Books update in
  memory immediately; SQLite is written on `ORDERBOOK_SNAPSHOT_INTERVAL_SECONDS`.
