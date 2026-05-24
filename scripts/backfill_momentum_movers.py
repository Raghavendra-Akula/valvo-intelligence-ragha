#!/usr/bin/env python3
"""
backfill_momentum_movers.py — Backfill up_*pc_*d momentum-mover columns.

For every (lookback, threshold) pair in MOMENTUM_PAIRS, walks each stock's
candles_daily series and counts, per date, how many stocks have rallied
> threshold% over the trailing N trading days. Writes the per-date counts
into the matching column on breadth_daily_history.

Re-runnable: it always recomputes from candles_daily, so to add new pairs
later (e.g. up_50pc_10d), append to MOMENTUM_PAIRS and re-run.

Usage:  cd Backend && python scripts/backfill_momentum_movers.py
"""
import os, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))

# (column_name, threshold_pct, lookback_days) — must mirror breadth_routes.MOMENTUM_PAIRS
MOMENTUM_PAIRS = [
    ("up_20pc_5d", 20, 5),
    ("up_30pc_5d", 30, 5),
]

FETCH_START = "2003-01-01"


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("=== Momentum Movers Backfill ===")
    log(f"Pairs: {[p[0] for p in MOMENTUM_PAIRS]}")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '600000'")  # 10 min

        # Pull (security_id, date, close), excluding ETFs to match the live universe.
        log(f"Fetching candles_daily from {FETCH_START} (excluding ETFs)...")
        cur.execute("""
            SELECT cd.security_id, cd.date, cd.close
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

        # Bucket per stock, ordered by date.
        per_stock = defaultdict(list)
        for r in rows:
            per_stock[r["security_id"]].append((r["date"], float(r["close"])))
        log(f"Bucketed into {len(per_stock):,} stocks")

        # Per-date counts: counts[col_name][date] = int.
        counts = {col: defaultdict(int) for col, _, _ in MOMENTUM_PAIRS}

        max_lookback = max(lb for _, _, lb in MOMENTUM_PAIRS)
        for sid, series in per_stock.items():
            n = len(series)
            if n <= max_lookback:
                continue
            for i in range(max_lookback, n):
                dt, close_now = series[i]
                if close_now <= 0:
                    continue
                for col, threshold, lookback in MOMENTUM_PAIRS:
                    if i < lookback:
                        continue
                    _, close_then = series[i - lookback]
                    if close_then <= 0:
                        continue
                    if (close_now - close_then) / close_then > (threshold / 100.0):
                        counts[col][dt] += 1

        for col, _, _ in MOMENTUM_PAIRS:
            log(f"  {col}: {len(counts[col]):,} dates with non-zero count")

        # Bulk-update breadth_daily_history. One UPDATE per column keeps the SQL bounded.
        for col, _, _ in MOMENTUM_PAIRS:
            log(f"Updating {col}...")
            updates = sorted(counts[col].items())  # [(date, count), ...]
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

        # Spot-check: pull a recent peak so we can sanity-check vs Chartink.
        cur.execute("""
            SELECT date, up_20pc_5d, up_30pc_5d
            FROM breadth_daily_history
            WHERE up_20pc_5d > 0
            ORDER BY up_20pc_5d DESC
            LIMIT 5
        """)
        log("Top-5 dates by up_20pc_5d:")
        for r in cur.fetchall():
            log(f"  {r['date']}: up_20pc_5d={r['up_20pc_5d']}, up_30pc_5d={r['up_30pc_5d']}")

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
