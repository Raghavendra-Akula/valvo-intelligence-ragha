"""
One-time historical backfill for the Breakout Success Intelligence pipeline.

Walks day-by-day from --from to --to (defaults: today-180 to today), invoking
the same 3 detection functions per trading day. Resumable via --from after a
crash. Runtime: ~30-45 min for 6 months on a warm pool.

Usage from Backend/:
  python -m scripts.backfill_breakouts --from 2025-11-01 --to 2026-05-08
  python -m scripts.backfill_breakouts --from 2026-04-01    # to=today
  python -m scripts.backfill_breakouts                       # last 180 days
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta

from database.database import get_db
from services.breakout_detection import (
    detect_pivots_for_universe,
    evaluate_pivots_for_breakouts,
    update_outcomes,
)


def _trading_dates(start: date, end: date) -> list:
    """Pull the actual trading dates (date present in candles_daily) between
    [start, end] inclusive. Excludes weekends + holidays automatically."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '15000'")
        cur.execute(
            """
            SELECT DISTINCT date
              FROM candles_daily
             WHERE date BETWEEN %s AND %s
             ORDER BY date
            """,
            (start, end),
        )
        return [r["date"] for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    args = ap.parse_args()

    today = date.today()
    end = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else today
    start = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from \
        else end - timedelta(days=180)

    print(f"📅 Backfill range: {start} → {end}")
    dates = _trading_dates(start, end)
    print(f"   {len(dates)} trading days")

    t_start = time.monotonic()
    for i, d in enumerate(dates, 1):
        t_day = time.monotonic()
        try:
            piv = detect_pivots_for_universe(d)
            evt = evaluate_pivots_for_breakouts(d)
            out = update_outcomes(d)
            elapsed = round(time.monotonic() - t_day, 1)
            print(f"  [{i:>3}/{len(dates)}] {d}  "
                  f"piv={piv['pivots_inserted']:>3}  evt={evt['events_inserted']:>3}  "
                  f"out={out['updated']:>3}  ({elapsed}s)")
        except Exception as e:
            print(f"  [{i:>3}/{len(dates)}] {d}  ❌ {e}", file=sys.stderr)

    print(f"🎯 Backfill complete in {round(time.monotonic() - t_start, 1)}s")


if __name__ == "__main__":
    main()
