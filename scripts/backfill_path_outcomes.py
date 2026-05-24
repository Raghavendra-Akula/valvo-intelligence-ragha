"""
One-time backfill: classify path_outcome_{5d,10d} for every historical
breakout_events row, using the new path-aware bucketing.

This does NOT touch legacy outcome_5d/10d/20d or max_gain/max_dd/final
columns — it only fills the path_outcome_* / peak_pct_* / final_vs_pivot_* /
hit_5pct_day_* / below_pivot_day_* columns.

Run from Backend/:
  PYTHONPATH=. python3 scripts/backfill_path_outcomes.py
  PYTHONPATH=. python3 scripts/backfill_path_outcomes.py --from 2026-01-01 --to 2026-05-09
  PYTHONPATH=. python3 scripts/backfill_path_outcomes.py --batch-days 30 --dry-run
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Allow running from Backend/ root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.database import get_db
from services.breakout_path_outcomes import classify_path_outcome


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def backfill_window(start: date, end: date, dry_run: bool = False) -> dict:
    """Backfill path_outcome for events with breakout_date in [start, end]."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '600000'")  # 10 min

        cur.execute(
            """
            SELECT id, security_id, breakout_date, pivot_price,
                   path_outcome_5d, path_outcome_10d
              FROM breakout_events
             WHERE breakout_date BETWEEN %s AND %s
               AND (path_outcome_5d IS NULL OR path_outcome_10d IS NULL)
             ORDER BY breakout_date, id
            """,
            (start, end),
        )
        events = cur.fetchall()
        if not events:
            return {"events_in_range": 0, "updated": 0}

        sids = list({e["security_id"] for e in events})
        min_d = min(e["breakout_date"] for e in events)
        max_d = max(e["breakout_date"] for e in events)
        # Fetch enough forward bars for the longest window we classify (10d).
        # Add a buffer of 25 calendar days to account for weekends/holidays.
        cur.execute(
            """
            SELECT security_id, date, open, high, low, close
              FROM candles_daily
             WHERE security_id = ANY(%s::text[])
               AND date BETWEEN %s AND %s
               AND volume > 0
             ORDER BY security_id, date
            """,
            (sids, min_d, max_d + timedelta(days=25)),
        )
        cand_by_sid: dict = {}
        for r in cur.fetchall():
            cand_by_sid.setdefault(r["security_id"], []).append(r)

        updated = 0
        skipped_no_data = 0
        skipped_insufficient_bars = 0
        bucket_counts = {}

        for e in events:
            cands = cand_by_sid.get(e["security_id"], [])
            fwd = [c for c in cands if c["date"] > e["breakout_date"]]
            if not fwd:
                skipped_no_data += 1
                continue

            pivot_price = float(e["pivot_price"])
            updates = {}

            for window_name, n in (("5d", 5), ("10d", 10)):
                if e.get(f"path_outcome_{window_name}") is not None:
                    continue
                if len(fwd) < n:
                    skipped_insufficient_bars += 1
                    continue
                path = classify_path_outcome(pivot_price, fwd, n)
                if path is None:
                    continue
                updates[f"path_outcome_{window_name}"] = path["bucket"]
                updates[f"peak_pct_{window_name}"] = path["peak_pct"]
                updates[f"final_vs_pivot_{window_name}"] = path["final_pct"]
                updates[f"hit_5pct_day_{window_name}"] = path["hit_5pct_day"]
                updates[f"below_pivot_day_{window_name}"] = path["hit_below_pivot_day"]
                key = f"{window_name}:{path['bucket']}"
                bucket_counts[key] = bucket_counts.get(key, 0) + 1

            if not updates:
                continue

            if not dry_run:
                cols = ", ".join(f"{k} = %s" for k in updates.keys())
                cur.execute(
                    f"UPDATE breakout_events SET {cols} WHERE id = %s",
                    list(updates.values()) + [e["id"]],
                )
            updated += 1

        if not dry_run:
            conn.commit()

        return {
            "events_in_range": len(events),
            "updated": updated,
            "skipped_no_data": skipped_no_data,
            "skipped_insufficient_bars": skipped_insufficient_bars,
            "bucket_counts": bucket_counts,
        }
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", type=parse_date, default=None,
                   help="Start date inclusive (default: 2024-01-01)")
    p.add_argument("--to", dest="end", type=parse_date, default=None,
                   help="End date inclusive (default: today - 12d, so 10d window resolves)")
    p.add_argument("--batch-days", type=int, default=30,
                   help="Process this many days at a time (default: 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify but do not UPDATE")
    args = p.parse_args()

    start = args.start or date(2024, 1, 1)
    # Need 10 trading days + buffer for the 10d window to resolve
    end = args.end or (date.today() - timedelta(days=12))

    if start > end:
        print(f"Empty range: {start} > {end}")
        return

    total_updated = 0
    total_seen = 0
    bucket_totals = {}

    cur_start = start
    while cur_start <= end:
        cur_end = min(cur_start + timedelta(days=args.batch_days - 1), end)
        print(f"\n=== Backfilling {cur_start} → {cur_end} (dry_run={args.dry_run}) ===")
        r = backfill_window(cur_start, cur_end, dry_run=args.dry_run)
        print(f"  events_in_range:  {r.get('events_in_range', 0)}")
        print(f"  updated:          {r.get('updated', 0)}")
        print(f"  skipped_no_data:  {r.get('skipped_no_data', 0)}")
        print(f"  skipped_insufficient_bars: {r.get('skipped_insufficient_bars', 0)}")
        for k, v in (r.get("bucket_counts") or {}).items():
            bucket_totals[k] = bucket_totals.get(k, 0) + v

        total_seen += r.get("events_in_range", 0)
        total_updated += r.get("updated", 0)
        cur_start = cur_end + timedelta(days=1)

    print(f"\n=== TOTAL ===")
    print(f"events scanned : {total_seen}")
    print(f"rows updated   : {total_updated}")
    print(f"\nBucket distribution:")
    for k in sorted(bucket_totals.keys()):
        print(f"  {k:50s} {bucket_totals[k]:>6d}")


if __name__ == "__main__":
    main()
