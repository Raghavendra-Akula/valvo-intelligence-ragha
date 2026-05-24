"""
backfill_etf_shares.py — Populates stock_universe.nav for every active ETF
using NSE's official ETF endpoint.

Background:
- NSE exposes per-unit NAV via /api/etf in a single request for the whole
  ETF universe (~320 funds).
- That endpoint also returns each fund's underlying index/asset and
  daily traded volume, but NOT AUM or units outstanding (we proved
  this empirically — `qty` for UTI Nifty 50 ETF was 35,114, which at
  NAV ₹266 implies ₹0.93 Cr — its real AUM is ₹68,858 Cr, so qty is
  daily volume).
- Yahoo Finance is also useless for Indian ETFs — it classifies them
  as quoteType=EQUITY and doesn't populate fund-specific fields.
- For real AUM, AMFI's quarterly scheme summary is the canonical source;
  that's a separate pipeline.

What this script writes today:
- nav                — per-unit NAV (rupees)
- nav_refreshed_at   — timestamp of the fetch

Usage (run from the Backend folder):
    pip install requests psycopg2-binary python-dotenv     # one-time
    python scripts/backfill_etf_shares.py                  # all ETFs (~5s)
    python scripts/backfill_etf_shares.py NIFTYBEES        # one ETF (testing)

Reads Backend/.env for DB credentials (DB_HOST, DB_USER, DB_PASSWORD).
Idempotent — re-run any time. Wire to a daily cron if you want NAV fresh.
"""

import os
import sys
import time

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import requests


load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # No 'br' — avoids needing the optional brotli package.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/market-data/exchange-traded-funds-etf",
    "Connection": "keep-alive",
}


def fetch_nse_etfs() -> list[dict]:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get("https://www.nseindia.com/", timeout=15)
    time.sleep(0.7)
    s.get(
        "https://www.nseindia.com/market-data/exchange-traded-funds-etf",
        timeout=15,
    )
    time.sleep(0.7)
    r = s.get("https://www.nseindia.com/api/etf", timeout=20)
    if r.status_code != 200 or "json" not in (r.headers.get("content-type") or "").lower():
        snippet = (r.text or "")[:500]
        raise RuntimeError(
            f"NSE returned non-JSON. status={r.status_code} "
            f"content-type={r.headers.get('content-type')!r}\n"
            f"--- body ---\n{snippet}\n---"
        )
    try:
        return (r.json().get("data") or [])
    except ValueError:
        raise RuntimeError(
            f"NSE returned a JSON content-type but the body wouldn't parse. "
            f"encoding={r.headers.get('content-encoding')!r}, "
            f"first 32 bytes={r.content[:32]!r}"
        )


def parse_nav(row: dict) -> float | None:
    raw = row.get("nav")
    if raw in (None, "", "-"):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def main() -> None:
    one_symbol = sys.argv[1].upper().strip() if len(sys.argv) > 1 else None

    print("Fetching ETF list from NSE…")
    etfs = fetch_nse_etfs()
    print(f"  Got {len(etfs)} ETFs from NSE")

    by_symbol: dict[str, dict] = {}
    for row in etfs:
        sym = (row.get("symbol") or "").upper().strip()
        if sym:
            by_symbol[sym] = row

    if one_symbol and one_symbol in by_symbol:
        sample = by_symbol[one_symbol]
        print(f"\n[debug] {one_symbol} from NSE: nav={sample.get('nav')}, "
              f"ltP={sample.get('ltP')}, underlying={sample.get('assets')!r}\n")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), database="postgres",
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=6543, sslmode="require", cursor_factory=RealDictCursor,
    )
    cur = conn.cursor()

    if one_symbol:
        cur.execute(
            """SELECT security_id, symbol FROM stock_universe
               WHERE is_etf = TRUE AND is_active = TRUE AND UPPER(symbol) = %s""",
            (one_symbol,),
        )
    else:
        cur.execute(
            """SELECT security_id, symbol FROM stock_universe
               WHERE is_etf = TRUE AND is_active = TRUE
               ORDER BY symbol"""
        )
    rows = cur.fetchall()
    total = len(rows)
    if total == 0:
        print(f"No ETF found{' for ' + one_symbol if one_symbol else ''} in stock_universe.")
        return

    print(f"Updating {total} ETF{'s' if total != 1 else ''}…")
    updated, missing, no_nav = 0, 0, 0
    for r in rows:
        sym = r["symbol"]
        nse_row = by_symbol.get(sym.upper())
        if not nse_row:
            missing += 1
            continue
        nav = parse_nav(nse_row)
        if nav is None or nav <= 0:
            no_nav += 1
            continue
        cur.execute(
            """UPDATE stock_universe
                  SET nav              = %s,
                      nav_refreshed_at = NOW()
                WHERE security_id = %s""",
            (nav, r["security_id"]),
        )
        conn.commit()
        updated += 1
        if one_symbol:
            print(f"  {sym}: nav={nav}")

    conn.close()
    print(
        f"\nDone: {updated} updated, {missing} not on NSE list, "
        f"{no_nav} on list but no NAV, of {total} total"
    )


if __name__ == "__main__":
    main()
