"""
unified_scanner.py — ALL-VENUE marriage scanner (Kalshi | Polymarket US | ProphetX | Novig)

Reads every venue's board each cycle, normalizes to canonical entities,
marries offers across books, detects fee-adjusted arbitrage, and writes:

    results/latest_scan.json   <- full snapshot (dashboard reads this)
    results/history.jsonl      <- append-only scan timeline (proof trail)

Venue autonomy matrix:
    Kalshi        PUBLIC  -> always scans
    Polymarket US PUBLIC  -> always scans
    ProphetX      JWT     -> scans when PROPHETX_JWT secret present, else SKIPPED (visible)
    Novig         OAuth   -> scans when NOVIG_CLIENT_ID/SECRET present, else SKIPPED (visible)

Exit codes: 0 ok | 7 nothing scannable | 1 error
"""

import base64
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "abet-desk/1.0", "Accept": "application/json"}
MIN_EDGE_BPS = int(os.environ.get("MIN_EDGE_BPS", "30"))
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "400"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", "results")
HISTORY_MAX_LINES = 5000

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PMUS_GAMMA = "https://gamma-api.polymarket.com"
PX_API = "https://cash.api.prophetx.co"
NOVIG_API = "https://api.novig.us"

# ─────────────────────────── HTTP ───────────────────────────
def http_json(url, headers=None, timeout=25):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_json_err(url, headers=None, timeout=25):
    """Returns (data|None, err_string|None)."""
    try:
        return http_json(url, headers, timeout), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)[:90]


# ─────────────────────── FEES ───────────────────────
def kalshi_fee_cents(price_cents: int) -> int:
    if price_cents <= 0 or price_cents >= 100:
        return 0
    raw = 0.07 * (price_cents / 100.0) * (1 - price_cents / 100.0)
    return max(1, math.ceil(raw * 100))

VENUE_FEE_MODEL = {
    "kalshi": "quadratic_07pct",
    "pmus": "zero_taker",
    "prophetx": "p2p_zero",
    "novig": "maker_zero",
}


# ─────────────────── VENUE ADAPTERS ───────────────────
def fetch_kalshi():
    """Two-sided single-contract markets. Returns (list, None) or (None, err)."""
    out, cursor, pages = [], None, 0
    while len(out) < SCAN_LIMIT and pages < 15:
        url = f"{KALSHI_API}/markets?status=open&limit=1000" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        data, err = http_json_err(url)
        if err:
            return (out, None) if out else (None, err)
        for m in data.get("markets", []):
            tk = m.get("ticker", "")
            if "SHARD" in tk.upper() or "MVE" in tk.upper():
                continue
            try:
                bid = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
                ask = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
            except (TypeError, ValueError):
                continue
            title = m.get("title", "").strip()
            if not title or not (0 < bid <= ask < 100):
                continue
            out.append({
                "venue": "kalshi",
                "key": tk,
                "title": title,
                "yes_bid": bid,
                "yes_ask": ask,
                "close": m.get("close_time", ""),
            })
            if len(out) >= SCAN_LIMIT:
                break
        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break
    return out, None


def fetch_pmus():
    url = f"{PMUS_GAMMA}/markets?active=true&closed=false&limit={SCAN_LIMIT}&order=volume24hr&ascending=false"
    data, err = http_json_err(url)
    if err:
        return None, err
    out = []
    for m in data if isinstance(data, list) else []:
        q = m.get("question", "").strip()
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
        except Exception:
            prices = []
        if len(prices) < 2 or not q:
            continue
        try:
            yes = float(prices[0]) * 100
        except ValueError:
            continue
        if not (0 < yes < 100):
            continue
        out.append({
            "venue": "pmus",
            "key": m.get("conditionId", ""),
            "title": q,
            "yes_bid": int(round(yes)),       # last-trade proxy (no public depth)
            "yes_ask": int(round(yes)),
            "close": m.get("endDate", "") or "",
            "proxy": "last_trade",
        })
    return out, None


def american_to_cents(odds):
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o > 0:
        p = 100.0 / (o + 100.0)
    elif o < 0:
        p = (-o) / ((-o) + 100.0)
    else:
        return None
    c = int(round(p * 100))
    return c if 0 < c < 100 else None


def fetch_prophetx():
    """Returns (markets|None, err). err='SKIPPED_NO_CREDENTIALS' when unconfigured."""
    jwt = os.environ.get("PROPHETX_JWT", "").strip()
    if not jwt:
        return None, "SKIPPED_NO_CREDENTIALS"
    hdrs = {"Authorization": f"Bearer {jwt}"}
    # Board endpoints confirmed live (401 without token -> exist & auth-gated)
    games, err = http_json_err(f"{PX_API}/trade/private/api/v2/games", hdrs)
    if err:
        return None, err
    items = games if isinstance(games, list) else games.get("games", []) if isinstance(games, dict) else []
    out = []

    def walk_lines(obj, game_title):
        """Defensive: collect anything that looks like a wagerable line."""
        found = []
        if isinstance(obj, dict):
            odds = obj.get("odds") or obj.get("americanOdds") or obj.get("price")
            lid = obj.get("lineID") or obj.get("lineId") or obj.get("id")
            side = obj.get("side") or obj.get("type") or ""
            if odds is not None and lid is not None:
                c = american_to_cents(odds)
                if c:
                    label = obj.get("name") or obj.get("title") or obj.get("selection") or side
                    found.append((str(lid), f"{game_title} | {label}".strip(" |"), side, c))
            for v in obj.values():
                found.extend(walk_lines(v, game_title))
        elif isinstance(obj, list):
            for v in obj:
                found.extend(walk_lines(v, game_title))
        return found

    for g in items:
        gtitle = g.get("name") or g.get("description") or g.get("title") or ""
        for lid, title, side, cents in walk_lines(g, str(gtitle)):
                out.append({
                    "venue": "prophetx",
                    "key": str(lid),
                    "title": title[:180],
                    "yes_bid": cents if side.lower() in ("over", "yes", "long", "home", "away", "moneyline") else int(round(100 - cents)) ,
                    "yes_ask": cents if side.lower() in ("over", "yes", "long") else int(round(100 - cents)),
                    "close": "",
                    "proxy": "p2p_line",
                })
    return out, None


def fetch_novig():
    cid = os.environ.get("NOVIG_CLIENT_ID", "").strip()
    sec = os.environ.get("NOVIG_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        return None, "SKIPPED_NO_CREDENTIALS"
    # NOTE: oauth body must be form-encoded
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": sec,
    }).encode()
    req = urllib.request.Request(f"{NOVIG_API}/oauth/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tokd = json.loads(r.read().decode())
        access = tokd.get("access_token")
        if not access:
            return None, "oauth:no_token"
    except Exception as e:
        return None, f"oauth:{str(e)[:70]}"
    mkts, err = http_json_err(f"{NOVIG_API}/nbx/v2/markets?limit={SCAN_LIMIT}",
                              headers={"Authorization": f"Bearer {access}"})
    if err:
        return None, err
    items = mkts if isinstance(mkts, list) else mkts.get("markets", []) if isinstance(mkts, dict) else []
    out = []
    for m in items:
        title = m.get("name") or m.get("title") or m.get("question") or ""
        best_yes = m.get("best_yes_ask") or m.get("bestAskYes") or {}
        best_no = m.get("best_no_ask") or m.get("bestAskNo") or {}
        try:
            ya = int(round(float(best_yes.get("price", 0)) * 100)) if isinstance(best_yes, dict) else None
            nb = int(round(100 - float(best_no.get("price", 1)) * 100)) if isinstance(best_no, dict) else None
        except (TypeError, ValueError):
            continue
        if not title or not ya:
            continue
        out.append({
            "venue": "novig",
            "key": str(m.get("id", "")),
            "title": str(title)[:180],
            "yes_bid": nb or max(1, ya - 1),
            "yes_ask": ya,
            "close": "",
        })
    return out, None


# ─────────────────── CANONICALIZATION + MATCHING ───────────────────
STOP = {"the","a","an","will","of","in","on","at","to","for","by","with","vs","versus",
        "and","or","be","is","are","who","what","when","where","which","that","this"}

def norm(s):
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return " ".join(w for w in s.split() if w not in STOP and len(w) > 1)

def jaccard(a, b):
    A, B = set(a.split()), set(b.split())
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)

def sim(t1, t2):
    n1, n2 = norm(t1), norm(t2)
    return 0.6 * SequenceMatcher(None, n1, n2).ratio() + 0.4 * jaccard(n1, n2)

def match_pairs(markets):
    """Greedy cross-venue marriage at >=55% similarity."""
    by_venue = {}
    for m in markets:
        by_venue.setdefault(m["venue"], []).append(m)
    venues = sorted(by_venue.keys())
    pairs = []
    used = set()
    for i, va in enumerate(venues):
        for vb in venues[i + 1:]:
            for ma in by_venue[va]:
                if ma["key"] in used:
                    continue
                best, bs = None, 0.0
                for mb in by_venue[vb]:
                    if mb["key"] in used:
                        continue
                    s = sim(ma["title"], mb["title"])
                    if s > bs:
                        best, bs = mb, s
                if best and bs >= 0.55:
                    pairs.append({"a": ma, "b": best, "confidence": round(bs, 3)})
                    used.add(ma["key"]); used.add(best["key"])
    return pairs


def detect_arbs(pairs):
    arbs = []
    for p in pairs:
        for x, y in ((p["a"], p["b"]), (p["b"], p["a"])):
            # buy YES on x at ask + buy NO on y (no ask = 100 - yes_bid on y)
            cost_cents = x["yes_ask"] + (100 - y["yes_bid"])
            fees = kalshi_fee_cents(x["yes_ask"] if x["venue"] == "kalshi" else 0) \
                 + kalshi_fee_cents(100 - y["yes_bid"] if y["venue"] == "kalshi" else 0)
            total = cost_cents + fees
            edge_bps = (100 - total) * 100
            if total < 100 and edge_bps >= MIN_EDGE_BPS:
                arbs.append({
                    "buy_yes": {"venue": x["venue"], "title": x["title"], "ask_cents": x["yes_ask"]},
                    "buy_no":  {"venue": y["venue"], "title": y["title"], "no_cost_cents": 100 - y["yes_bid"]},
                    "total_cost_cents": total,
                    "fees_cents": fees,
                    "edge_bps": edge_bps,
                    "match_confidence": p["confidence"],
                })
    return sorted(arbs, key=lambda z: -z["edge_bps"])


# ─────────────────────────── MAIN ───────────────────────────
def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    venues_status, all_markets = [], []

    specs = [("kalshi", fetch_kalshi), ("pmus", fetch_pmus),
             ("prophetx", fetch_prophetx), ("novig", fetch_novig)]

    for name, fn in specs:
        vt0 = time.time()
        try:
            data, err = fn()
            if err == "SKIPPED_NO_CREDENTIALS":
                state, detail = "SKIPPED_NO_CREDENTIALS", "add secret to enable"
            elif err:
                state, detail = "ERROR", err
            else:
                state, detail = "OK", ""
            venues_status.append({
                "venue": name, "state": state,
                "markets": len(data) if data else 0,
                "latency_ms": int((time.time() - vt0) * 1000),
                "detail": detail,
            })
            if data:
                all_markets.extend(data)
        except Exception as e:
            venues_status.append({"venue": name, "state": "EXCEPTION", "markets": 0,
                                  "latency_ms": int((time.time() - vt0) * 1000), "detail": str(e)[:120]})

    pairs = match_pairs(all_markets)
    arbs = detect_arbs(pairs)

    snapshot = {
        "scanned_at": now.isoformat(),
        "duration_s": round(time.time() - t0, 1),
        "min_edge_bps": MIN_EDGE_BPS,
        "venues": venues_status,
        "totals": {
            "markets_scanned": len(all_markets),
            "pairs_married": len(pairs),
            "arbitrages": len(arbs),
        },
        "arbitrages": arbs[:25],
        "pairs_sample": [
            {"a": p["a"]["title"][:80], "va": p["a"]["venue"],
             "b": p["b"]["title"][:80], "vb": p["b"]["venue"],
             "conf": p["confidence"]}
            for p in pairs[:40]
        ],
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "latest_scan.json"), "w") as f:
        json.dump(snapshot, f, indent=1)

    hist_path = os.path.join(RESULTS_DIR, "history.jsonl")
    row = {
        "ts": now.isoformat(), "dur": snapshot["duration_s"],
        "mk": len(all_markets), "pairs": len(pairs), "arbs": len(arbs),
        "v": {s["venue"]: f'{s["state"]}:{s["markets"]}' for s in venues_status},
    }
    existing = []
    if os.path.exists(hist_path):
        with open(hist_path) as f:
            existing = f.readlines()
    existing.append(json.dumps(row) + "\n")
    with open(hist_path, "w") as f:
        f.writelines(existing[-HISTORY_MAX_LINES:])

    print(f"[SCAN] {now.isoformat()} | venues: "
          + " ".join(f'{s["venue"]}={s["state"]}({s["markets"]})' for s in venues_status))
    print(f"[SCAN] markets={len(all_markets)} married_pairs={len(pairs)} arbs>= {MIN_EDGE_BPS}bps: {len(arbs)}")

    if os.environ.get("DISCORD_WEBHOOK_ABET"):
        for a in arbs[:3]:
            try:
                payload = json.dumps({
                    "content": f"\u0023\ufe0f\u20e3 ARB {a['edge_bps']}bps \u2014 "
                               f"YES {a['buy_yes']['venue']} @{a['buy_yes']['ask_cents']}\u00a2 + "
                               f"NO {a['buy_no']['venue']} @{a['buy_no']['no_cost_cents']}\u00a2 "
                               f"= {a['total_cost_cents']}\u00a2 (conf {a['match_confidence']:.0%})",
                }).encode()
                rq = urllib.request.Request(os.environ["DISCORD_WEBHOOK_ABET"], data=payload,
                                            headers={"Content-Type": "application/json"})
                urllib.request.urlopen(rq, timeout=10)
            except Exception as e:
                print(f"[WARN] discord: {e}")

    scannable = sum(1 for s in venues_status if s["state"] == "OK")
    sys.exit(0 if scannable >= 1 else 7)


if __name__ == "__main__":
    main()
