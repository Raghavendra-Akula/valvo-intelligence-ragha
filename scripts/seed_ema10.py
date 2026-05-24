#!/usr/bin/env python3
"""
seed_ema10.py — one-shot seed for stock_daily_summary.ema10.

Iterates candles_daily from FETCH_START with the 10-period EMA recurrence
(K10 = 2/11), then writes the final EMA10 per security to
stock_daily_summary.ema10. After this runs once, the daily 16:30 IST pg_cron
in summary_functions.sql STEP 6 keeps ema10 in step with ema20/50/200.

Usage:  cd Backend && python scripts/seed_ema10.py
"""
import os, sys, time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))
K10 = 2.0 / 11        # 0.181818
WARMUP_DAYS = 250     # ~12 months for EMA10 to fully converge
FETCH_START = "2019-01-01"


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("=== Seed stock_daily_summary.ema10 ===")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '300000'")  # 5 min

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

        # Organise per-stock time series
        stocks = defaultdict(list)
        for r in rows:
            stocks[r["security_id"]].append(float(r["close"]))
        log(f"Organised {len(stocks)} stocks")

        final_ema10 = {}
        skipped_short = 0
        for sid, closes in stocks.items():
            if len(closes) < 50:
                skipped_short += 1
                continue
            ema10 = closes[0]
            for cl in closes:
                ema10 = cl * K10 + ema10 * (1 - K10)
            final_ema10[sid] = ema10

        log(f"Computed EMA10 for {len(final_ema10)} stocks "
            f"(skipped {skipped_short} with <50 days)")

        # Bulk upsert via execute_values for speed
        from psycopg2.extras import execute_values
        pairs = [(sid, ema) for sid, ema in final_ema10.items()]

        cur.execute("SET LOCAL statement_timeout = '60000'")
        execute_values(
            cur,
            """
            UPDATE stock_daily_summary AS s
            SET ema10 = v.ema10
            FROM (VALUES %s) AS v(security_id, ema10)
            WHERE s.security_id = v.security_id
            """,
            pairs,
            template="(%s, %s)",
            page_size=1000,
        )
        conn.commit()
        log(f"✅ Updated stock_daily_summary.ema10 for {len(pairs)} stocks")

        # Sanity-check: a few sample rows
        cur.execute("""
            SELECT security_id, ema10, ema20, prev_close
            FROM stock_daily_summary
            WHERE ema10 IS NOT NULL AND ema20 IS NOT NULL
            ORDER BY security_id
            LIMIT 5
        """)
        for r in cur.fetchall():
            log(f"  sid={r['security_id']} ema10={r['ema10']:.2f} "
                f"ema20={r['ema20']:.2f} prev_close={r['prev_close']:.2f}")

    finally:
        close_db(conn)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"Done in {time.time() - t0:.1f}s")
