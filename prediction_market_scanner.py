"""
prediction_market_scanner.py — Cross-venue arb scanner (Kalshi ↔ Polymarket US)

Reads PUBLIC market data from both venues, resolves entities across venues,
detects fee-adjusted arbitrage opportunities, and writes structured results.

NO SECRETS REQUIRED. All endpoints are public read-only.

Usage:
  python prediction_market_scanner.py --once          # single scan cycle
  python prediction_market_scanner.py --once --json   # machine-readable output

Exit codes:
  0 = scan completed successfully
  7 = no markets found on either venue (off-hours)
  1 = error

Follows the same sustain-governor pattern as nfl_unified_odds_monitor.py.
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PMUS_GAMMA = "https://gamma-api.polymarket.com"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ABET", "")
MIN_EDGE_BPS = int(os.environ.get("MIN_EDGE_BPS", "50"))  # 50bps = 0.5%
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "500"))

# ── Kalshi quadratic fee model (verified vs docs.kalshi.com) ────────────
def kalshi_fee_cents(price_cents: int) -> int:
    """ceil(0.07 * C * P * (1-P)) rounded UP to nearest cent."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    raw = 0.07 * p * (1 - p)
    return max(1, math.ceil(raw * 100))


# ── HTTP helpers ────────────────────────────────────────────────────────
def http_get_json(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "abet-scanner/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[WARN] GET {url[:80]}... failed: {e}")
        return None


# ── Kalshi fetcher ──────────────────────────────────────────────────────
def fetch_kalshi_markets(limit=500):
    """Fetch open Kalshi single-contract markets with two-sided quotes.
    Paginates through results, skipping SHARD/MVE composite baskets."""
    markets = []
    cursor = None
    pages = 0
    max_pages = 15  # covers ~15k markets (shards dominate default sort)

    while len(markets) < limit and pages < max_pages:
        url = f"{KALSHI_API}/markets?status=open&limit=1000"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        data = http_get_json(url)
        if not data or "markets" not in data:
            break

        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            # Skip SHARD/MVE composite baskets entirely
            if "SHARD" in ticker.upper() or "MVE" in ticker.upper():
                continue
            try:
                yes_bid = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
                yes_ask = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
            except (ValueError, TypeError):
                continue
            title = m.get("title", "").strip()
            close_time = m.get("close_time", "")

            # Only clean two-sided markets
            if yes_bid <= 0 or yes_ask <= 0 or yes_bid >= 100 or yes_ask >= 100 or yes_ask < yes_bid:
                continue
            if not title or not ticker:
                continue

            markets.append({
                "venue": "kalshi",
                "ticker": ticker,
                "title": title,
                "yes_bid_cents": yes_bid,
                "yes_ask_cents": yes_ask,
                "close_time": close_time,
                "volume": int(float(m.get("volume_fp") or m.get("volume_24h_fp") or 0)),
            })
            if len(markets) >= limit:
                break

        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break

    return markets


# ── PM-US fetcher ──────────────────────────────────────────────────────
def fetch_pmus_markets(limit=500):
    """Fetch active Polymarket US markets from Gamma API."""
    url = f"{PMUS_GAMMA}/markets?active=true&closed=false&limit={limit}&order=volume24hr&ascending=false"
    data = http_get_json(url)
    if not isinstance(data, list):
        return []
    
    markets = []
    for m in data:
        question = m.get("question", "").strip()
        condition_id = m.get("conditionId", "")
        
        # Get best bid/ask from outcomePrices or clobTokenIds
        outcomes = m.get("outcomes", [])
        outcome_prices_str = m.get("outcomePrices", "")
        
        try:
            prices = json.loads(outcome_prices_str) if outcome_prices_str else []
        except:
            prices = []
        
        if len(prices) < 2 or not question:
            continue
        
        try:
            yes_price = float(prices[0]) * 100  # convert to cents
            no_price = float(prices[1]) * 100
        except (ValueError, IndexError):
            continue
        
        if yes_price <= 0 or yes_price >= 100:
            continue
        
        markets.append({
            "venue": "pmus",
            "condition_id": condition_id,
            "title": question,
            "yes_price_cents": round(yes_price, 1),
            "no_price_cents": round(no_price, 1),
            "volume_24h": float(m.get("volume24hr", 0)),
            "end_date": m.get("endDate", ""),
        })
    return markets


# ── Entity normalization & matching ─────────────────────────────────────
STOPWORDS = {
    "the","a","an","will","of","in","on","at","to","for","by","with",
    "vs","vs.","versus","against","and","or","be","is","are","was",
    "who","what","when","where","which","that","this","these","those",
}

def normalize_text(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    words = [w for w in s.split() if w not in STOPWORDS and len(w) > 1]
    return " ".join(words)


def extract_teams_and_date(title):
    """Try to extract team names and date info from a sports title."""
    # Common patterns: "Lakers vs Celtics", "Team A @ Team B"
    teams = []
    separators = [" vs ", " vs. ", " @ ", " at ", " - "]
    for sep in separators:
        if sep in title.lower():
            parts = title.lower().split(sep, 1)
            if len(parts) == 2:
                teams = [normalize_text(parts[0]), normalize_text(parts[1])]
                break
    
    # Extract year/month/day patterns
    date_match = re.search(r"(20\d{2})", title)
    year = date_match.group(1) if date_match else None
    
    month_names = ["january","february","march","april","may","june",
                   "july","august","september","october","november","december"]
    month_match = next((m for m in month_names if m in title.lower()), None)
    
    day_match = re.search(r"\b(\d{1,2})\b", title)
    day = int(day_match.group(1)) if day_match else None
    
    return {"teams": teams, "year": year, "month": month_match, "day": day}


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def match_entities(kalshi_markets, pmus_markets):
    """Find entity pairs that likely refer to the same event."""
    pairs = []
    
    for k in kalshi_markets:
        k_norm = normalize_text(k["title"])
        k_info = extract_teams_and_date(k["title"])
        
        best_match = None
        best_score = 0.0
        
        for p in pmus_markets:
            p_norm = normalize_text(p["title"])
            p_info = extract_teams_and_date(p["title"])
            
            # Quick reject: different years
            if k_info["year"] and p_info["year"] and k_info["year"] != p_info["year"]:
                continue
            
            # Team overlap boost
            team_score = 0.0
            if k_info["teams"] and p_info["teams"]:
                common = set(k_info["teams"]) & set(p_info["teams"])
                if common:
                    team_score = 0.3 * len(common) / max(len(k_info["teams"]), len(p_info["teams"]))
            
            sim = similarity(k_norm, p_norm)
            score = min(1.0, sim + team_score)
            
            if score > best_score:
                best_score = score
                best_match = p
        
        if best_match and best_score >= 0.55:  # threshold
            pairs.append({
                "kalshi": k,
                "pmus": best_match,
                "match_confidence": round(best_score, 3),
            })
    
    return pairs


# ── Arb detection ──────────────────────────────────────────────────────
def detect_arbs(pairs):
    """
    For each matched pair, check if buying YES on one venue and NO on the
    other costs less than $1 total (after fees).
    
    Kalshi fee applies per side based on price paid.
    PM-US currently has zero trading fees (as of Aug 2026).
    """
    arbs = []
    
    for pair in pairs:
        k, p = pair["kalshi"], pair["pmus"]
        
        # Strategy A: Buy YES@Kalshi + NO@PM-US
        # Cost = K_yes_ask + P_no_price
        cost_a = k["yes_ask_cents"] / 100.0 + p["no_price_cents"] / 100.0
        fee_k = kalshi_fee_cents(k["yes_ask_cents"]) / 100.0
        total_cost_a = cost_a + fee_k
        
        # Strategy B: Buy NO@Kalshi + YES@PM-US  
        # NO@Kalshi ask = 100 - yes_bid
        k_no_ask = 100 - k["yes_bid_cents"]
        cost_b = k_no_ask / 100.0 + p["yes_price_cents"] / 100.0
        fee_k_no = kalshi_fee_cents(k_no_ask) / 100.0
        total_cost_b = cost_b + fee_k_no
        
        payout = 1.00  # binary contract pays $1 if correct
        
        for strategy, total_cost in [("YES_K_NO_P", total_cost_a), ("NO_K_YES_P", total_cost_b)]:
            edge_dollars = payout - total_cost
            edge_bps = round(edge_dollars * 10000, 1)
            
            if edge_bps >= MIN_EDGE_BPS:
                arbs.append({
                    **pair,
                    "strategy": strategy,
                    "total_cost": round(total_cost, 4),
                    "payout": payout,
                    "edge_dollars": round(edge_dollars, 4),
                    "edge_bps": edge_bps,
                    "kalshi_leg": k,
                    "pmus_leg": p,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
    
    return sorted(arbs, key=lambda x: -x["edge_bps"])


# ── Discord alerting ───────────────────────────────────────────────────
def post_discord(arb):
    """Post arb opportunity to Discord webhook."""
    if not DISCORD_WEBHOOK:
        return False
    try:
        payload = {
            "content": f"🎯 **ARB DETECTED** {arb['edge_bps']}bps",
            "embeds": [{
                "title": f"Cross-venue arb: {arb['edge_bps']} bps",
                "description": (
                    f"**Kalshi**: {arb['kalshi']['title']}\n"
                    f"  Ask: {arb['kalshi']['yes_ask_cents']}¢\n"
                    f"**PM-US**: {arb['pmus']['title']}\n"
                    f"  Price: {arb['pmus']['yes_price_cents']}¢\n\n"
                    f"**Strategy**: {arb['strategy']}\n"
                    f"**Total cost**: ${arb['total_cost']:.4f}\n"
                    f"**Payout**: ${arb['payout']:.2f}\n"
                    f"**Edge**: ${arb['edge_dollars']:.4f} ({arb['edge_bps']} bps)\n"
                    f"**Match confidence**: {arb['match_confidence']:.0%}"
                ),
                "color": 0x00FF00,  # green
                "timestamp": arb["detected_at"],
            }]
        }
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f"[WARN] Discord post failed: {e}")
        return False


# ── Main scanner loop ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="single scan then exit")
    parser.add_argument("--json", action="store_true", help="output JSON only")
    args = parser.parse_args()
    
    started = datetime.now(timezone.utc).isoformat()
    
    print(f"[SCANNER] Starting cross-venue scan at {started}")
    print(f"[SCANNER] Min edge: {MIN_EDGE_BPS}bps | Limit: {SCAN_LIMIT}")
    
    # Step 1: Fetch both venues
    kalshi = fetch_kalshi_markets(SCAN_LIMIT)
    pmus = fetch_pmus_markets(SCAN_LIMIT)
    
    print(f"[FETCH] Kalshi: {len(kalshi)} two-sided markets")
    print(f"[FETCH] PM-US:  {len(pmus)} active markets")
    
    if not kalshi or not pmus:
        result = {
            "status": "no_markets",
            "kalshi_count": len(kalshi),
            "pmus_count": len(pmus),
            "scanned_at": started,
        }
        print(json.dumps(result))
        sys.exit(7)
    
    # Step 2: Match entities
    pairs = match_entities(kalshi, pmus)
    print(f"[MATCH] Found {len(pairs)} entity pairs with confidence ≥55%")
    
    # Step 3: Detect arbs
    arbs = detect_arbs(pairs)
    print(f"[ARB] Detected {len(arbs)} opportunities ≥{MIN_EDGE_BPS}bps")
    
    # Step 4: Alert on each arb
    alerted = 0
    for arb in arbs:
        if post_discord(arb):
            alerted += 1
    
    # Step 5: Write results
    result = {
        "status": "ok",
        "scanned_at": started,
        "kalshi_markets": len(kalshi),
        "pmus_markets": len(pmus),
        "matched_pairs": len(pairs),
        "arbitrages_detected": len(arbs),
        "discord_alerts_sent": alerted,
        "min_edge_bps_threshold": MIN_EDGE_BPS,
        "arbs": arbs[:20],  # top 20 by edge
        "pairs_sample": [
            {"k": p["kalshi"]["title"], "p": p["pmus"]["title"], "conf": p["match_confidence"]}
            for p in pairs[:10]
        ],
    }
    
    output_path = os.environ.get("SCAN_OUTPUT_PATH", "scan_results.json")
    try:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[WRITE] Results saved to {output_path}")
    except OSError:
        import tempfile
        output_path = os.path.join(tempfile.gettempdir(), "scan_results.json")
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[WRITE] Results saved to {output_path} (fallback)")
    
    if args.json:
        print(json.dumps(result))
    
    sys.exit(0)


if __name__ == "__main__":
    main()
