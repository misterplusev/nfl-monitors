"""
desk_daemon.py — THE DESK. Persistent, event-driven, loops forever.

Runs inside one GitHub Actions job (up to ~5h45m, then cron respawns it).
Architecture:
  - WebSocket connectors per venue (auto-reconnect, exponential backoff)
  - Every tick arrives -> book updated -> affected cross-venue pairs
    re-evaluated IMMEDIATELY (event-driven, microseconds per tick)
  - REST sweep loop round-robins venues without live tick flow (~1s cadence)
  - Results written continuously; git commit every COMMIT_EVERY_S

Venue feeds:
  kalshi    WSS wss://external-api-ws.kalshi.com/trade-api/ws/v2  (+REST)
  pmus      WSS wss://ws-subscriptions-clob.polymarket.com/ws/market (+gamma REST)
  prophetx  WSS wss://ws-cash.prophetx.co (attempt) + REST when JWT present
  novig     NBX REST when OAuth creds present

Exit code 0 at deadline so Actions uploads state cleanly.
"""

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

try:
    import websockets  # pip install websockets
except ImportError:
    os.system(f"{sys.executable} -m pip install -q websockets")
    import websockets

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "340"))
UA = {"User-Agent": "abet-desk/1.0", "Accept": "application/json"}
COMMIT_EVERY_S = int(os.environ.get("COMMIT_EVERY_S", "300"))
SWEEP_EVERY_S = float(os.environ.get("SWEEP_EVERY_S", "1.0"))
MIN_EDGE_BPS = int(os.environ.get("MIN_EDGE_BPS", "30"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", "results")

KALSHI_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PM_GAMMA = "https://gamma-api.polymarket.com"
PMUS_API = "https://api.polymarket.us"   # REAL CFTC exchange (Ed25519 auth)
PX_WS = "wss://ws-cash.prophetx.co"
PX_API = "https://cash.api.prophetx.co"
NOVIG_API = "https://api.novig.us"

DEADLINE = time.time() + RUN_MINUTES * 60

# ───────────────────────── books ─────────────────────────
class Book:
    __slots__ = ("venue", "title", "yes_bid", "yes_ask", "ts", "proxy")
    def __init__(self, venue, title, yes_bid, yes_ask, proxy=""):
        self.venue, self.title = venue, title
        self.yes_bid, self.yes_ask = int(yes_bid), int(yes_ask)
        self.ts = time.time()
        self.proxy = proxy
    def ok(self):
        return 0 < self.yes_bid <= self.yes_ask < 100


BOOKS: dict[tuple, Book] = {}          # (venue,key) -> Book
TITLES: dict[tuple, str] = {}
PAIRS_CACHE: list = []                 # [(ka,kb)]
LAST_TICK_TS: dict[str, float] = defaultdict(float)   # venue -> last data ts
STATS = defaultdict(int)


# ───────────────────── normalization ─────────────────────
STOP = {"the","a","an","will","of","in","on","at","to","for","by","with","vs",
        "versus","and","or","be","is","are","who","what","when","where","which"}

def norm(s):
    s = re.sub(r"[^a-z0-9\s]", " ", str(s).lower())
    return " ".join(w for w in s.split() if w not in STOP and len(w) > 1)

def sim(t1, t2):
    n1, n2 = norm(t1), norm(t2)
    if not n1 or not n2:
        return 0.0
    A, B = set(n1.split()), set(n2.split())
    j = len(A & B) / len(A | B)
    if j < 0.25:            # cheap prefilter: skip SequenceMatcher on dud pairs
        return 0.0
    return 0.6 * SequenceMatcher(None, n1, n2).ratio() + 0.4 * j


def put_book(venue, key, title, bid, ask, proxy=""):
    bk = BOOKS.get((venue, key))
    changed = (bk is None or bk.yes_bid != bid or bk.yes_ask != ask or bk.title != title)
    BOOKS[(venue, key)] = Book(venue, title, bid, ask, proxy)
    TITLES[(venue, key)] = title
    LAST_TICK_TS[venue] = time.time()
    STATS[f"{venue}.ticks"] += 1
    if changed:
        STATS[f"{venue}.updates"] += 1
    return changed


ANTONYM_GROUPS = [
    {"cut", "hike", "raise", "increase", "decrease", "reduce"},
    {"closed", "close", "reopen", "open", "resume", "normal"},
    {"above", "below", "under", "over"},
    {"higher", "lower"},
    {"win", "lose", "eliminated"},
    {"before", "after"},
]

MONTHS = ["january","february","march","april","may","june","july",
          "august","september","october","november","december"]

def date_signature(s):
    """Extract (month, day, year-ish) deadline hints for compatibility checks."""
    s = s.lower()
    sig = set()
    for i, mn in enumerate(MONTHS):
        if mn in s:
            sig.add(("m", i))
    m = re.search(r"\b(20\d{2})\b", s)
    if m:
        sig.add(("y", m.group(1)))
    m = re.search(r"\b(?:by\s+)?(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\b", s)
    if m:
        sig.add(("md", m.group(1) + "/" + m.group(2)))
    return sig

def incompatible(t1, t2):
    """True if two titles refer to logically distinct events."""
    a1 = {w for w in norm(t1).split()}
    a2 = {w for w in norm(t2).split()}
    for grp in ANTONYM_GROUPS:
        hit1 = a1 & grp
        hit2 = a2 & grp
        if hit1 and hit2 and hit1 != hit2:
            return True
    # date/deadline mismatch: both mention months but different ones
    d1, d2 = date_signature(t1), date_signature(t2)
    m1 = {v for k, v in d1 if k == "m"}
    m2 = {v for k, v in d2 if k == "m"}
    if m1 and m2 and not (m1 & m2):
        return True
    y1 = {v for k, v in d1 if k == "y"}
    y2 = {v for k, v in d2 if k == "y"}
    if y1 and y2 and not (y1 & y2):
        return True
    md1 = {v for k, v in d1 if k == "md"}
    md2 = {v for k, v in d2 if k == "md"}
    if md1 and md2 and not (md1 & md2):
        return True
    # deadline ASYMMETRY: explicit month/day/month-day on one side only
    # => different resolution criteria => never marry for arb purposes
    hard1 = m1 | md1
    hard2 = m2 | md2
    if (hard1 and not hard2) or (hard2 and not hard1):
        return True
    return False


def rebuild_pairs():
    """Inverted-index cross-venue marriage.
    Candidates = title pairs sharing >=1 distinctive token (df<=3000, len>3),
    then scored with sim(). Handles 10k x 5k boards cheaply."""
    global PAIRS_CACHE
    normd = {}
    for (v, k), b in BOOKS.items():
        if b.ok():
            n = norm(b.title)
            if n:
                normd[(v, k)] = n
    postings = defaultdict(list)
    for key, n in normd.items():
        for t in set(n.split()):
            postings[t].append(key)
    distinctive = [t for t, ps in postings.items() if 1 < len(ps) <= 3000 and len(t) > 3]
    cand = set()
    for t in distinctive:
        ps = postings[t]
        if len(ps) > 60:          # cap fan-out per token
            continue
        for i in range(len(ps)):
            for jn in range(i + 1, len(ps)):
                ka, kb = ps[i], ps[jn]
                if ka[0] == kb[0]:
                    continue
                pair = (ka, kb) if ka[0] < kb[0] else (kb, ka)
                cand.add(pair)
    pairs = []
    for a, b in cand:
        ta = normd[a]
        tb = normd[b]
        # cheap guards before expensive scoring
        ja = jaccard_raw(ta, tb)
        if ja < 0.30:
            continue
        if incompatible(TITLES[a], TITLES[b]):
            STATS["pairs_blocked_antonym_date"] += 1
            continue
        if sim(ta, tb) >= 0.70:
            pairs.append((a, b))
    PAIRS_CACHE = pairs


def jaccard_raw(n1, n2):
    A, B = set(n1.split()), set(n2.split())
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def evaluate_all():
    """Fee-adjusted arb across every married pair."""
    def vfee(venue, cents):
        if venue == "kalshi":
            return kalshi_fee(cents)
        if venue == "pmus":
            return pmus_fee_cents(cents)
        return 0

    arbs = []
    for (a, b) in PAIRS_CACHE:
        ba, bb = BOOKS.get(a), BOOKS.get(b)
        if not ba or not bb or not ba.ok() or not bb.ok():
            continue
        for x, y in ((ba, bb), (bb, ba)):
            no_y = 100 - y.yes_bid
            total = x.yes_ask + no_y + vfee(x.venue, x.yes_ask) + vfee(y.venue, no_y)
            edge_bps = (100 - total) * 100
            if edge_bps >= MIN_EDGE_BPS:
                arbs.append({
                    "buy_yes": {"venue": x.venue, "key": "", "title": x.title,
                                "ask_cents": x.yes_ask},
                    "buy_no": {"venue": y.venue, "key": "", "title": y.title,
                               "no_cost_cents": no_y},
                    "total_cost_cents": total, "edge_bps": edge_bps,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
    return sorted(arbs, key=lambda z: -z["edge_bps"])[:25]


def kalshi_fee(cents):
    if cents <= 0 or cents >= 100:
        return 0
    return max(1, math.ceil(0.07 * (cents / 100) * (1 - cents / 100) * 100))


def pmus_fee_cents(cents, coefficient=0.06):
    """PM-US charges feeCoefficient-style quadratic fees (observed 0.06)."""
    if cents <= 0 or cents >= 100:
        return 0
    return max(1, math.ceil(coefficient * (cents / 100) * (1 - cents / 100) * 100))


# ── PM-US Ed25519 auth (proven client rule: sign PATH ONLY, query rides free)
def pmus_auth_headers(path_no_query):
    key_id = os.environ.get("POLYMARKET_US_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_US_SECRET_KEY", "").strip()
    if not key_id or not secret:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        ts = str(int(time.time() * 1000))
        seed = base64.b64decode(secret)[:32]
        sk = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        sig = base64.b64encode(sk.sign(f"{ts}GET{path_no_query}".encode())).decode()
        return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": sig}
    except Exception as e:
        print(f"[pmus-auth] {str(e)[:90]}", flush=True)
        STATS["pmus.auth_fail"] += 1
        return None


def rest_pmus_exchange_sweep():
    """REAL Polymarket US exchange board — authenticated, paginated (500/page).
    NOTE: server ignores status= param; closed=false is the live-board filter."""
    hdrs = pmus_auth_headers("/v1/markets")
    if not hdrs:
        STATS["pmus_x.sweeps_skipped"] += 1
        return None
    h = dict(UA)
    h.update(hdrs)
    total = 0
    offset = 0
    while offset < 8000 and time.time() < DEADLINE:
        url = f"{PMUS_API}/v1/markets?closed=false&limit=500&offset={offset}"
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        ms = d.get("markets", [])
        for m in ms:
            if m.get("closed") or m.get("status") == "MARKET_STATUS_RESOLVED":
                continue
            q = m.get("question", "").strip()
            mid = str(m.get("id", ""))
            if not q or not mid:
                continue
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                yes = float(prices[0]) * 100
            except Exception:
                continue
            if not (0 < yes < 100):
                continue
            if put_book("pmus", mid, q, int(round(yes)), int(round(yes)), proxy="xchg"):
                STATS["pmus.rest_updates"] += 1
            total += 1
        if len(ms) < 500:
            break
        offset += 500
    STATS["pmus_x.sweeps"] += 1
    STATS["pmus_x.last_count"] = total
    LAST_TICK_TS["pmus"] = time.time()
    return total


# ───────────────────── REST fetchers ─────────────────────
def http_json(url, headers=None, timeout=20):
    h = {"User-Agent": "abet-desk/1.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def rest_kalshi_sweep():
    url = f"{KALSHI_API}/markets?status=open&limit=1000"
    d = http_json(url)
    fresh = 0
    for m in d.get("markets", []):
        tk = m.get("ticker", "")
        if "SHARD" in tk.upper() or "MVE" in tk.upper():
            continue
        try:
            bid = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
            ask = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
        except (TypeError, ValueError):
            continue
        t = m.get("title", "").strip()
        if t and put_book("kalshi", tk, t, bid, ask):
            STATS["kalshi.rest_updates"] += 1
        fresh += 1
    return fresh


def rest_pmus_sweep():
    d = http_json(f"{PM_GAMMA}/markets?active=true&closed=false&limit=400&order=volume24hr&ascending=false")
    fresh = 0
    for m in d if isinstance(d, list) else []:
        q = m.get("question", "").strip()
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            yes = float(prices[0]) * 100
        except Exception:
            continue
        cid = m.get("conditionId", "")
        if q and cid and put_book("pmglobal", cid, q, int(round(yes)), int(round(yes)), proxy="last"):
            STATS["pmglobal.rest_updates"] += 1
        # register clob token ids so the WS feed can route events back here
        try:
            toks = json.loads(m.get("clobTokenIds", "[]"))
            if isinstance(toks, list):
                for t in toks[:1]:  # yes-leg token
                    if t:
                        ASSET2KEY[t] = cid
        except Exception:
            pass
        fresh += 1
    return fresh


ASSET2KEY: dict[str, str] = {}


def rest_px_sweep():
    jwt = os.environ.get("PROPHETX_JWT", "").strip()
    if not jwt:
        return None
    d = http_json(f"{PX_API}/trade/private/api/v2/games",
                  headers={"Authorization": f"Bearer {jwt}"})
    items = d if isinstance(d, list) else d.get("games", []) if isinstance(d, dict) else []
    n = 0
    def amer_to_cents(o):
        o = float(o)
        p = 100.0/(o+100) if o > 0 else (-o)/((-o)+100)
        c = int(round(p*100))
        return c if 0 < c < 100 else None
    def walk(obj, gt):
        nonlocal n
        if isinstance(obj, dict):
            odds = obj.get("odds") or obj.get("americanOdds")
            lid = obj.get("lineID") or obj.get("lineId") or obj.get("id")
            side = str(obj.get("side") or obj.get("type") or "").lower()
            label = obj.get("name") or obj.get("selection") or ""
            if odds is not None and lid is not None:
                c = amer_to_cents(odds)
                if c:
                    title = f"{gt} | {label}".strip(" |")[:180]
                    inv = 100 - c
                    bid, ask = (c, c) if side in ("over","yes","long") else (inv, inv)
                    if put_book("prophetx", str(lid), title, bid, ask, proxy="p2p"):
                        STATS["px.rest_updates"] += 1
                    n += 1
            for v in obj.values():
                walk(v, gt)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, gt)
    for g in items:
        walk(g, str(g.get("name") or g.get("description") or ""))
    return n


def rest_novig_sweep():
    cid, sec = os.environ.get("NOVIG_CLIENT_ID","").strip(), os.environ.get("NOVIG_CLIENT_SECRET","").strip()
    if not (cid and sec):
        return None
    body = urllib.parse.urlencode({"grant_type":"client_credentials","client_id":cid,"client_secret":sec}).encode()
    req = urllib.request.Request(f"{NOVIG_API}/oauth/token", data=body,
                                 headers={"Content-Type":"application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read().decode()).get("access_token")
    d = http_json(f"{NOVIG_API}/nbx/v2/markets?limit=400",
                  headers={"Authorization": f"Bearer {tok}"})
    items = d if isinstance(d, list) else d.get("markets", []) if isinstance(d, dict) else []
    n = 0
    for m in items:
        title = m.get("name") or m.get("question") or ""
        ya = m.get("best_yes_ask"); nb = m.get("best_no_ask")
        try:
            ya_c = int(round(float(ya.get("price"))*100)) if isinstance(ya, dict) and ya.get("price") is not None else None
            nb_c = int(round(float(nb.get("price"))*100)) if isinstance(nb, dict) and nb.get("price") is not None else None
        except (TypeError, ValueError):
            continue
        if title and ya_c:
            bid = (100 - nb_c) if nb_c else max(1, ya_c-1)
            if put_book("novig", str(m.get("id","")), str(title)[:180], bid, ya_c):
                STATS["novig.rest_updates"] += 1
            n += 1
    return n


VENUE_SWEEPS = [("kalshi", rest_kalshi_sweep), ("pmus", rest_pmus_exchange_sweep),
                ("pmglobal", rest_pmus_sweep), ("prophetx", rest_px_sweep),
                ("novig", rest_novig_sweep)]


# ───────────────────── WebSocket feeds ─────────────────────
# ───────────────────── Kalshi WS auth ─────────────────────
def kalshi_ws_headers():
    """RSA-PSS signature over str(ts)+'GET'+'/trade-api/ws/v2' (proven rule)."""
    try:
        import base64 as b64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "").strip()
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id or not (pem or path):
            print("[kalshi-ws-auth] missing creds", flush=True)
            return None
        if pem:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
        else:
            with open(path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
        ts = int(time.time() * 1000)
        msg = str(ts) + "GET" + "/trade-api/ws/v2"
        sig = key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-SIGNATURE": b64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": str(ts)}
    except Exception as e:
        print(f"[kalshi-ws-auth] FAILED: {str(e)[:120]}", flush=True)
        STATS["kalshi.ws_auth_fail"] += 1
        return None


async def ws_kalshi():
    """Kalshi v2 WS: signed handshake, public ticker+trade firehose (all markets)."""
    while time.time() < DEADLINE:
        try:
            hdrs = kalshi_ws_headers()
        except Exception as e:
            print(f"[kalshi-ws-auth] crash: {str(e)[:100]}", flush=True)
            STATS["kalshi.ws_auth_fail"] += 1
            hdrs = None
        if not hdrs:
            STATS["kalshi.ws_noauth"] += 1
            await asyncio.sleep(30)
            continue
        try:
            async with websockets.connect(KALSHI_WS, additional_headers=hdrs,
                                          ping_interval=10, close_timeout=5) as ws:
                print("[kalshi-ws] connected (signed)", flush=True)
                # ticker+trade are PUBLIC channels; unfiltered = entire market
                sub = {"id": 1, "cmd": "subscribe",
                       "params": {"channels": ["ticker", "trade"]}}
                await ws.send(json.dumps(sub))
                while time.time() < DEADLINE:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    mt = msg.get("type")
                    if mt == "ticker":
                        m = msg.get("msg", {})
                        tk = m.get("market_ticker")
                        if tk:
                            try:
                                bid = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
                                ask = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
                            except (TypeError, ValueError):
                                continue
                            title = TITLES.get(("kalshi", tk), tk)
                            put_book("kalshi", tk, title, bid, ask)
                            STATS["kalshi.ws_ticks"] += 1
                    elif mt == "trade":
                        STATS["kalshi.ws_trades"] += 1
                        LAST_TICK_TS["kalshi"] = time.time()
                    elif mt == "error":
                        STATS["kalshi.ws_errors"] += 1
                        if STATS["kalshi.ws_errors"] <= 3:
                            print(f"[kalshi-ws] error msg: {raw[:160]}", flush=True)
                    elif mt == "subscribed":
                        print(f"[kalshi-ws] subscribed ok", flush=True)
            continue
        except asyncio.TimeoutError:
            continue
        except TypeError:
            # older websockets uses extra_headers
            try:
                async with websockets.connect(KALSHI_WS, extra_headers=hdrs,
                                              ping_interval=10, close_timeout=5) as ws:
                    await _kalshi_consume(ws)
            except Exception as e2:
                STATS["kalshi.ws_errors"] += 1
                print(f"[kalshi-ws] {str(e2)[:90]} — reconnect 5s", flush=True)
                await asyncio.sleep(5)
        except Exception as e:
            STATS["kalshi.ws_errors"] += 1
            print(f"[kalshi-ws] {str(e)[:90]} — reconnect 5s", flush=True)
            await asyncio.sleep(5)


async def _kalshi_consume(ws):
    while time.time() < DEADLINE:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        msg = json.loads(raw)
        if msg.get("type") == "orderbook_delta":
            for ob in msg.get("msgs", []):
                tk = ob.get("market_ticker")
                yes = [lv for lv in (ob.get("yes") or []) if lv and lv[1]]
                if tk and yes:
                    prices = [p for p, _q in yes if 0 < p < 100]
                    put_book("kalshi", tk, TITLES.get(("kalshi", tk), tk),
                             max(prices), min(prices))
            STATS["kalshi.ws_ticks"] += 1


async def ws_pmus_clob():
    """Polymarket CLOB market channel: subscribe with yes-leg clob token ids."""
    while time.time() < DEADLINE:
        try:
            tokens = list(ASSET2KEY)[:100]
            async with websockets.connect(PM_WS, ping_interval=10, close_timeout=5) as ws:
                if tokens:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    print(f"[pmus-ws] subscribed {len(tokens)} assets", flush=True)
                while time.time() < DEADLINE:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    STATS["pmglobal.ws_msgs"] += 1
                    try:
                        msgs = json.loads(raw)
                        if isinstance(msgs, dict):
                            msgs = [msgs]
                        for ev in msgs:
                            try:
                                aid = ev.get("asset_id")
                                cid = ASSET2KEY.get(aid)
                                if not cid:
                                    continue
                                et = ev.get("event_type")
                                cur = BOOKS.get(("pmglobal", cid))
                                bid = cur.yes_bid if cur else None
                                ask = cur.yes_ask if cur else None
                                if et == "book":
                                    def best(levels, hi):
                                        ps = []
                                        for lv in levels or []:
                                            try:
                                                pf = float(lv[0] if isinstance(lv, (list, tuple)) else lv.get("price")) * 100
                                                if 0 < pf < 100:
                                                    ps.append(pf)
                                            except (TypeError, ValueError, IndexError):
                                                continue
                                        return int(round(max(ps) if hi else min(ps))) if ps else None
                                    b2, a2 = best(ev.get("bids"), True), best(ev.get("asks"), False)
                                    bid, ask = (b2 if b2 is not None else bid), (a2 if a2 is not None else ask)
                                elif et == "price_change":
                                    changes = ev.get("changes") or ([ev] if ev.get("price") else [])
                                    for ch in changes:
                                        try:
                                            pf = int(round(float(ch.get("price")) * 100))
                                            if not (0 < pf < 100):
                                                continue
                                            if ch.get("side") == "BUY":
                                                ask = pf
                                            else:
                                                bid = pf
                                        except (TypeError, ValueError):
                                            continue
                                else:
                                    continue
                                title = TITLES.get(("pmglobal", cid), cid[:16])
                                put_book("pmglobal", cid, title, bid if bid else 1, ask if ask else 99)
                                STATS["pmglobal.ws_ticks"] += 1
                            except Exception:
                                STATS["pmglobal.ws_event_errors"] += 1
                                continue
                    except json.JSONDecodeError:
                        pass
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            STATS["pmglobal.ws_errors"] += 1
            print(f"[pmus-ws] {str(e)[:90]} — reconnect 5s", flush=True)
            await asyncio.sleep(5)


async def ws_prophetx():
    """ProphetX cash WS — captured endpoint; may gate data behind auth."""
    jwt = os.environ.get("PROPHETX_JWT", "").strip()
    while time.time() < DEADLINE:
        try:
            hdr = {"Authorization": f"Bearer {jwt}"} if jwt else {}
            try:
                conn = websockets.connect(PX_WS, additional_headers=hdr,
                                          ping_interval=10, close_timeout=5)
            except TypeError:
                conn = websockets.connect(PX_WS, extra_headers=hdr,
                                          ping_interval=10, close_timeout=5)
            async with conn as ws:
                print("[px-ws] connected", flush=True)
                while time.time() < DEADLINE:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    STATS["prophetx.ws_msgs"] += 1
                    LAST_TICK_TS["prophetx"] = time.time()
                    if STATS["prophetx.ws_msgs"] <= 3:
                        print(f"[px-ws sample] {raw[:200]}", flush=True)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            STATS["prophetx.ws_errors"] += 1
            print(f"[px-ws] {str(e)[:90]} — reconnect 15s", flush=True)
            await asyncio.sleep(15)


# ───────────────────── persistence ─────────────────────
def write_state(arbs):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    venues_status = []
    for v in ["kalshi", "pmus", "pmglobal", "prophetx", "novig"]:
        n = sum(1 for (vv, _k) in BOOKS if vv == v)
        lt = LAST_TICK_TS.get(v, 0)
        age = time.time() - lt if lt else None
        if n and (age is not None and age < 120):
            st = "OK"
        elif n:
            st = "STALE"
        else:
            jwt = bool(os.environ.get("PROPHETX_JWT")) if v == "prophetx" else \
                  bool(os.environ.get("NOVIG_CLIENT_ID")) if v == "novig" else True
            st = "OK_EMPTY" if v in ("kalshi", "pmus") else (
                 "SKIPPED_NO_CREDENTIALS" if not jwt else "OK_EMPTY")
        venues_status.append({"venue": v, "state": st, "markets": n,
                              "last_tick_age_s": round(age, 1) if age else None})
    snap = {
        "scanned_at": now_iso,
        "engine": "desk_daemon (persistent loop)",
        "loop_uptime_min": round((time.time() - (DEADLINE - RUN_MINUTES*60)) / 60, 1),
        "min_edge_bps": MIN_EDGE_BPS,
        "venues": venues_status,
        "totals": {"markets_tracked": len(BOOKS), "pairs_married": len(PAIRS_CACHE),
                   "arbitrages": len(arbs)},
        "stats": {k: v for k, v in sorted(STATS.items())},
        "arbitrages": arbs,
    }
    with open(os.path.join(RESULTS_DIR, "latest_scan.json"), "w") as f:
        json.dump(snap, f, indent=1)
    row = {"ts": now_iso, "mk": len(BOOKS), "pairs": len(PAIRS_CACHE), "arbs": len(arbs),
           "ticks": sum(v for k, v in STATS.items() if k.endswith(".ticks")),
           "v": {s["venue"]: f'{s["state"]}:{s["markets"]}' for s in venues_status}}
    hp = os.path.join(RESULTS_DIR, "history.jsonl")
    lines = []
    if os.path.exists(hp):
        with open(hp) as f:
            lines = f.readlines()
    lines.append(json.dumps(row) + "\n")
    with open(hp, "w") as f:
        f.writelines(lines[-4000:])
    return snap


async def persistence_loop():
    last_commit = time.time()
    while time.time() < DEADLINE:
        arbs = evaluate_all()
        snap = write_state(arbs)
        if arbs and os.environ.get("DISCORD_WEBHOOK_ABET"):
            try:
                a = arbs[0]
                payload = json.dumps({"content":
                    f"\u0023\ufe0f\u20e3 ARB {a['edge_bps']}bps \u2014 "
                    f"YES {a['buy_yes']['venue']} @{a['buy_yes']['ask_cents']}\u00a2 + "
                    f"NO {a['buy_no']['venue']} @{a['buy_no']['no_cost_cents']}\u00a2"}).encode()
                rq = urllib.request.Request(os.environ["DISCORD_WEBHOOK_ABET"],
                                            data=payload,
                                            headers={"Content-Type": "application/json"})
                urllib.request.urlopen(rq, timeout=8)
            except Exception:
                pass
        if time.time() - last_commit >= COMMIT_EVERY_S:
            last_commit = time.time()
            try:
                import subprocess
                subprocess.run(["git", "add", "results/"], check=False)
                r = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
                if r.returncode != 0:
                    subprocess.run(["git", "-c", "user.name=abet-desk-bot",
                                    "-c", "user.email=abet-desk-bot@users.noreply.github.com",
                                    "commit", "-m",
                                    f'desk {datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")} '
                                    f'mk={len(BOOKS)} pairs={len(PAIRS_CACHE)} arbs={len(arbs)}'],
                                   check=False)
                    subprocess.run(["git", "pull", "--rebase", "-q"], check=False, timeout=90)
                    p = subprocess.run(["git", "push"], check=False, timeout=90,
                                       capture_output=True, text=True)
                    if p.returncode == 0:
                        STATS["commits"] += 1
                    elif STATS.get("push_fails", 0) < 2:
                        STATS["push_fails"] += 1
            except Exception as e:
                print(f"[git] {str(e)[:80]}", flush=True)
        await asyncio.sleep(2)


async def sweep_loop():
    idx = 0
    while time.time() < DEADLINE:
        name, fn = VENUE_SWEEPS[idx % len(VENUE_SWEEPS)]
        try:
            r = fn()
            if r is None:
                STATS[f"{name}.sweeps_skipped"] += 1
            else:
                STATS[f"{name}.sweeps"] += 1
        except Exception as e:
            STATS[f"{name}.sweep_errors"] += 1
            if STATS[f"{name}.sweep_errors"] % 25 == 1:
                print(f"[{name}-rest] {str(e)[:90]}", flush=True)
        idx += 1
        # rebuild marriage cache periodically (cheap relative to sweeps)
        if idx % 4 == 0:
            rebuild_pairs()
        await asyncio.sleep(SWEEP_EVERY_S)


async def main():
    t0 = datetime.now(timezone.utc)
    print(f"[desk-daemon] started {t0.isoformat()} deadline={RUN_MINUTES}min", flush=True)
    for _, fn in VENUE_SWEEPS:
        with contextlib.suppress(Exception):
            fn()
    rebuild_pairs()
    tasks = [
        asyncio.create_task(sweep_loop()),
        asyncio.create_task(persistence_loop()),
        asyncio.create_task(ws_kalshi()),
        asyncio.create_task(ws_pmus_clob()),
        asyncio.create_task(ws_prophetx()),
    ]
    # watchdog: clean exit at deadline so Actions uploads state
    while time.time() < DEADLINE:
        await asyncio.sleep(5)
    print("[desk-daemon] deadline reached — shutting down", flush=True)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
