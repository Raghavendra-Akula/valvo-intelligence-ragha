"""
Entry-time market metrics — liquidity and market cap snapshot at the moment
a position is opened. Stored on positions + journal_trades so the AI / analytics
layer can later answer "do I do better in mid-cap entries vs small-cap?" or
"did this stock's liquidity dry up after I entered?".

Both values are in Crores (₹).
  liquidity_at_entry = 20-day average of (volume × close) ending at entry_date
  mcap_at_entry      = shares_outstanding × close on entry_date

Source tables:
  candles_daily  — historical OHLCV (intraday-updated)
  stock_universe — shares_outstanding (refreshed monthly from Yahoo)

Returns None for either field when data isn't available; callers should
treat None as "unknown" and not fall back to a stale snapshot.
"""
from __future__ import annotations
from typing import Optional


def compute_entry_metrics(security_id: str, entry_date, conn=None) -> tuple[Optional[float], Optional[float]]:
    """Return (liquidity_at_entry_cr, mcap_at_entry_cr) for the given stock
    on the given date. Both values are floats in Crores, or None if the
    underlying data is missing. entry_date may be a date / datetime / 'YYYY-MM-DD'."""
    if not security_id:
        return (None, None)

    sid = str(security_id)
    ed_iso = entry_date.isoformat()[:10] if hasattr(entry_date, "isoformat") else (str(entry_date)[:10] if entry_date else None)
    if not ed_iso:
        return (None, None)

    should_close = False
    if conn is None:
        from database.database import get_db, close_db
        conn = get_db()
        should_close = True
    if conn is None:
        return (None, None)

    liquidity_cr: Optional[float] = None
    mcap_cr: Optional[float] = None

    try:
        cur = conn.cursor()

        # 20-day average dollar volume ending at entry_date (in Crores).
        # We require volume > 0 so we don't average in EOD-pending zero-volume
        # rows that would deflate the figure.
        cur.execute(
            """
            SELECT AVG(volume * close) / 10000000.0 AS liq_cr
            FROM (
                SELECT volume, close
                FROM candles_daily
                WHERE security_id = %s
                  AND date <= %s
                  AND volume > 0
                ORDER BY date DESC
                LIMIT 20
            ) sub
            """,
            (sid, ed_iso),
        )
        row = cur.fetchone()
        if row and row.get("liq_cr") is not None:
            liquidity_cr = round(float(row["liq_cr"]), 2)

        # Market cap = shares_outstanding × close-on-entry-date / 1e7.
        # We do this in one query so we don't make a second round trip.
        cur.execute(
            """
            SELECT su.shares_outstanding, cd.close
            FROM stock_universe su
            LEFT JOIN LATERAL (
                SELECT close
                FROM candles_daily
                WHERE security_id = %s
                  AND date <= %s
                ORDER BY date DESC
                LIMIT 1
            ) cd ON TRUE
            WHERE su.security_id = %s
            LIMIT 1
            """,
            (sid, ed_iso, sid),
        )
        row = cur.fetchone()
        if row and row.get("shares_outstanding") and row.get("close"):
            mcap_cr = round(float(row["shares_outstanding"]) * float(row["close"]) / 1e7, 2)

    except Exception as e:
        print(f"[entry_metrics] error sid={sid} date={ed_iso}: {e}")
    finally:
        if should_close:
            try:
                from database.database import close_db
                close_db(conn)
            except Exception:
                pass

    return (liquidity_cr, mcap_cr)
