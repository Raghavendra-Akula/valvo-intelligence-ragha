"""
IPO Lab — Young IPO analysis endpoints.
v2: Pre-computed from stock_daily_summary + live candle join (~1s vs old 5-7s).
IPO detected by first_trade_date in summary (refreshed daily by pg_cron).
"""
from flask import Blueprint, jsonify, request
from extensions import limiter

ipo_bp = Blueprint("ipo", __name__)


def _get_db():
    from database.database import get_db
    return get_db()


def _close_db(conn):
    from database.database import close_db
    close_db(conn)


@ipo_bp.route("/api/ipo/young", methods=["GET"])
@limiter.limit("60 per minute")
def young_ipos():
    """
    Get recent IPOs with listing performance metrics.
    v2: Single query on stock_daily_summary + live candle join.
    ?period=30|90|180|365|730|1095|1825 (default 365)
    Clamped to [7, 1825] — 7 days minimum, 5 years maximum.
    """
    period = request.args.get("period", 365, type=int)
    period = max(7, min(int(period or 365), 1825))

    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"error": "DB unavailable", "ipos": []}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")

        # Query A: IPOs from summary table (stocks with 50+ trading days — fast, pre-computed)
        # Uses trigger-synced live_close for current price, falls back to prev_close.
        # `u.is_ipo = true` is the precise filter — populated by the v2 migration
        # (full-history sweep of stock_daily_summary, since 50+ trading days
        # proves a stock is a real listing, not a Dhan phantom back-fill).
        # No `listing_open > 30` gate — KWIL @ ₹29.80, IBULLSLTD @ ₹21,
        # NAVKARURB @ ₹2.89 are all legitimate IPOs.
        cur.execute("""
            SELECT s.symbol, s.company_name as company, s.security_id,
                s.first_trade_date as listing_date, s.trading_days,
                s.listing_open, s.listing_close, s.listing_high, s.listing_volume,
                GREATEST(s.ath, COALESCE(s.live_high, s.ath)) as ath,
                COALESCE(s.live_close, s.prev_close) as current_price,
                ROUND(((s.listing_close - s.listing_open) / NULLIF(s.listing_open, 0) * 100)::numeric, 2) as listing_gain_pct,
                ROUND(((COALESCE(s.live_close, s.prev_close) - s.listing_close) / NULLIF(s.listing_close, 0) * 100)::numeric, 2) as return_from_listing_pct,
                ROUND(((COALESCE(s.live_close, s.prev_close) - GREATEST(s.ath, COALESCE(s.live_high, s.ath))) / NULLIF(GREATEST(s.ath, COALESCE(s.live_high, s.ath)), 0) * 100)::numeric, 2) as from_ath_pct,
                s.liq_cr as liquidity_cr,
                u.sector
            FROM stock_daily_summary s
            JOIN stock_universe u ON s.security_id = u.security_id
            WHERE s.first_trade_date >= CURRENT_DATE - %s
              AND s.is_etf = false
              AND s.listing_open > 0
              AND u.is_ipo = true
              AND u.is_active = true
              AND u.company_name NOT LIKE '%%AMC - %%'
            ORDER BY s.first_trade_date DESC
        """, (period,))
        rows_a = [dict(r) for r in cur.fetchall()]

        # Query B: Fresh IPOs NOT in summary (<50 trading days)
        # Optimized: start from stock_universe, anti-join with summary (~100ms vs ~8s).
        # `is_ipo = true` is the precise filter — it's stamped at ingestion by
        # Pipeline/ipo_check.py only when the symbol matches a real `ipo_issues`
        # row from NSE. Phantoms from Dhan's periodic historical back-fills stay
        # is_ipo=false (the default) and are excluded automatically.
        cur.execute("""
            WITH missing_sids AS (
                SELECT u.security_id
                FROM stock_universe u
                WHERE u.is_active = true
                  AND COALESCE(u.is_etf, false) = false
                  AND u.is_ipo = true
                  AND NOT EXISTS (SELECT 1 FROM stock_daily_summary s WHERE s.security_id = u.security_id)
            ),
            fresh AS (
                SELECT cd.security_id, MIN(cd.date) as listing_date, MAX(cd.date) as last_date,
                       COUNT(*) as trading_days, MAX(cd.high) as ath,
                       ROUND((AVG(cd.close * cd.volume) / 10000000)::numeric, 2) as liq_cr
                FROM candles_daily cd
                JOIN missing_sids ms ON cd.security_id = ms.security_id
                WHERE cd.volume > 0
                GROUP BY cd.security_id
                HAVING MIN(cd.date) >= CURRENT_DATE - %(period)s AND COUNT(*) < 50
            ),
            listing AS (
                SELECT DISTINCT ON (cd.security_id) cd.security_id, cd.open, cd.close, cd.high, cd.volume
                FROM candles_daily cd
                JOIN fresh f ON cd.security_id = f.security_id
                WHERE cd.volume > 0
                ORDER BY cd.security_id, cd.date ASC
            ),
            latest AS (
                SELECT DISTINCT ON (cd.security_id) cd.security_id, cd.close as current_price, cd.high as today_high
                FROM candles_daily cd
                JOIN fresh f ON cd.security_id = f.security_id
                WHERE cd.date >= CURRENT_DATE - 5
                ORDER BY cd.security_id, cd.date DESC
            )
            SELECT u.symbol, u.company_name as company, f.security_id,
                f.listing_date, f.trading_days,
                l.open as listing_open, l.close as listing_close, l.high as listing_high, l.volume as listing_volume,
                GREATEST(f.ath, c.today_high) as ath,
                c.current_price,
                ROUND(((l.close - l.open) / NULLIF(l.open, 0) * 100)::numeric, 2) as listing_gain_pct,
                ROUND(((c.current_price - l.close) / NULLIF(l.close, 0) * 100)::numeric, 2) as return_from_listing_pct,
                ROUND(((c.current_price - GREATEST(f.ath, c.today_high)) / NULLIF(GREATEST(f.ath, c.today_high), 0) * 100)::numeric, 2) as from_ath_pct,
                f.liq_cr as liquidity_cr,
                u.sector
            FROM fresh f
            JOIN listing l ON f.security_id = l.security_id
            JOIN latest c ON f.security_id = c.security_id
            JOIN stock_universe u ON f.security_id = u.security_id
            WHERE l.open > 0
              AND u.company_name NOT LIKE '%%AMC - %%'
              AND u.symbol NOT SIMILAR TO '%%(GOLD|SILVER|NIFTY|BANKBEES|LIQUID|DEBT)%%'
            ORDER BY f.listing_date DESC
        """, {"period": period})
        rows_b = [dict(r) for r in cur.fetchall()]

        rows = rows_a + rows_b
        # Sort combined results by listing_date descending (most recent first)
        rows.sort(key=lambda r: str(r.get("listing_date") or ""), reverse=True)

        ipos = []
        for r in rows:
            ipos.append({
                "symbol": r["symbol"],
                "company": r["company"],
                "sector": r.get("sector"),
                "listing_date": str(r["listing_date"]) if r["listing_date"] else None,
                "trading_days": r["trading_days"] or 0,
                "listing_open": float(r["listing_open"] or 0),
                "listing_close": float(r["listing_close"] or 0),
                "listing_high": float(r["listing_high"] or 0),
                "listing_gain_pct": float(r["listing_gain_pct"] or 0),
                "current_price": float(r["current_price"] or 0),
                "return_from_listing_pct": float(r["return_from_listing_pct"] or 0),
                "ath": float(r["ath"] or 0),
                "from_ath_pct": float(r["from_ath_pct"] or 0),
                "liquidity_cr": float(r["liquidity_cr"] or 0),
                "listing_volume": int(r["listing_volume"] or 0),
                "security_id": r["security_id"],
            })

        # Summary stats
        if ipos:
            avg_listing_gain = round(sum(i["listing_gain_pct"] for i in ipos) / len(ipos), 2)
            avg_return = round(sum(i["return_from_listing_pct"] for i in ipos) / len(ipos), 2)
            positive_listings = sum(1 for i in ipos if i["listing_gain_pct"] > 0)
            still_above_listing = sum(1 for i in ipos if i["return_from_listing_pct"] > 0)
        else:
            avg_listing_gain = avg_return = 0
            positive_listings = still_above_listing = 0

        return jsonify({
            "ipos": ipos,
            "total": len(ipos),
            "period_days": period,
            "summary": {
                "avg_listing_gain": avg_listing_gain,
                "avg_return_from_listing": avg_return,
                "positive_listings_pct": round(positive_listings / max(len(ipos), 1) * 100, 1),
                "still_above_listing_pct": round(still_above_listing / max(len(ipos), 1) * 100, 1),
            },
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[ipo] error: {e}")
        return jsonify({"error": "Internal error", "ipos": []}), 500
    finally:
        if conn:
            _close_db(conn)


@ipo_bp.route("/api/ipo/issues", methods=["GET"])
@limiter.limit("60 per minute")
def ipo_issues():
    """
    Pre-listing IPO pipeline from public.ipo_issues (populated daily by Feed 5 from NSE).
    Returns open / upcoming / recently-closed issues plus aggregate counts that include
    post-listing gain/loss stats derived from stock_daily_summary.

    Query params:
      ?segment=all|mainboard|sme  (default all) — filters by NSE series code
    """
    segment = (request.args.get("segment", "all") or "all").lower()
    if segment not in ("all", "mainboard", "sme"):
        segment = "all"

    def _segment_sql(alias="i"):
        if segment == "sme":
            return f"{alias}.series = 'SME'"
        if segment == "mainboard":
            return f"{alias}.series <> 'SME'"
        return "TRUE"

    def _classify_type(series):
        return "SME" if (series or "").upper() == "SME" else "Mainboard"

    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"error": "DB unavailable"}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")

        # Pull all rows from the configured segment — the dataset is small
        # (a handful of rows live at any time) so a full scan is fine.
        cur.execute(f"""
            SELECT id, symbol, company_name, series,
                   issue_start_date, issue_end_date,
                   price_band_raw, price_band_min, price_band_max,
                   issue_size, status, category, fetched_at
            FROM ipo_issues
            WHERE {_segment_sql('ipo_issues')}
            ORDER BY issue_start_date DESC NULLS LAST
        """)
        raw_rows = [dict(r) for r in cur.fetchall()]

        def _to_iso(d):
            return d.isoformat() if d else None

        def _num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        open_list, upcoming_list, closed_list = [], [], []
        for r in raw_rows:
            start = r["issue_start_date"]
            end = r["issue_end_date"]
            status_raw = (r["status"] or "").lower()
            # Prefer date-driven classification; fall back to status text.
            from datetime import date
            today = date.today()
            if start and end and start <= today <= end:
                bucket = "open"
            elif start and start > today:
                bucket = "upcoming"
            elif end and end < today:
                bucket = "closed"
            elif "forthcoming" in status_raw or "upcoming" in status_raw:
                bucket = "upcoming"
            elif "close" in status_raw:
                bucket = "closed"
            else:
                bucket = "open"

            item = {
                "id": r["id"],
                "symbol": r["symbol"],
                "company": r["company_name"],
                "series": r["series"],
                "type": _classify_type(r["series"]),
                "issue_start_date": _to_iso(start),
                "issue_end_date": _to_iso(end),
                "price_band_min": _num(r["price_band_min"]),
                "price_band_max": _num(r["price_band_max"]),
                "price_band_raw": r["price_band_raw"],
                "issue_size": r["issue_size"],
                "status": r["status"],
                "category": r["category"],
            }
            if bucket == "open":
                open_list.append(item)
            elif bucket == "upcoming":
                upcoming_list.append(item)
            else:
                closed_list.append(item)

        # Listed-gain / listed-loss counts from post-listing summary.
        # stock_universe only tracks mainboard (EQ/BE) series, so these counts are
        # the mainboard universe regardless of the segment filter — for "sme" the
        # counts stay the same until SME listing data is ingested.
        listed_gain = 0
        listed_loss = 0
        try:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN (s.listing_close - s.listing_open) > 0 THEN 1 ELSE 0 END) AS gains,
                    SUM(CASE WHEN (s.listing_close - s.listing_open) < 0 THEN 1 ELSE 0 END) AS losses
                FROM stock_daily_summary s
                WHERE s.first_trade_date >= CURRENT_DATE - 365
                  AND s.is_etf = false
                  AND s.listing_open > 0
            """)
            row = cur.fetchone()
            if row:
                listed_gain = int(row["gains"] or 0)
                listed_loss = int(row["losses"] or 0)
        except Exception as stat_err:
            print(f"[ipo.issues] listed-gain/loss query failed: {stat_err}")

        return jsonify({
            "segment": segment,
            "open": open_list,
            "upcoming": upcoming_list,
            "closed": closed_list,
            "counts": {
                "open": len(open_list),
                "upcoming": len(upcoming_list),
                "closed": len(closed_list),
                "listed_gain": listed_gain,
                "listed_loss": listed_loss,
            },
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[ipo.issues] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        if conn:
            _close_db(conn)
