# NFL Unified Odds Monitor

NFL twin of the MLB Unified Odds Monitor v007 pattern. Fetches moneyline,
spread, and totals odds from TheOddsAPI (9-key rotation) and posts movement
charts to three Discord channels (#nfl-moneylines, #nfl-spreads, #nfl-totals).

- Loop: 4 hours (RUN_INTERVAL = 14400)
- Single API call per game covers all three markets (h2h, spreads, totals)
- SQLite history + stored averages for movement charts
- `--once` mode for scheduled runs (GitHub Actions)

## Running

```bash
pip install -r requirements.txt
# set ODDS_API_KEY_1..9 + DISCORD_WEBHOOK_NFL_MONEYLINE/SPREADS/TOTALS
python nfl_unified_odds_monitor.py            # continuous loop
python nfl_unified_odds_monitor.py --once     # single iteration
```

## Deployment: GitHub Actions

`.github/workflows/nfl-odds-monitor.yml` runs the monitor on a 4-hour cron.
Secrets live in the repo (Settings -> Secrets and variables -> Actions):
`ODDS_API_KEY_1..9`, `DISCORD_WEBHOOK_NFL_MONEYLINE`, `DISCORD_WEBHOOK_NFL_SPREADS`,
`DISCORD_WEBHOOK_NFL_TOTALS`.

Manual trigger: Actions -> NFL Odds Monitor -> Run workflow.

## Notes

- Chart PNGs land in `data/runtime/nfl/charts/`; SQLite DB in `data/runtime/nfl/`.
- TheOddsAPI: 9 keys x 500 credits/month = 4,500 credits/month.