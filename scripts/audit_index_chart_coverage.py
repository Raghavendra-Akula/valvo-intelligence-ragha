"""
audit_index_chart_coverage.py — Identify constituents with no candle data.

When some Bird's Eye tiles render "No data" while others render fine, the
likely cause isn't the SQL filter (we already dropped volume>0) — it's
that those specific constituents genuinely have zero rows in
candles_daily for the requested window. This script finds them.

Usage:
    PYTHONPATH=Backend python3 Backend/scripts/audit_index_chart_coverage.py "NIFTY SMALLCAP 100"
    PYTHONPATH=Backend python3 Backend/scripts/audit_index_chart_coverage.py        # all indices

Reports per index:
  - constituents with zero candles in last 365d
  - constituents with sparse data (<60 rows = ~3 months)
  - first/last candle date for the most-sparse stocks

Pure SELECTs; safe to run anytime. No mutations.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from database.database import close_db, get_db  # noqa: E402


def audit_one(cur, index_symbol: str, days: int = 365) -> tuple[int, int, list]:
    """Return (total, missing_count, gaps) for one index."""
    cur.execute(
        """
        WITH resolved AS (
            SELECT
                ic.stock_symbol,
                COALESCE(
                    su_symbol.security_id,
                    su_isin.security_id,
                    NULLIF(BTRIM(ic.security_id), '')
                ) AS sid
            FROM index_constituents ic
            LEFT JOIN stock_universe su_symbol
              ON su_symbol.symbol = ic.stock_symbol AND su_symbol.is_active = true
            LEFT JOIN stock_universe su_isin
              ON ic.isin IS NOT NULL AND su_isin.isin = ic.isin AND su_isin.is_active = true
            WHERE ic.index_symbol = %s
        )
        SELECT
            r.stock_symbol,
            r.sid,
            COUNT(cd.date) FILTER (
                WHERE cd.date >= (CURRENT_DATE - make_interval(days => %s))
                  AND cd.close > 0
            ) AS recent_rows,
            MAX(cd.date) AS last_date
        FROM resolved r
        LEFT JOIN candles_daily cd ON cd.security_id = r.sid
        WHERE r.sid IS NOT NULL AND r.sid <> 'UNMAPPED'
        GROUP BY r.stock_symbol, r.sid
        ORDER BY recent_rows ASC, r.stock_symbol
        """,
        (index_symbol, days),
    )
    rows = cur.fetchall() or []
    total = len(rows)
    missing = [r for r in rows if (r["recent_rows"] or 0) == 0]
    sparse = [r for r in rows if 0 < (r["recent_rows"] or 0) < 60]
    return total, len(missing), missing + sparse


def main() -> int:
    target = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else None

    conn = get_db()
    if not conn:
        print("ERROR: cannot open DB — check SUPABASE_* / DATABASE_URL env")
        return 2
    cur = conn.cursor()

    if target:
        targets = [target]
    else:
        cur.execute(
            "SELECT DISTINCT index_symbol FROM index_constituents "
            "ORDER BY index_symbol ASC"
        )
        targets = [r["index_symbol"] for r in (cur.fetchall() or [])]

    print(f"Auditing {len(targets)} index(es) — chart coverage in last 365d.\n")

    bad = 0
    for idx_sym in targets:
        total, missing_count, gaps = audit_one(cur, idx_sym, days=365)
        if total == 0:
            continue
        pct = (missing_count / total * 100) if total else 0
        flag = "⚠ " if missing_count else "  "
        print(f"{flag}{idx_sym:35s}  {missing_count:3d}/{total:3d} missing ({pct:.0f}%)")

        # First 6 worst offenders for indices that have any gap
        if missing_count and not target:
            for r in gaps[:6]:
                last = r.get("last_date")
                last_txt = last.strftime("%Y-%m-%d") if hasattr(last, "strftime") else (str(last) if last else "never")
                print(f"     · {r['stock_symbol']:15s} sid={r['sid']:<12s} rows={r['recent_rows'] or 0:4d} last={last_txt}")
            bad += 1

        # When auditing one specific index, dump the full list.
        if target and (missing_count or gaps):
            print()
            print("    Stocks with missing or sparse data:")
            for r in gaps:
                last = r.get("last_date")
                last_txt = last.strftime("%Y-%m-%d") if hasattr(last, "strftime") else (str(last) if last else "never")
                rows = r["recent_rows"] or 0
                tag = "MISSING" if rows == 0 else "SPARSE"
                print(f"      [{tag:7s}] {r['stock_symbol']:15s} sid={r['sid']:<12s} rows={rows:4d} last={last_txt}")

    close_db(conn)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
