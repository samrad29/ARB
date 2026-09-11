Pull NFL and college football moneyline markets from Kalshi and Polymarket, match the same games, and store a SQLite snapshot.

```
python main.py          # one discovery pass
python -m polling       # poll live prices; rediscover about every 10 minutes
```

`markets/nfl/` and `markets/cfp_moneyline/` hold sport-specific team names and API pulls. `polling/` quotes the current matches.

`moneyline.db` tables:

- `markets`, `matches`, `arbs` — current snapshot (`sport` is `nfl`/`cfb`, `level` is `pro`/`college`). Replaced on each discovery.
- `price_ticks` — every poll quote, with `observed_at`
- `arb_history` — timestamped arb flags from discovery and polling (`source` is `discovery` or `poll`)

```sql
SELECT * FROM arb_history ORDER BY observed_at DESC;
SELECT * FROM price_ticks WHERE is_arb = 1 ORDER BY observed_at DESC;
SELECT * FROM matches WHERE level = 'college';
```
