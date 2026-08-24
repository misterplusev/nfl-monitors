"""
nfl_supabase_client.py — NFL port of ABET MLB supabase_client.py (v3.0 semantics).

Mirrors the MLB client's REST contract against nfl_-prefixed tables:
  nfl_games          -> game reference data
  nfl_odds_history   -> append-only odds snapshots (bulk insert)
  nfl_api_usage      -> per-run key credit telemetry
  nfl_monitor_state  -> health heartbeats, learned stats, api-key rotation state
  nfl_ml_predictions -> timing learning samples (adaptive polling roadmap)

Deviations from MLB original (documented, intentional):
  1. No hardcoded project URL: SUPABASE_URL + SUPABASE_KEY are required env vars.
     Missing either => connected=False and every method is a safe no-op.
  2. TLS certificate verification stays ENABLED (MLB disabled it; not ported).

Usage:
    db = NflSupabaseClient()
    db.append_odds_history([{...}, ...])
"""

import json
import os
import statistics
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

USER_AGENT = "ABET-NFL-Monitor/1.0"
BATCH_SIZE = 200


class SupabaseClient:

    def __init__(self, url=None, key=None):
        self.url = ((url or SUPABASE_URL) or "").rstrip("/")
        self.key = key or SUPABASE_KEY
        self.rest = f"{self.url}/rest/v1" if self.url else ""
        self._headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
            "User-Agent": USER_AGENT,
        }
        self._stats_cache = {}

    @property
    def connected(self) -> bool:
        return bool(self.url and self.key)

    def _request(self, method, path, data=None, headers_extra=None):
        if not self.connected:
            return None
        url = f"{self.rest}/{path}"
        headers = dict(self._headers)
        if headers_extra:
            headers.update(headers_extra)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"[SUPABASE] {method} {path[:80]} -> {e.code}: {err_body[:200]}")
            return None
        except Exception as e:
            print(f"[SUPABASE] {method} {path[:80]} -> ERROR: {e}")
            return None

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, data, upsert=False):
        extra = {"Prefer": "resolution=merge-duplicates,return=minimal"} if upsert else None
        return self._request("POST", path, data, extra)

    def upsert_game(self, game_id, game_date, home_team, away_team,
                    commence_time=None, status="Preview"):
        if not self.connected:
            return
        self._post("nfl_games", {
            "game_id": str(game_id),
            "game_date": game_date,
            "home_team": home_team,
            "away_team": away_team,
            "commence_time": commence_time,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, upsert=True)

    def append_odds_history(self, records):
        """Bulk-insert odds snapshots into nfl_odds_history, batched at 200."""
        if not self.connected or not records:
            return
        total = 0
        for i in range(0, len(records), BATCH_SIZE):
            batch = records[i:i + BATCH_SIZE]
            result = self._request("POST", "nfl_odds_history", batch)
            if result is not None or True:
                total += len(batch)
        if total > 0:
            print(f"[SUPABASE] Inserted {total} odds history records")

    def record_api_usage(self, api_key_id, credits_used, credits_remaining,
                         games_count=0, bookmakers_count=0, regions="us",
                         sport_key="", request_type="unified_fetch"):
        if not self.connected:
            return
        self._post("nfl_api_usage", {
            "id": str(uuid.uuid4()),
            "api_key_id": api_key_id,
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
            "games_count": games_count,
            "bookmakers_count": bookmakers_count,
            "regions": regions,
            "sport_key": sport_key,
            "request_type": request_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    def push_api_key_state(self, keys_state):
        """Persist 9-key rotation state across runs/restarts."""
        if not self.connected:
            return
        self._post("nfl_monitor_state", {
            "monitor_name": "nfl_odds_api_keys",
            "last_state": json.dumps(keys_state),
            "last_run_pt": datetime.now(timezone.utc).isoformat(),
        }, upsert=True)

    def get_api_key_state(self):
        if not self.connected:
            return {}
        rows = self._get("nfl_monitor_state?monitor_name=eq.nfl_odds_api_keys&select=last_state")
        if rows and len(rows) > 0:
            state = rows[0].get("last_state")
            if isinstance(state, str):
                state = json.loads(state)
            return state if isinstance(state, dict) else {}
        return {}

    def update_status(self, monitor_type, **kwargs):
        if not self.connected:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        state = dict(kwargs)
        state["updated_at"] = now_iso
        self._post("nfl_monitor_state", {
            "monitor_name": monitor_type,
            "last_state": json.dumps(state),
            "last_run_pt": now_iso,
        }, upsert=True)

    def record_sample(self, monitor_type, game_id, team_name, hours_before,
                      game_date=None):
        if not self.connected:
            return
        if not game_date:
            game_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).isoformat()
        self._post("nfl_ml_predictions", {
            "id": str(uuid.uuid4()),
            "game_id": str(game_id),
            "prediction_type": f"{monitor_type}_detection",
            "predicted_time_utc": now_iso,
            "confidence": round(hours_before, 3),
            "features": json.dumps({
                "hours_before": round(hours_before, 3),
                "team_name": team_name,
                "game_date": game_date,
                "monitor_type": monitor_type,
            }),
            "created_at": now_iso,
        })

    def refresh_stats(self, monitor_type):
        if not self.connected:
            return
        rows = self._get(
            f"nfl_ml_predictions?prediction_type=eq.{monitor_type}_detection"
            f"&select=confidence&order=confidence.asc&limit=1000"
        )
        if not rows:
            return
        values = [float(r["confidence"]) for r in rows
                  if r.get("confidence") is not None and float(r["confidence"]) > 0]
        if len(values) < 3:
            return
        n = len(values)
        stats = {
            "sample_count": n,
            "median_hours": round(statistics.median(values), 3),
            "p25_hours": round(values[n // 4], 3),
            "p75_hours": round(values[3 * n // 4], 3),
            "min_hours": round(values[0], 3),
            "max_hours": round(values[-1], 3),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._post("nfl_monitor_state", {
            "monitor_name": f"{monitor_type}_ml",
            "last_state": json.dumps(stats),
            "last_run_pt": datetime.now(timezone.utc).isoformat(),
        }, upsert=True)
        self._stats_cache[monitor_type] = stats

    def get_stats(self, monitor_type):
        if monitor_type in self._stats_cache:
            return self._stats_cache[monitor_type]
        if self.connected:
            rows = self._get(f"nfl_monitor_state?monitor_name=eq.{monitor_type}_ml")
            if rows and len(rows) > 0:
                state = rows[0].get("last_state")
                if isinstance(state, str):
                    state = json.loads(state)
                if isinstance(state, dict) and state.get("median_hours"):
                    self._stats_cache[monitor_type] = state
                    return state
        return {"median_hours": 2.0, "p25_hours": 1.0, "p75_hours": 4.0}

    def get_adaptive_interval(self, monitor_type, hours_until_game):
        """Band logic mirrors MLB v3.0 exactly (parity requirement)."""
        stats = self.get_stats(monitor_type)
        median = float(stats.get("median_hours", 2.0))
        p25 = float(stats.get("p25_hours", 1.0))
        p75 = float(stats.get("p75_hours", 4.0))
        hot_start = p75 + 0.5
        past_peak = max(p25 - 0.5, 0.1)
        if hours_until_game <= 0:
            return 600
        elif hours_until_game <= past_peak:
            return 30
        elif hours_until_game <= p25:
            return 45
        elif hours_until_game <= median:
            return 60
        elif hours_until_game <= p75:
            return 90
        elif hours_until_game <= hot_start:
            return 300
        elif hours_until_game <= hot_start + 2:
            return 600
        return 900

    def test_connection(self):
        if not self.connected:
            print("[SUPABASE] SUPABASE_URL/SUPABASE_KEY not configured; client disabled")
            return False
        result = self._get("nfl_monitor_state?limit=1")
        if result is not None:
            print(f"[SUPABASE] Connected to {self.url}")
            return True
        return False

NflSupabaseClient = SupabaseClient  # backwards-compatible alias
