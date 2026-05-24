#!/usr/bin/env python3
"""
backfill_pct_above_ema10.py — one-shot backfill for breadth_daily_history.pct_above_ema10.

Mirrors the EMA10 recurrence from build_breadth_history.py / seed_ema10.py
to compute % of non-ETF NSE stocks trading above their daily EMA(10) for every
historical trading date in breadth_daily_history, then UPDATE-by-date.

Forward-only would discard the multi-year history the Index Breadth Dashboard
needs to render, so we iterate candles_daily from FETCH_START.

Usage:  cd Backend && python scripts/backfill_pct_above_ema10.py
"""
import os, sys, time
from datetime import datetime, timedelta, timezone, date as date_type
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))
K10 = 2.0 / 11
FETCH_START = "2003-01-01"
DATA_START = "2003-09-05"


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("=== Backfill breadth_daily_history.pct_above_ema10 ===")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '600000'")  # 10 min

        log(f"Fetching candles_daily from {FETCH_START} (non-ETF only)...")
        cur.execute("""
            SELECT cd.security_id, cd.date, cd.close
            FROM candles_daily cd
            JOIN stock_universe su ON cd.security_id = su.security_id
            WHERE cd.date >= %s
              AND cd.close > 0
              AND COALESCE(su.is_etf, false) = false
            ORDER BY cd.security_id, cd.date
        """, (FETCH_START,))
        rows = cur.fetchall()
        log(f"Fetched {len(rows):,} candle rows")
        if not rows:
            log("No data; aborting.")
            return

        stocks = defaultdict(list)
        for r in rows:
            stocks[r["security_id"]].append({
                "date": r["date"],
                "close": float(r["close"]),
            })
        log(f"Organised {len(stocks)} stocks")

        data_start = date_type.fromisoformat(DATA_START)

        # Per-date list of (close, ema10) pairs from DATA_START onward
        per_date = defaultdict(list)
        skipped_short = 0

        for sid, series in stocks.items():
            if len(series) < 50:
                skipped_short += 1
                continue
            ema10 = series[0]["close"]
            for c in series:
                cl = c["close"]
                ema10 = cl * K10 + ema10 * (1 - K10)
                if c["date"] >= data_start:
                    per_date[c["date"]].append((cl, ema10))

        log(f"Built per-date EMA10 series (skipped {skipped_short} short stocks). "
            f"{len(per_date)} dates")

        # Compute pct_above_ema10 per date
        updates = []
        for d, pairs in sorted(per_date.items()):
            total = len(pairs)
            if total < 100:
                continue
            above = sum(1 for cl, e in pairs if cl > e)
            pct = round(100.0 * above / total, 1)
            updates.append((d, pct))

        log(f"Computed {len(updates)} date rows")

        # Bulk UPDATE — one statement per chunk
        cur.execute("SET LOCAL statement_timeout = '120000'")
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            """
            UPDATE breadth_daily_history AS b
            SET pct_above_ema10 = v.pct
            FROM (VALUES %s) AS v(d, pct)
            WHERE b.date = v.d
            """,
            updates,
            template="(%s::date, %s)",
            page_size=500,
        )
        conn.commit()
        log(f"✅ Updated breadth_daily_history.pct_above_ema10 for {len(updates)} dates")

        # Sanity-check the tail
        cur.execute("""
            SELECT date, pct_above_ema10, pct_above_ema20, pct_above_ema50
            FROM breadth_daily_history
            WHERE pct_above_ema10 IS NOT NULL
            ORDER BY date DESC
            LIMIT 5
        """)
        for r in cur.fetchall():
            log(f"  {r['date']}: ema10={r['pct_above_ema10']}  "
                f"ema20={r['pct_above_ema20']}  ema50={r['pct_above_ema50']}")
    finally:
        close_db(conn)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"Done in {time.time() - t0:.1f}s")
