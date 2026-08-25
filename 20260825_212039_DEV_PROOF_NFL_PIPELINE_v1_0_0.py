r"""DEV PROOF — the full NFL pipeline, end to end, with ZERO TheOddsAPI credits.

WHAT THIS PROVES, using only real data:
  P1  Supabase read-back   nfl_odds_history rows actually come back out
  P2  SQLite restore       those rows rebuild a MULTI-POINT per-book series
  P3  Team logos           real ESPN logos composite into the title band
  P4  Chart render         a real chart is drawn from the restored history
  P5  Discord delivery     it posts to the TEST channel, never production
  P6  Idempotency          replaying the same rows does not double-count

WHY IT COSTS NOTHING:
  * odds prices come from Supabase nfl_odds_history — rows this monitor already
    wrote, ~1,000/hour, which nothing has ever read back
  * team logos come from a.espncdn.com — free
  * game metadata, if needed, comes from /events/:sport — documented FREE
  * NOT ONE call to /odds/:sport is made

SAFETY:
  * dispatch-only, on a branch, never scheduled
  * posts ONLY to DISCORD_WEBHOOK_TESTING
  * opens SQLite in a temp dir; production odds_nfl.db is never touched
  * read-only against Supabase — SELECT only, no writes
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev_proof_out")
os.makedirs(OUT, exist_ok=True)
RESULTS = {}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ABETMonitor/1.0)"})

TEAM_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}
BOOKMAKER_COLORS = {
    'draftkings': '#53D337', 'fanduel': '#1A73E8', 'betmgm': '#FDB913',
    'betrivers': '#003DA5', 'caesars': '#522398', 'bovada': '#CC0000',
    'betonlineag': '#FF4500', 'mybookieag': '#800080', 'betus': '#006400',
    'lowvig': '#4B0082',
}
_logo_cache = {}


def hdr(n, t):
    print("\n" + "=" * 78)
    print(f"{n} — {t}")
    print("=" * 78)


def to_decimal(am):
    try:
        a = float(am)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return a / 100.0 + 1.0 if a > 0 else 100.0 / abs(a) + 1.0


def american_fmt(y, _pos):
    if y >= 2.0:
        return f"+{int((y - 1) * 100)}"
    if y > 1.0:
        return f"{int(-100 / (y - 1))}"
    return ""


def get_logo(team, size=36):
    if team in _logo_cache:
        return _logo_cache[team]
    logo = None
    abbr = TEAM_ABBR.get(team)
    if abbr:
        try:
            r = SESSION.get(f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr.lower()}.png",
                            timeout=10)
            if r.status_code == 200 and len(r.content) > 1000:
                im = Image.open(BytesIO(r.content)).convert("RGBA").resize(
                    (size, size), Image.LANCZOS)
                logo = np.array(im)
        except Exception as e:
            print(f"   logo {team}: {e}")
    _logo_cache[team] = logo
    return logo


# ---------------------------------------------------------------- P1
hdr("P1", "SUPABASE READ-BACK (real rows, zero credits)")
SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_KEY", "")
rows = []
if not (SUPA_URL and SUPA_KEY):
    print("   FAIL: SUPABASE_URL / SUPABASE_KEY not present in this environment")
    RESULTS["P1"] = "FAIL - no credentials"
else:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        r = SESSION.get(
            f"{SUPA_URL}/rest/v1/nfl_odds_history",
            params={"fetched_at_pt": f"gte.{since}",
                    "order": "fetched_at_pt.asc", "limit": "20000"},
            headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"},
            timeout=60)
        print(f"   HTTP {r.status_code}")
        if r.status_code == 200:
            rows = r.json()
        else:
            print(f"   body: {r.text[:250]}")
    except Exception as e:
        print(f"   ERROR {type(e).__name__}: {e}")
    print(f"   rows returned since {since[:19]}: {len(rows)}")
    if rows:
        print(f"   columns: {sorted(rows[0].keys())}")
        print(f"   sample : {json.dumps(rows[0])[:200]}")
    RESULTS["P1"] = f"{'PASS' if rows else 'FAIL'} - {len(rows)} rows"

# ---------------------------------------------------------------- P2
hdr("P2", "SQLITE RESTORE -> MULTI-POINT SERIES")
db = os.path.join(tempfile.mkdtemp(), "odds_nfl_devproof.db")
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute("""CREATE TABLE odds_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT, fetch_timestamp TEXT, bookmaker_key TEXT, bookmaker_title TEXT,
    market_key TEXT, outcome_name TEXT, price_decimal REAL,
    price_american INTEGER, point REAL, is_live_game BOOLEAN)""")
c.execute("CREATE UNIQUE INDEX ux ON odds_history"
          "(game_id, fetch_timestamp, bookmaker_key, market_key, outcome_name)")
payload = [(r.get("game_id"), r.get("fetched_at_pt") or r.get("created_at"),
            r.get("bookmaker"), r.get("bookmaker"), r.get("market"),
            r.get("outcome"), to_decimal(r.get("price")), r.get("price"),
            r.get("point"), 0) for r in rows]
c.executemany("INSERT OR IGNORE INTO odds_history (game_id, fetch_timestamp, "
              "bookmaker_key, bookmaker_title, market_key, outcome_name, "
              "price_decimal, price_american, point, is_live_game) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
conn.commit()
n_rows = c.execute("SELECT COUNT(*) FROM odds_history").fetchone()[0]
best = c.execute(
    "SELECT game_id, market_key, outcome_name, COUNT(DISTINCT fetch_timestamp) pts "
    "FROM odds_history WHERE bookmaker_key != '_average' "
    "GROUP BY game_id, market_key, outcome_name ORDER BY pts DESC LIMIT 5").fetchall()
print(f"   inserted: {n_rows}")
print("   deepest series (distinct timestamps per game/market/outcome):")
for g, m, o, p in best:
    print(f"      {str(g)[:12]}  {m:8} {str(o)[:26]:28} {p} points")
deepest = best[0][3] if best else 0
RESULTS["P2"] = f"{'PASS' if deepest > 1 else 'FAIL'} - deepest series {deepest} points"

# ---------------------------------------------------------------- P3/P4
hdr("P3/P4", "LOGOS + CHART RENDERED FROM RESTORED HISTORY")
chart_path = None
if best:
    gid, mkt = best[0][0], best[0][1]
    series = c.execute(
        "SELECT bookmaker_key, outcome_name, fetch_timestamp, price_decimal, price_american "
        "FROM odds_history WHERE game_id=? AND market_key=? ORDER BY fetch_timestamp",
        (gid, mkt)).fetchall()
    outcomes = sorted({s[1] for s in series})[:2]
    away, home = (outcomes + outcomes)[:2]
    fig, axes = plt.subplots(len(outcomes), 1, figsize=(14, 9), sharex=True, squeeze=False)
    fig.patch.set_facecolor('#0F1419')
    for ax, oc in zip(axes[:, 0], outcomes):
        ax.set_facecolor('#1A1F2E')
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.grid(True, alpha=0.3, axis='y', color='#4A5568')
        ax.grid(True, alpha=0.1, axis='x', color='#4A5568')
        books = sorted({s[0] for s in series if s[1] == oc})
        for bk in books:
            pts = [(s[2], s[3]) for s in series if s[0] == bk and s[1] == oc and s[3]]
            if len(pts) < 1:
                continue
            xs = [p[0][11:16] for p in pts]
            ys = [p[1] for p in pts]
            if bk == '_average':
                ax.plot(xs, ys, color='#FFD700', linewidth=3.5, linestyle='--',
                        alpha=0.9, zorder=10, label='AVERAGE')
            else:
                ax.plot(xs, ys, color=BOOKMAKER_COLORS.get(bk, '#8a9aaf'),
                        linewidth=2, marker='o', markersize=6, alpha=0.9, zorder=12,
                        markeredgecolor='white', markeredgewidth=1, label=bk)
        allv = [s[3] for s in series if s[1] == oc and s[3]]
        if allv:
            lo, hi = min(allv), max(allv)
            pad = abs(hi - lo) * 0.15 or 0.05
            ax.set_ylim(hi + pad, lo - pad)
        ax.yaxis.set_major_formatter(FuncFormatter(american_fmt))
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position('right')
        ax.set_ylabel('American Odds', fontsize=10, color='#CCCCCC')
        ax.set_title(str(oc)[:40], fontsize=13, color='white', fontweight='bold')
        ax.legend(loc='upper left', fontsize=8, ncol=2, facecolor='#1A1F2E',
                  edgecolor='#4A5568', framealpha=0.9)
    axes[-1, 0].set_xlabel('Time', fontsize=10, color='#CCCCCC')

    la, lh = get_logo(away), get_logo(home)
    logos_used = 0
    if la is not None or lh is not None:
        from matplotlib.offsetbox import OffsetImage, AnnotationBbox
        fig.suptitle('', fontsize=1)
        axt = fig.add_axes([0, 0.92, 1, 0.08])
        axt.set_xlim(0, 1); axt.set_ylim(0, 1); axt.axis('off')
        axt.text(0.5, 0.5,
                 f"  {TEAM_ABBR.get(away, away)}  @  {TEAM_ABBR.get(home, home)}  "
                 f"— DEV PROOF  ",
                 transform=axt.transAxes, fontsize=14, color='white',
                 fontweight='bold', ha='center', va='center')
        for lg, x in ((la, 0.32), (lh, 0.68)):
            if lg is not None:
                axt.add_artist(AnnotationBbox(OffsetImage(lg, zoom=0.8), (x, 0.5),
                               frameon=False, xycoords='axes fraction'))
                logos_used += 1
    else:
        fig.suptitle("DEV PROOF", fontsize=14, color='white', fontweight='bold')
    plt.tight_layout(rect=[0, 0, 1, 0.88])
    chart_path = os.path.join(OUT, f"NFL_devproof_{mkt}.png")
    plt.savefig(chart_path, dpi=130, facecolor='#0F1419', bbox_inches='tight')
    plt.close(fig)

    im = Image.open(chart_path).convert("RGB")
    a = np.array(im)
    band = a[: int(im.height * 0.14)]
    r_, g_, b_ = band[:, :, 0].astype(int), band[:, :, 1].astype(int), band[:, :, 2].astype(int)
    mx, mn = np.maximum(np.maximum(r_, g_), b_), np.minimum(np.minimum(r_, g_), b_)
    sat = int((((mx - mn) > 45) & (mx > 70)).sum())
    print(f"   chart: {chart_path}  {im.size[0]}x{im.size[1]}  {os.path.getsize(chart_path):,} bytes")
    print(f"   logos embedded: {logos_used}   saturated px in title band: {sat:,}")
    RESULTS["P3"] = f"{'PASS' if sat > 400 else 'FAIL'} - {logos_used} logos, {sat} sat px"
    RESULTS["P4"] = f"PASS - chart rendered from {len(series)} restored rows"
else:
    print("   SKIPPED: no restored history to chart")
    RESULTS["P3"] = RESULTS["P4"] = "SKIP - no history"

# ---------------------------------------------------------------- P6
hdr("P6", "IDEMPOTENCY")
c.executemany("INSERT OR IGNORE INTO odds_history (game_id, fetch_timestamp, "
              "bookmaker_key, bookmaker_title, market_key, outcome_name, "
              "price_decimal, price_american, point, is_live_game) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)", payload)
conn.commit()
n2 = c.execute("SELECT COUNT(*) FROM odds_history").fetchone()[0]
print(f"   replayed {len(payload)} rows: {n_rows} -> {n2}")
RESULTS["P6"] = f"{'PASS' if n2 == n_rows else 'FAIL'} - {n_rows} -> {n2}"
conn.close()

# ---------------------------------------------------------------- P5
hdr("P5", "DISCORD DELIVERY — TEST CHANNEL ONLY")
hook = os.environ.get("DISCORD_WEBHOOK_TESTING", "")
if not hook:
    print("   SKIP: DISCORD_WEBHOOK_TESTING not set")
    RESULTS["P5"] = "SKIP - no test webhook"
elif not chart_path:
    print("   SKIP: nothing rendered")
    RESULTS["P5"] = "SKIP - no chart"
else:
    # discord.com fails TLS from HF Spaces; discordapp.com is the alias that
    # works. Harmless on an Actions runner, and keeps one code path everywhere.
    hook = hook.replace("https://discord.com/", "https://discordapp.com/", 1)
    summary = " | ".join(f"{k} {v}" for k, v in RESULTS.items())
    try:
        with open(chart_path, "rb") as f:
            rr = SESSION.post(hook + "?wait=true",
                              data={"content": f"**NFL DEV PROOF** — zero TheOddsAPI "
                                               f"credits spent\n{summary}"[:1900]},
                              files={"file": (os.path.basename(chart_path), f, "image/png")},
                              timeout=30)
        mid = (rr.json() or {}).get("id") if rr.status_code < 300 else None
        print(f"   HTTP {rr.status_code}  message_id={mid}")
        RESULTS["P5"] = f"{'PASS' if mid else 'FAIL'} - HTTP {rr.status_code}"
    except Exception as e:
        print(f"   ERROR {type(e).__name__}: {e}")
        RESULTS["P5"] = f"FAIL - {type(e).__name__}"

hdr("SUMMARY", "")
for k in sorted(RESULTS):
    print(f"   {k}: {RESULTS[k]}")
with open(os.path.join(OUT, "RESULTS.json"), "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, indent=1)
print(f"\n   artifacts -> {OUT}")
