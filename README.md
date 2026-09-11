Pull NFL and college football moneyline markets from Kalshi and Polymarket, match the same games, and store a SQLite snapshot.

```
python main.py
```

`nfl/` and `cfp_moneyline/` hold sport-specific team names and API pulls. `main.py` matches games, scores arbs, and writes `moneyline.db`.

Tables: `markets`, `matches`, `arbs`. Each has `sport` (`nfl` or `cfb`) and `level` (`pro` or `college`). Each run replaces the snapshot.

```sql
SELECT * FROM arbs WHERE is_arb = 1 ORDER BY best_edge DESC;
SELECT * FROM matches WHERE level = 'college';
SELECT * FROM markets WHERE sport = 'nfl';
```
