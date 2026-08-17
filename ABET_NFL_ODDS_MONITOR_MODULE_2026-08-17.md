# ABET NFL ODDS MONITOR - MODULE DOCUMENTATION

**Module:** NFL Unified Odds Monitor v001
**Doc generated:** 2026-08-17 05:30 UTC (local 2026-08-16 ~22:30 EDT)
**Status:** LIVE on GitHub Actions; parked HF Space fallback exists
**Owner accounts:** GitHub `abet-hq` · HF `abetco` (fallback) · email `abethq@proton.me`

> This is the exhaustive record for this module - files, config, secrets
> locations, deployment topology, proof runs, and known constraints. If a
> question about this module isn't answerable here, check
> `D:\abet_swarm\nflverse-preseason-proof\PROGRESS.md` (build log) or the
> source-of-truth secret docs listed in section 6.

---

## 1. What this module is

NFL twin of the **MLB Unified Odds Monitor v007** pattern. Fetches moneyline,
spread, and totals odds for all NFL games from TheOddsAPI in ONE API call per
sport key (`h2h,spreads,totals`), stores history in SQLite, generates
matplotlib movement charts (dark theme), and posts them to three Discord
channels. Runs on a **4-hour loop** (continuous mode) or a single iteration
(`--once`, used by the GitHub Actions scheduler).

- **Script version:** `001` (SCRIPT_VERSION in header)
- **Lineage:** replicates MLB v007; shares the SAME 9-key TheOddsAPI pool
  (ODDS_API_KEY_1..9) with the MLB suite - see sections 6 and 10.
- **Interval:** `RUN_INTERVAL = 14400` seconds (4 h). Was 1800 s (30 min)
  until 2026-08-16/17; changed per user request.

## 2. Build timeline (datetime-stamped)

| When (UTC) | Event |
|---|---|
| 2026-08-14 ~02:30 | First HF deploy attempt: bundle built in `hf_space/` (Dockerfile, nfl_launcher.py, README, requirements). |
| 2026-08-14 | HF `create_repo` blocked: **402 Payment Required** - docker Spaces need PRO. |
| 2026-08-14 | Repurposed `abetco/bovada-depth-proof-static` (static->docker flip via README frontmatter), pushed bundle, deleted stale assets, set 3 webhook secrets, flipped private. |
| 2026-08-14 | Space renamed -> **`abetco/nfl-monitors`** (move_repo = rename, no payment wall). |
| 2026-08-14 | Found the 9 odds keys documented locally in `ABET_MLB_MONITOR_SYSTEM_2026-03-07.md` section 10 (HF secrets are write-only; cannot be read from mlb-monitors). Set all 9 as HF secrets -> 12 total. |
| 2026-08-14 | Boot attempt failed: runtime error **"Quota exceeded for flavor cpu-basic: current=3, limit=0"** - abetco's 3 grandfathered docker slots are full (mlb-monitors RUNNING, live-odds RUNNING, mlb-forecaster RUNTIME_ERROR). Free docker tier no longer exists on HF (PRO required; confirmed on pricing page + fresh account). |
| 2026-08-16 | `abetco/mlb-forecaster` archived locally (full clone + `ABET_MLB_FORECASTER_ARCHIVE_2026-08-16.md`) as a candidate slot; **user decided NOT to repurpose it yet**. |
| 2026-08-16 | New accounts created by user: GitHub **`abet-hq`** (abethq@proton.me) + HF **`abethq`** (same email). Both documented in `E:\abet\.env.account-registry.local` + `LOCAL_ONLY_MASTER_SECRETS.md`. |
| 2026-08-16 | gh CLI login (device flow) for abet-hq; made active account. |
| 2026-08-16 | User: loop must be **4 hours** (not 30 min); expects Discord channel images as proof. `RUN_INTERVAL` changed 1800->14400 in source + hf_space copies. |
| 2026-08-16 ~22:01 | **LOCAL PROOF RUN** (`--once`): 16 games, 96 records, 48 charts - all posted to Discord OK (exit 0, 100.9 s). |
| 2026-08-16 ~22:15 | Charts copied to `nflverse-preseason-proof\proof\` (48 PNGs). |
| 2026-08-17 ~05:10 | Repo **`abet-hq/nfl-monitors`** created (PUBLIC -> free unlimited Actions minutes). Bundle pushed (commit 98991aa). |
| 2026-08-17 ~05:20 | 12 repo secrets set via `gh secret set` (9 keys + 3 webhooks), verified. |
| 2026-08-17 ~05:20 | Workflow scope added to abet-hq token (`gh auth refresh --scopes workflow`, device flow). |
| 2026-08-17 ~05:20 | Workflow `nfl-odds-monitor.yml` pushed; manual run **31997571974** triggered. |
| 2026-08-17 ~05:22 | **GH ACTIONS PROOF RUN**: completed/success in 1m38s - 16 games, 128 records, **48 charts posted from GitHub runner** (log: `OK Posted:` x48). |
| 2026-08-17 05:30 | This document created. |

## 3. Where the module lives (all copies)

| Location | Purpose | Sync |
|---|---|---|
| `D:\abet_swarm\nflverse-preseason-proof\nfl_unified_odds_monitor.py` | SOURCE (authoritative) | - |
| `...\hf_space\` (Dockerfile, nfl_launcher.py, README, requirements, monitor, .env.example, .gitignore) | parked HF Space bundle | copy of source monitor |
| `...\gh_bundle\` (+ `.github\workflows\nfl-odds-monitor.yml`) | GitHub repo bundle | pushes to GitHub |
| **https://github.com/abet-hq/nfl-monitors** (PUBLIC) | LIVE deployment | `git push` main |
| **https://huggingface.co/spaces/abetco/nfl-monitors** (PRIVATE, docker SDK) | PARKED fallback - 12 secrets set, **cannot boot** (cpu-basic quota limit=0) | - |
| `...\proof\` (48 PNGs) | proof charts from local run | - |
| `...\data\runtime\nfl\charts\` (947 PNGs as of 2026-08-17) | all generated charts (local) | - |

## 4. File inventory & roles

| File | Role |
|---|---|
| `nfl_unified_odds_monitor.py` (1517 lines) | The monitor. Env-driven config (section 5), 9-key rotation (section 6), SQLite history, chart generation, Discord posting, `--once` mode, signal handling (SIGINT/SIGTERM -> graceful stop + shutdown message). |
| `hf_space\nfl_launcher.py` | HF-launcher wrapper (only for the parked HF Space): runs monitor as subprocess, auto-restart, status page :7860. |
| `hf_space\Dockerfile` | python:3.11-slim + fonts-liberation; `CMD nfl_launcher.py`. |
| `requirements.txt` | requests>=2.31.0, pandas>=2.0.0, matplotlib>=3.7.0, pytz>=2023.3. |
| `.env.example` (from `.env.nfl.example`) | Documents every env var the monitor reads. |
| `.github\workflows\nfl-odds-monitor.yml` | GH Actions: cron `0 */4 * * *` + `workflow_dispatch`; python 3.11; runs `--once` with all 12 secrets mapped to env. |
| `run_nfl_odds_monitor.ps1` (module root) | Local launcher convenience script. |
| `PROGRESS.md` | Running build log (this module + related work). |
| `ABET_MLB_FORECASTER_ARCHIVE_2026-08-16.md` | Separate module (forecaster) - not this one. |

## 5. Configuration reference (read from env at import)

| Constant | Value / source |
|---|---|
| `BASE_DIR` | `$ABET_BASE_DIR` or script's parent dir |
| `LOG_DIR` | `<BASE>/data/logs` - file log `nfl_odds_monitor.log` (`[ts] LEVEL: msg`) |
| `DB_PATH` | `<BASE>/data/database/odds_nfl.db` (SQLite, history + averages) |
| `RAW_DIR` | `<BASE>/data/runtime/nfl/raw_odds` (raw JSON snapshots per fetch) |
| `RUNTIME_DIR` | `<BASE>/data/runtime/nfl` |
| `CHARTS_DIR` | `<RUNTIME_DIR>/charts` - chart PNGs |
| `API_STATE_FILE` | `<RUNTIME_DIR>/api_state.json` - key rotation state (credits/backoff) |
| Sport keys | Aug-Sep: `americanfootball_nfl_preseason` then `americanfootball_nfl`; otherwise `americanfootball_nfl` only |
| API | `https://api.the-odds-api.com/v4/sports` - `REGIONS=us` - `MARKETS=h2h,spreads,totals` (one call for all 3 markets) |
| Flags | `VERBOSE_LOGGING=True`, `SEND_DISCORD=True`, `GENERATE_CHARTS=True` |
| `RUN_INTERVAL` | **14400 s (4 h)** - continuous mode |
| Teams | TEAM_ABBR map, all 32 teams |

Runtime artifacts (per iteration): 1 fetch per sport key (rotating keys on
401/credit issues), SQLite records for every game x market line, charts named
`<gameid8>_<ml|sp|tot>[_line]_HHMMSS.png` (e.g. `b1293da5_tot_37_5_220147.png`).
## 6. Secrets - names, locations, NEVER duplicated here

12 secrets total: `ODDS_API_KEY_1..9` (TheOddsAPI, 32-hex, shared with MLB
suite) + `DISCORD_WEBHOOK_NFL_MONEYLINE` / `_SPREADS` / `_TOTALS`
(discordapp.com domain - HF-compatible variant of discord.com).

**Where the VALUES live (source of truth):**
1. **GitHub repo secrets** - `abet-hq/nfl-monitors` -> Settings -> Secrets ->
   Actions (LIVE, verified 2026-08-17).
2. **HF Space secrets** - `abetco/nfl-monitors` -> Settings -> Secrets
   (PARKED fallback, set 2026-08-14).
3. **Local** - `...\.env.nfl.local` (gitignored) and
   `E:\abet\.env.account-registry.local` (account registry; Supabase +
   GitHub + HF accounts, email abethq@proton.me).
4. **Odds keys source doc** - `D:\abet_swarm\mlb-monitors-clone\
   ABET_MLB_MONITOR_SYSTEM_2026-03-07.md` section 10 (where keys were
   recovered from; HF secrets are write-only and cannot be read back).
5. **Webhook channel IDs** (public part of URLs, for reference):
   MONEYLINE `1538722358972653588` - SPREADS `1538722660924526612` -
   TOTALS `1538722750993268766`.

## 7. Deployment topology

```
GitHub (abet-hq/nfl-monitors, PUBLIC)          <- LIVE
  `- .github/workflows/nfl-odds-monitor.yml
       cron 0 */4 * * *  +  manual dispatch
       -> ubuntu runner, python 3.11, pip install -r requirements.txt
       -> python nfl_unified_odds_monitor.py --once
       -> env from 12 repo secrets
       -> posts charts -> Discord #nfl-moneylines / #nfl-spreads / #nfl-totals

HF Space abetco/nfl-monitors (PRIVATE, docker) <- PARKED (cannot boot:
  cpu-basic quota limit=0 on abetco; new docker Spaces require PRO)

Local: run_nfl_odds_monitor.ps1 / python nfl_unified_odds_monitor.py
  (uses .env.nfl.local when present)
```

## 8. Proof records

| Run | When (UTC) | Result |
|---|---|---|
| Local `--once` | 2026-08-16 22:01 | 16 games - 96 records - 48 charts - all posted OK - 100.9 s - exit 0 |
| GH Actions #31997571974 | 2026-08-17 05:22 | success 1m38s - 16 games - 128 records - 48 charts posted from runner OK |

Charts on disk: `proof\` (48), `data\runtime\nfl\charts\` (947 total incl.
earlier test runs). Verify Discord delivery via workflow logs:
`gh run view <id> --repo abet-hq/nfl-monitors --log | grep "Posted:"`.

## 9. Operations

- **Manual GH run:** `gh workflow run nfl-odds-monitor.yml --repo abet-hq/nfl-monitors`
- **Watch:** `gh run watch <id> --repo abet-hq/nfl-monitors`
- **Local run:** `python nfl_unified_odds_monitor.py` (loop) / `--once`
- **Change interval:** edit `RUN_INTERVAL` in the SOURCE file, re-copy to
  `hf_space/` and `gh_bundle/`, commit+push (3 copies stay in sync).
- **Change keys/webhooks:** no code change needed - just update the GH repo
  secrets (and/or HF space secrets for the fallback).
- **Redeploy:** `git -C gh_bundle add -A && git -C gh_bundle commit -m ... &&
  git -C gh_bundle push` (credential: gh auth for abet-hq).

## 10. Known constraints & risks

1. **Shared key pool:** NFL monitor uses the SAME 9 keys as the MLB suite -
   credit contention is possible (MLB historically depletes keys; see MLB
   docs "API_6 was depleting"). Budget: 9 x 500 = 4,500 credits/month. NFL
   at 4 h cadence ~ 6 fetches/day ~ 180/month - small, but watch
   `api_state.json` / MLB logs. NFL-dedicated keys can be swapped in with
   no code change.
2. **HF fallback can't boot:** abetco quota limit=0; only PRO ($9/mo) or
   paid hardware (cpu-upgrade ~$30/mo) would revive it. Not pursued.
3. **GH Actions free tier:** public repo = unlimited minutes; keep repo
   public (or accept 2,000 min/mo private limit). Repo contains NO secrets.
4. **Node 20 deprecation:** actions/checkout@v4 + setup-python@v5 emit
   deprecation warnings on runners (harmless; upgrade actions when convenient).
5. **Webhook domains:** monitor reads `discordapp.com` URLs; if a webhook is
   recreated, Discord issues a NEW token - update all 3 secret stores.
6. **HF secret changes do not wake the parked space** - irrelevant until
   quota/PRO situation changes.

## 11. Roadmap (user intent)

- Consolidate ALL sport monitors (MLB, NFL, NBA, NHL, ...) into ONE
  multi-sport space/repo with per-sport config instead of one per sport.
- NFL-dedicated TheOddsAPI keys (swap values into the 12-secret set).
- `abetco/mlb-forecaster` remains untouched/archived; may be fixed or
  repurposed later.
