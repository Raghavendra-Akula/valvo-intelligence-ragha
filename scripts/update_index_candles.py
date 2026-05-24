#!/usr/bin/env python3
"""
update_index_candles.py — Daily index OHLCV updater for candles_indices

Runs on the VM as a cron job after market close (4:30 PM IST).
Fetches missing daily candles for all indices from historical API
and pushes to Supabase candles_indices table.

Setup on VM:
  1. Place this file alongside the websocket script
  2. pip install requests
  3. Set env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY, DHAN_ACCESS_TOKEN
  4. Add cron: 30 16 * * 1-5 /usr/bin/python3 /path/to/update_index_candles.py >> /var/log/index_update.log 2>&1

Or run manually: python3 update_index_candles.py
"""
import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone

# ═══════ CONFIG ═══════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://sxyktzpiixmidlxxfgdd.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")  # service_role key
API_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "1110741667")

HISTORICAL_API_URL = "https://api.dhan.co/v2/charts/historical"
IST = timezone(timedelta(hours=5, minutes=30))

# Rate limiting
API_DELAY = 0.35  # seconds between API calls (stay under rate limit)


def log(msg):
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{ts}] {msg}", flush=True)


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert
    }


def api_headers():
    return {
        "access-token": API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_all_indices():
    """Fetch distinct (symbol, security_id) pairs from candles_indices."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/get_index_symbols"
    # Fallback: direct query
    url = f"{SUPABASE_URL}/rest/v1/candles_indices?select=symbol,security_id&order=symbol"
    headers = supabase_headers()
    # We need distinct — use a workaround with limit
    # Actually, let's just fetch and deduplicate in Python
    # Fetch a small sample per symbol by using the latest date
    url = f"{SUPABASE_URL}/rest/v1/candles_indices?select=symbol,security_id&order=symbol.asc,date.desc"
    resp = requests.get(url, headers=headers, params={"limit": 10000})
    if resp.status_code != 200:
        log(f"ERROR fetching indices: {resp.status_code} {resp.text[:200]}")
        return []

    rows = resp.json()
    # Deduplicate
    seen = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in seen:
            seen[sym] = r["security_id"]

    result = [{"symbol": sym, "security_id": sid} for sym, sid in seen.items()]
    log(f"Found {len(result)} unique indices")
    return result


def get_latest_date(symbol):
    """Get the most recent date for a symbol in candles_indices."""
    url = f"{SUPABASE_URL}/rest/v1/candles_indices"
    params = {
        "select": "date",
        "symbol": f"eq.{symbol}",
        "order": "date.desc",
        "limit": 1,
    }
    resp = requests.get(url, headers=supabase_headers(), params=params)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["date"]
    return None


def fetch_historical(security_id, from_date, to_date):
    """Fetch daily OHLCV for an index."""
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "expiryCode": 0,
        "fromDate": from_date,
        "toDate": to_date,
    }
    try:
        resp = requests.post(
            HISTORICAL_API_URL,
            headers=api_headers(),
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        # API returns: {open:[...], high:[...], low:[...], close:[...], volume:[...], timestamp:[...]}
        if not data or "open" not in data:
            return None

        candles = []
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])
        timestamps = data.get("timestamp", [])

        for i in range(len(timestamps)):
            # Timestamp is epoch seconds or date string
            ts = timestamps[i]
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, tz=IST)
            else:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

            date_str = dt.strftime("%Y-%m-%d")

            candles.append({
                "date": date_str,
                "open": float(opens[i]) if i < len(opens) else 0,
                "high": float(highs[i]) if i < len(highs) else 0,
                "low": float(lows[i]) if i < len(lows) else 0,
                "close": float(closes[i]) if i < len(closes) else 0,
                "volume": int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
            })

        return candles

    except Exception as e:
        log(f"  Historical fetch error: {e}")
        return None


def upsert_candles(symbol, security_id, candles):
    """Upsert candles into candles_indices via Supabase REST API."""
    if not candles:
        return 0

    rows = []
    now = datetime.now(IST).isoformat()
    for c in candles:
        rows.append({
            "symbol": symbol,
            "security_id": str(security_id),
            "date": c["date"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c["volume"],
            "updated_at": now,
        })

    # Upsert in batches of 50
    inserted = 0
    for i in range(0, len(rows), 50):
        batch = rows[i:i+50]
        url = f"{SUPABASE_URL}/rest/v1/candles_indices"
        headers = supabase_headers()
        # Need unique constraint for upsert — on (symbol, date)
        headers["Prefer"] = "resolution=merge-duplicates"
        resp = requests.post(url, headers=headers, json=batch)
        if resp.status_code in (200, 201):
            inserted += len(batch)
        else:
            log(f"  Upsert error: {resp.status_code} {resp.text[:200]}")

    return inserted


def run():
    """Main update loop."""
    log("=" * 60)
    log("INDEX CANDLE UPDATER — Starting")
    log("=" * 60)

    if not SUPABASE_KEY:
        log("ERROR: SUPABASE_SERVICE_KEY not set")
        sys.exit(1)
    if not API_TOKEN:
        log("ERROR: DHAN_ACCESS_TOKEN not set")
        sys.exit(1)

    # Get today's date in IST
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    log(f"Today: {today_str}")

    # Get all indices
    indices = get_all_indices()
    if not indices:
        log("No indices found — exiting")
        return

    total_inserted = 0
    skipped = 0
    errors = 0
    updated = 0

    for idx in indices:
        sym = idx["symbol"]
        sid = idx["security_id"]

        # Get latest date for this symbol
        latest = get_latest_date(sym)

        if latest and latest >= today_str:
            skipped += 1
            continue  # Already up to date

        # Calculate from_date (day after latest, or 5 days back if no data)
        if latest:
            from_dt = datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)
        else:
            from_dt = now_ist - timedelta(days=5)

        from_str = from_dt.strftime("%Y-%m-%d")

        # Skip if from_date is in the future
        if from_str > today_str:
            skipped += 1
            continue

        # Fetch historical candles
        candles = fetch_historical(sid, from_str, today_str)
        time.sleep(API_DELAY)  # Rate limit

        if candles is None:
            log(f"  SKIP {sym} (sid={sid}) — API returned no data for {from_str} to {today_str}")
            errors += 1
            continue

        if not candles:
            skipped += 1
            continue

        # Upsert
        count = upsert_candles(sym, sid, candles)
        if count > 0:
            log(f"  ✓ {sym}: +{count} candles ({from_str} → {today_str})")
            total_inserted += count
            updated += 1

    log("")
    log(f"DONE — Updated: {updated} | Skipped (fresh): {skipped} | Errors: {errors} | Total candles inserted: {total_inserted}")
    log("=" * 60)


if __name__ == "__main__":
    run()
