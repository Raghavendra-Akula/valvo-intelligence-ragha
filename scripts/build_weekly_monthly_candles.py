"""
Build pre-computed weekly and monthly candle tables from candles_daily.

Usage:
    python scripts/build_weekly_monthly_candles.py              # Full backfill
    python scripts/build_weekly_monthly_candles.py --days 7     # Incremental (last 7 days)
    python scripts/build_weekly_monthly_candles.py --rebuild    # Truncate + full rebuild

The script:
1. Reads raw daily candles from candles_daily
2. Aggregates into ISO weeks (first trading day) and calendar months (first trading day)
3. Upserts into candles_weekly and candles_monthly
"""

import os
import sys
import argparse
from datetime import datetime

# Add parent dir so we can import database module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from database.database import get_db, close_db


def build_weekly(conn, days=None):
    """Aggregate daily candles into ISO weekly candles.
    Groups by ISO week number, uses MIN(date) as candle date so the date
    reflects the first actual trading day (handles NSE holidays naturally).
    """
    cur = conn.cursor()
    date_filter = f"AND date >= (CURRENT_DATE - INTERVAL '{days} days')" if days else ""

    print(f"[Weekly] Building candles{f' (last {days} days)' if days else ' (full backfill)'}...")

    cur.execute(f"""
        INSERT INTO candles_weekly (security_id, date, open, high, low, close, volume, updated_at)
        SELECT
            security_id,
            MIN(date)::date as date,
            (array_agg(open ORDER BY date ASC))[1] as open,
            MAX(high) as high,
            MIN(low) as low,
            (array_agg(close ORDER BY date DESC))[1] as close,
            SUM(volume) as volume,
            NOW() as updated_at
        FROM candles_daily
        WHERE volume > 0 AND EXTRACT(dow FROM date) BETWEEN 1 AND 5 {date_filter}
        GROUP BY security_id, EXTRACT(isoyear FROM date), EXTRACT(week FROM date)
        ON CONFLICT (security_id, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            updated_at = NOW()
    """)
    count = cur.rowcount
    conn.commit()
    print(f"[Weekly] Upserted {count:,} weekly candles")
    return count


def build_monthly(conn, days=None):
    """Aggregate daily candles into calendar monthly candles.
    Groups by year+month, uses MIN(date) as candle date so the date
    reflects the first actual trading day of that month.
    """
    cur = conn.cursor()
    date_filter = f"AND date >= (CURRENT_DATE - INTERVAL '{days} days')" if days else ""

    print(f"[Monthly] Building candles{f' (last {days} days)' if days else ' (full backfill)'}...")

    cur.execute(f"""
        INSERT INTO candles_monthly (security_id, date, open, high, low, close, volume, updated_at)
        SELECT
            security_id,
            MIN(date)::date as date,
            (array_agg(open ORDER BY date ASC))[1] as open,
            MAX(high) as high,
            MIN(low) as low,
            (array_agg(close ORDER BY date DESC))[1] as close,
            SUM(volume) as volume,
            NOW() as updated_at
        FROM candles_daily
        WHERE volume > 0 AND EXTRACT(dow FROM date) BETWEEN 1 AND 5 {date_filter}
        GROUP BY security_id, EXTRACT(year FROM date), EXTRACT(month FROM date)
        ON CONFLICT (security_id, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            updated_at = NOW()
    """)
    count = cur.rowcount
    conn.commit()
    print(f"[Monthly] Upserted {count:,} monthly candles")
    return count


def main():
    parser = argparse.ArgumentParser(description="Build weekly/monthly candle tables")
    parser.add_argument("--days", type=int, default=None, help="Only process last N days (incremental)")
    parser.add_argument("--weekly-only", action="store_true", help="Only build weekly")
    parser.add_argument("--monthly-only", action="store_true", help="Only build monthly")
    parser.add_argument("--rebuild", action="store_true", help="Truncate tables before full rebuild (use after date-key changes)")
    args = parser.parse_args()

    start = datetime.now()
    print(f"=== Build Weekly/Monthly Candles — {start.strftime('%Y-%m-%d %H:%M:%S')} ===")

    conn = get_db()
    if not conn:
        print("ERROR: Cannot connect to database")
        sys.exit(1)

    try:
        if args.rebuild:
            cur = conn.cursor()
            print("[Rebuild] Truncating candles_weekly and candles_monthly...")
            if not args.monthly_only:
                cur.execute("TRUNCATE candles_weekly")
            if not args.weekly_only:
                cur.execute("TRUNCATE candles_monthly")
            conn.commit()
            print("[Rebuild] Tables truncated — starting full backfill")

        if not args.monthly_only:
            build_weekly(conn, days=args.days)
        if not args.weekly_only:
            build_monthly(conn, days=args.days)

        elapsed = (datetime.now() - start).total_seconds()
        print(f"=== Done in {elapsed:.1f}s ===")
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
