"""
index_summary.py — Populates public.index_daily_summary from candles_indices.

Mirrors Backend/routes/screener_routes.py::_refresh_summary() in structure:
one big CTE that aggregates the last 400 trading days per index symbol,
computes multi-timeframe closes + MAs + 52W range + return%, and UPSERTs
into index_daily_summary. Called lazily (background thread from the
sector-read path) or manually via the admin POST endpoint.

Leadership score (sector indices only) blends:
    50% * return_20d + 25% * return_60d + 25% * above_ma50_bonus
    (above_ma50_bonus = +10 if above, -10 if below)
which is a sane default. We rank sectors by this score and return the top N.

The CATEGORY_MAP below classifies each index as 'broad' / 'sector' / 'thematic'.
Symbols not listed default to 'other' and are excluded from leading-sector
ranking. Add new listings here as NSE expands their index family.
"""
from __future__ import annotations

import threading

from database.database import get_db, close_db


# ─── Curated symbol → category map ──────────────────────────────────────────
# Source: candles_indices.symbol as populated by the NSE index feed.
# Rules of thumb:
#   'sector'   = single-industry sector indices (used for leading-sector ranking)
#   'broad'    = market-wide benchmarks (NIFTY, NIFTY 500, BANKNIFTY…)
#   'thematic' = strategy / style / factor / theme indices (QUALITY 30, LOW VOL…)
# Anything else ends up 'other' and is excluded from the leading-sector UI.

CATEGORY_MAP = {
    # ── Broad market ───────────────────────────────────────────────────────
    "NIFTY":                "broad",
    "NIFTY 100":            "broad",
    "NIFTY 200":            "broad",
    "NIFTY 500":            "broad",
    "NIFTY MIDCAP 150":     "broad",
    "NIFTY SMALLCAP 250":   "broad",
    "BANKNIFTY":            "broad",
    "FINNIFTY":             "broad",
    "MIDCPNIFTY":           "broad",
    "NIFTY NEXT 50":        "broad",
    "INDIA VIX":            "broad",
    # ── Sector indices (leading-sector candidates) ─────────────────────────
    "NIFTY AUTO":           "sector",
    "NIFTY ENERGY":         "sector",
    "NIFTY FMCG":           "sector",
    "NIFTY HEALTHCARE":     "sector",
    "NIFTY IND DEFENCE":    "sector",
    "NIFTY INDIA MFG":      "sector",
    "NIFTY MEDIA":          "sector",
    "NIFTY METAL":          "sector",
    "NIFTY OIL AND GAS":    "sector",
    "NIFTY PHARMA":         "sector",
    "NIFTY PSU BANK":       "sector",
    "NIFTY PVT BANK":       "sector",
    "NIFTY REALTY":         "sector",
    "NIFTYIT":              "sector",   # no-space outlier, NSE canonical
    "NIFTY SERV SECTOR":    "sector",
    "NIFTY COMMODITIES":    "sector",
    "NIFTY CONSUMPTION":    "sector",
    "NIFTY CONS DURBL":     "sector",
    "NIFTYINFRA":           "sector",
    "NIFTYPSE":             "sector",
    "NIFTYCPSE":             "sector",
    # Everything else defaults to 'other' at INSERT time.
}


# ─── Refresh SQL ────────────────────────────────────────────────────────────
# One CTE pass over the last 400 trading days of candles_indices. Per symbol:
#  • cl[]   — closes DESC (cl[1] = latest, cl[2] = 1 day ago, …)
#  • ma50   — avg of cl[1..50]
#  • ma200  — avg of cl[1..200]
#  • 52w range — min/max of last 252 closes
#  • ath    — all-time high close across the full lookback
# UPSERT keyed on symbol so re-runs are idempotent.
_REFRESH_SQL = """
WITH cutoff AS (
    SELECT MAX(date) - INTERVAL '400 days' AS since FROM candles_indices
),
arrayed AS (
    SELECT
        symbol,
        ARRAY_AGG(close ORDER BY date DESC) AS cl
    FROM candles_indices, cutoff
    WHERE date >= cutoff.since
    GROUP BY symbol
    HAVING COUNT(*) >= 50
),
metrics AS (
    SELECT
        symbol,
        cl[1]                                                        AS prev_close,
        (SELECT AVG(x) FROM UNNEST(cl[1:50]) x)                      AS ma50,
        (SELECT AVG(x) FROM UNNEST(cl[1:200]) x)                     AS ma200,
        (SELECT MAX(x) FROM UNNEST(cl[1:252]) x)                     AS high_52w,
        (SELECT MIN(x) FROM UNNEST(cl[1:252]) x)                     AS low_52w,
        (SELECT MAX(x) FROM UNNEST(cl) x)                            AS ath,
        COALESCE(cl[5],   cl[ARRAY_LENGTH(cl, 1)])                   AS close_5d,
        COALESCE(cl[20],  cl[ARRAY_LENGTH(cl, 1)])                   AS close_20d,
        COALESCE(cl[60],  cl[ARRAY_LENGTH(cl, 1)])                   AS close_60d,
        COALESCE(cl[120], cl[ARRAY_LENGTH(cl, 1)])                   AS close_120d,
        COALESCE(cl[252], cl[ARRAY_LENGTH(cl, 1)])                   AS close_252d
    FROM arrayed
)
INSERT INTO index_daily_summary AS t (
    symbol, category,
    prev_close, ma50, ma200, high_52w, low_52w, ath,
    close_5d, close_20d, close_60d, close_120d, close_252d,
    return_5d, return_20d, return_60d, return_252d,
    above_ma50, above_ma200, leadership_score,
    computed_date, updated_at
)
SELECT
    m.symbol,
    COALESCE(%s::jsonb ->> m.symbol, 'other') AS category,
    m.prev_close, m.ma50, m.ma200, m.high_52w, m.low_52w, m.ath,
    m.close_5d, m.close_20d, m.close_60d, m.close_120d, m.close_252d,
    CASE WHEN m.close_5d   > 0 THEN (m.prev_close - m.close_5d)   / m.close_5d   * 100 END AS return_5d,
    CASE WHEN m.close_20d  > 0 THEN (m.prev_close - m.close_20d)  / m.close_20d  * 100 END AS return_20d,
    CASE WHEN m.close_60d  > 0 THEN (m.prev_close - m.close_60d)  / m.close_60d  * 100 END AS return_60d,
    CASE WHEN m.close_252d > 0 THEN (m.prev_close - m.close_252d) / m.close_252d * 100 END AS return_252d,
    (m.prev_close > m.ma50)                                                               AS above_ma50,
    (m.prev_close > m.ma200)                                                              AS above_ma200,
    -- Leadership score: 50% 20d-return + 25% 60d-return + 25% MA50 bonus.
    -- Only populated for sector indices (via the COALESCE below).
    CASE
        WHEN COALESCE(%s::jsonb ->> m.symbol, 'other') = 'sector'
             AND m.close_20d > 0 AND m.close_60d > 0
        THEN (
            0.50 * ((m.prev_close - m.close_20d)  / m.close_20d  * 100)
          + 0.25 * ((m.prev_close - m.close_60d)  / m.close_60d  * 100)
          + 0.25 * (CASE WHEN m.prev_close > m.ma50 THEN 10.0 ELSE -10.0 END)
        )
    END AS leadership_score,
    CURRENT_DATE AS computed_date,
    NOW()        AS updated_at
FROM metrics m
ON CONFLICT (symbol) DO UPDATE SET
    category         = EXCLUDED.category,
    prev_close       = EXCLUDED.prev_close,
    ma50             = EXCLUDED.ma50,
    ma200            = EXCLUDED.ma200,
    high_52w         = EXCLUDED.high_52w,
    low_52w          = EXCLUDED.low_52w,
    ath              = GREATEST(t.ath, EXCLUDED.ath),
    close_5d         = EXCLUDED.close_5d,
    close_20d        = EXCLUDED.close_20d,
    close_60d        = EXCLUDED.close_60d,
    close_120d       = EXCLUDED.close_120d,
    close_252d       = EXCLUDED.close_252d,
    return_5d        = EXCLUDED.return_5d,
    return_20d       = EXCLUDED.return_20d,
    return_60d       = EXCLUDED.return_60d,
    return_252d      = EXCLUDED.return_252d,
    above_ma50       = EXCLUDED.above_ma50,
    above_ma200      = EXCLUDED.above_ma200,
    leadership_score = EXCLUDED.leadership_score,
    computed_date    = EXCLUDED.computed_date,
    updated_at       = NOW();
"""


# ─── Lazy-refresh plumbing (same shape as screener_routes) ───────────────────

_refresh_lock = threading.Lock()


def refresh_index_summary() -> dict:
    """Re-populate index_daily_summary from candles_indices. Returns row counts
    for diagnostics. Safe to call from a background thread; the lock prevents
    duplicate concurrent runs."""
    import json

    with _refresh_lock:
        conn = get_db()
        if not conn:
            return {"ok": False, "error": "Database unavailable"}
        try:
            cur = conn.cursor()
            category_json = json.dumps(CATEGORY_MAP)
            cur.execute(_REFRESH_SQL, (category_json, category_json))
            affected = cur.rowcount
            conn.commit()
            cur.execute("SELECT COUNT(*) AS n FROM index_daily_summary WHERE category = 'sector'")
            sector_count = cur.fetchone()["n"]
            return {"ok": True, "upserted": affected, "sector_rows": sector_count}
        except Exception as exc:
            conn.rollback()
            return {"ok": False, "error": f"Refresh failed: {exc}"}
        finally:
            close_db(conn)


def _is_stale(cur, hours: int = 12) -> bool:
    """True if the summary needs a refresh (no rows, or last updated_at older
    than `hours`)."""
    try:
        cur.execute("SELECT MAX(updated_at) AS last FROM index_daily_summary")
        row = cur.fetchone()
        if not row or not row["last"]:
            return True
        import datetime
        return (datetime.datetime.utcnow() - row["last"]).total_seconds() > hours * 3600
    except Exception:
        return True


def trigger_background_refresh_if_stale() -> None:
    """Fire-and-forget: spawn a daemon thread that refreshes the summary if
    the current rows are stale. Returns immediately — caller reads whatever
    data is currently in the table."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if not _is_stale(cur):
            return
    finally:
        close_db(conn)

    def _worker():
        try:
            result = refresh_index_summary()
            print(f"[index_summary] background refresh: {result}")
        except Exception as exc:
            print(f"[index_summary] background refresh failed: {exc}")

    thread = threading.Thread(target=_worker, daemon=True, name="index-summary-refresh")
    thread.start()


def get_leading_sectors(limit: int = 5) -> list[dict]:
    """Return the top-N sector indices by leadership_score. Caller is
    responsible for triggering the background refresh if they care about
    freshness."""
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, category, prev_close, return_5d, return_20d, return_60d, "
            "       above_ma50, above_ma200, leadership_score, updated_at "
            "FROM index_daily_summary "
            "WHERE category = 'sector' AND leadership_score IS NOT NULL "
            "ORDER BY leadership_score DESC NULLS LAST "
            "LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[index_summary] get_leading_sectors failed: {exc}")
        return []
    finally:
        close_db(conn)


# ─── Sparklines + breadth stats for the dashboard's Leading Sectors card ───
# `return_20d` is preferred over `leadership_score` for ranking so the list
# shuffles meaningfully day to day (the score blends 60d and binary MA50
# bonus, which makes the order feel stagnant). For each leading sector we
# also surface:
#   • sparkline_closes  — last `spark_days` closes of the index itself
#   • new_highs_count   — constituents printing a new 52w high today
#   • above_all_mas     — constituents above EMA20 AND EMA50 AND EMA200
#   • total_stocks      — denominator (mapped, non-ETF constituents)
# Done in two SQL trips: one for the sparkline arrays, one for the breadth
# aggregates. Both filter to the top-N symbols only, so cost stays bounded.

def get_leading_sectors_with_stats(
    limit: int = 5,
    spark_days: int = 30,
) -> list[dict]:
    """Top-N sectors enriched with a sparkline + 52w-high count + above-all-MAs
    count. Sorted by `return_20d` DESC so the order refreshes daily."""
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, category, prev_close, return_5d, return_20d, return_60d, "
            "       above_ma50, above_ma200, leadership_score, updated_at "
            "FROM index_daily_summary "
            "WHERE category = 'sector' AND return_20d IS NOT NULL "
            "ORDER BY return_20d DESC NULLS LAST "
            "LIMIT %s",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return []

        symbols = [r["symbol"] for r in rows]

        # ── Sparklines: last `spark_days` closes per leading index ──
        # Buffer is `spark_days * 2 + 5` calendar days to cover weekends and
        # holidays while staying bounded.
        calendar_window = max(spark_days * 2 + 5, 60)
        cur.execute(
            """
            WITH recent AS (
                SELECT symbol, date, close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM candles_indices
                WHERE symbol = ANY(%s)
                  AND date >= CURRENT_DATE - %s
            )
            SELECT symbol,
                   ARRAY_AGG(close ORDER BY date ASC) AS closes
            FROM recent
            WHERE rn <= %s
            GROUP BY symbol
            """,
            (symbols, calendar_window, spark_days),
        )
        spark_map = {r["symbol"]: list(r["closes"] or []) for r in cur.fetchall()}

        # ── Constituent breadth: 52w highs + above-all-MAs counts ──
        # index_constituents.security_id can be UNMAPPED, so we resolve via
        # stock_universe.symbol / isin first (mirrors get_index_constituents).
        # close > ema_N (from stock_daily_summary) is mathematically equivalent
        # to close > live_ema_N because live_ema = close*K + ema*(1-K) and K∈(0,1).
        cur.execute(
            """
            WITH targets AS (
                SELECT UNNEST(%s::text[]) AS index_symbol
            ),
            constituents AS (
                SELECT DISTINCT
                    ic.index_symbol,
                    COALESCE(
                        su_symbol.security_id,
                        su_isin.security_id,
                        NULLIF(BTRIM(ic.security_id), '')
                    ) AS sid
                FROM index_constituents ic
                INNER JOIN targets t ON ic.index_symbol = t.index_symbol
                LEFT JOIN LATERAL (
                    SELECT su.security_id FROM stock_universe su
                    WHERE su.symbol = ic.stock_symbol
                      AND su.is_active = true
                    LIMIT 1
                ) su_symbol ON TRUE
                LEFT JOIN LATERAL (
                    SELECT su.security_id FROM stock_universe su
                    WHERE ic.isin IS NOT NULL
                      AND BTRIM(ic.isin) <> ''
                      AND su.isin = ic.isin
                      AND su.is_active = true
                    LIMIT 1
                ) su_isin ON TRUE
            ),
            live AS (
                SELECT DISTINCT ON (cd.security_id)
                    cd.security_id, cd.close, cd.high
                FROM candles_daily cd
                WHERE cd.date >= CURRENT_DATE - 5
                ORDER BY cd.security_id, cd.date DESC
            )
            SELECT
                c.index_symbol,
                COUNT(*) AS total_stocks,
                COUNT(*) FILTER (
                    WHERE l.close > s.ema20
                      AND l.close > s.ema50
                      AND l.close > s.ema200
                ) AS above_all_mas,
                COUNT(*) FILTER (
                    WHERE GREATEST(l.high, l.close) >= s.high_52w
                      AND s.high_52w > 0
                ) AS new_highs_count
            FROM constituents c
            JOIN stock_daily_summary s ON s.security_id = c.sid
            JOIN live l ON l.security_id = c.sid
            WHERE c.sid IS NOT NULL
              AND c.sid <> 'UNMAPPED'
              AND COALESCE(s.is_etf, false) = false
              AND COALESCE(s.ema20, 0) > 0
            GROUP BY c.index_symbol
            """,
            (symbols,),
        )
        stats_map = {
            r["index_symbol"]: {
                "total_stocks": int(r["total_stocks"] or 0),
                "above_all_mas": int(r["above_all_mas"] or 0),
                "new_highs_count": int(r["new_highs_count"] or 0),
            }
            for r in cur.fetchall()
        }

        for r in rows:
            sym = r["symbol"]
            r["sparkline"] = [float(c) for c in spark_map.get(sym, []) if c is not None]
            stats = stats_map.get(sym, {})
            r["total_stocks"] = stats.get("total_stocks", 0)
            r["above_all_mas"] = stats.get("above_all_mas", 0)
            r["new_highs_count"] = stats.get("new_highs_count", 0)
        return rows
    except Exception as exc:
        print(f"[index_summary] get_leading_sectors_with_stats failed: {exc}")
        # Fall back to the lean variant so the card still renders.
        try:
            return get_leading_sectors(limit=limit)
        except Exception:
            return []
    finally:
        close_db(conn)
