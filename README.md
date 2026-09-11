Pull NFL, college football, and tennis moneyline markets from Kalshi and Polymarket, match the same games, and store a SQLite snapshot.

```
python main.py               # one discovery pass
python -m polling            # poll until Ctrl+C; rediscover about every 10 minutes
python -m polling --minutes 60
python -m polling.analysis   # arb counts, size buckets, and durations from price_ticks
```

`python -m polling` quotes matches in parallel and starts the next pass immediately if quoting already took 8+ seconds (otherwise it waits out the rest of an 8-second cycle). Discovery still reruns about every 10 minutes.

`moneyline.db` tables:

- `markets`, `matches`, `arbs` — current snapshot (`sport` is `nfl`/`cfb`/`tennis`; `level` is `pro`/`college` or tennis `atp`/`wta`/`itf`/`challenger`). Replaced on each discovery. `live` / `ended` come from Polymarket at discovery and stay until the next discovery.
- `price_ticks` — every poll quote, with `observed_at` and the discovery-time `live` / `ended` flags
- `arb_history` — timestamped arb flags from discovery and polling (`source` is `discovery` or `poll`)

```sql
SELECT * FROM arb_history ORDER BY observed_at DESC;
SELECT * FROM price_ticks WHERE is_arb = 1 ORDER BY observed_at DESC;
SELECT * FROM matches WHERE level = 'college';
SELECT * FROM matches WHERE sport = 'tennis' AND level = 'challenger';
```
