#!/usr/bin/env python3
"""
backfill_4pc_vol.py — Stockbee 4% on Volume daily counts.

For every stock × consecutive trading-day pair (yesterday, today), counts:
  up_4pc_vol   if (today_close - prev_close)/prev_close >=  4% AND today_vol > prev_vol
  down_4pc_vol if (today_close - prev_close)/prev_close <= -4% AND today_vol > prev_vol
…then writes per-date counts into breadth_daily_history.

Re-runnable. Excludes ETFs to match the live universe.

Usage:  cd Backend && python scripts/backfill_4pc_vol.py
"""
import os, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))

FETCH_START = "2003-01-01"


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("=== Stockbee 4% on Volume Backfill ===")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '600000'")

        log(f"Fetching candles_daily from {FETCH_START} (excluding ETFs)...")
        cur.execute("""
            SELECT cd.security_id, cd.date, cd.close, cd.volume
            FROM candles_daily cd
            JOIN stock_universe su ON cd.security_id = su.security_id
            WHERE cd.date >= %s
              AND cd.volume > 0
              AND cd.close > 0
              AND COALESCE(su.is_etf, false) = false
            ORDER BY cd.security_id, cd.date
        """, (FETCH_START,))
        rows = cur.fetchall()
        log(f"Fetched {len(rows):,} candle rows")

        if not rows:
            log("No data found. Exiting.")
            return

        per_stock = defaultdict(list)
        for r in rows:
            per_stock[r["security_id"]].append(
                (r["date"], float(r["close"]), int(r["volume"]))
            )
        log(f"Bucketed into {len(per_stock):,} stocks")

        up_counts = defaultdict(int)
        down_counts = defaultdict(int)
        for series in per_stock.values():
            for i in range(1, len(series)):
                dt, close_now, vol_now = series[i]
                _, close_prev, vol_prev = series[i - 1]
                if close_prev <= 0 or vol_prev <= 0 or vol_now <= vol_prev:
                    continue
                pct = (close_now - close_prev) / close_prev
                if pct >= 0.04:
                    up_counts[dt] += 1
                elif pct <= -0.04:
                    down_counts[dt] += 1

        log(f"  up_4pc_vol:   {len(up_counts):,} dates with non-zero count")
        log(f"  down_4pc_vol: {len(down_counts):,} dates with non-zero count")

        for col, counts in (("up_4pc_vol", up_counts), ("down_4pc_vol", down_counts)):
            log(f"Updating {col}...")
            updates = sorted(counts.items())
            if not updates:
                log(f"  no dates for {col}, skipping")
                continue
            batch_size = 500
            updated = 0
            for i in range(0, len(updates), batch_size):
                batch = updates[i:i+batch_size]
                values_sql = ",".join(
                    cur.mogrify("(%s::date, %s)", (dt, cnt)).decode()
                    for dt, cnt in batch
                )
                cur.execute(f"""
                    UPDATE breadth_daily_history bd
                    SET {col} = v.cnt
                    FROM (VALUES {values_sql}) AS v(d, cnt)
                    WHERE bd.date = v.d
                """)
                updated += cur.rowcount
            conn.commit()
            log(f"  Updated {updated:,} rows for {col}")

        cur.execute("""
            SELECT date, up_4pc_vol, down_4pc_vol
            FROM breadth_daily_history
            WHERE up_4pc_vol > 0
            ORDER BY up_4pc_vol DESC
            LIMIT 5
        """)
        log("Top-5 dates by up_4pc_vol:")
        for r in cur.fetchall():
            log(f"  {r['date']}: up={r['up_4pc_vol']}, down={r['down_4pc_vol']}")

        cur.execute("""
            SELECT date, up_4pc_vol, down_4pc_vol
            FROM breadth_daily_history
            WHERE down_4pc_vol > 0
            ORDER BY down_4pc_vol DESC
            LIMIT 5
        """)
        log("Top-5 dates by down_4pc_vol:")
        for r in cur.fetchall():
            log(f"  {r['date']}: up={r['up_4pc_vol']}, down={r['down_4pc_vol']}")

        log("=== Backfill complete ===")

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
