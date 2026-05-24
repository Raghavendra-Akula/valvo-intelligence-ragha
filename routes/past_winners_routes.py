"""
Past Winners — Find biggest gaining stocks for any date range.

Endpoints:
    POST /api/past-winners/scan    — public (authenticated) gainer scan
    POST /api/past-winners/export  — admin-only CSV with sector/theme/quarterly
                                      enrichment, used for offline analysis.

Uses candles_daily (6.56M rows) with optimised MIN/MAX + point-lookup SQL.
Query runs in ~5-8s for a 1-year range.
"""
import csv
import io
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from flask import Blueprint, Response, g, jsonify, request
from extensions import limiter
from database.database import get_db, close_db
from services.admin_service import is_admin, log_admin_action
from services.segment_name_utils import clean_segment_name

past_winners_bp = Blueprint("past_winners", __name__)

VALID_SORT = {"roc", "max_roc"}


@past_winners_bp.route("/api/past-winners/scan", methods=["POST"])
@limiter.limit("30 per minute")
def scan_winners():
    """Scan candles_daily for biggest gainers in a date range.

    Body JSON:
        from_date (str):  YYYY-MM-DD  (required)
        to_date   (str):  YYYY-MM-DD  (required)
        min_price (float):            default 15
        min_liquidity_cr (float):     default 0 (no filter)
        min_trading_days (int):       default 10
        sort_by (str):                "roc" | "max_roc"  default "roc"
        limit (int):                  max rows, default 500
    """
    body = request.get_json(force=True, silent=True) or {}

    from_date = body.get("from_date", "")
    to_date = body.get("to_date", "")
    if not from_date or not to_date:
        return jsonify({"error": "from_date and to_date are required"}), 400

    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
        if fd >= td:
            return jsonify({"error": "from_date must be before to_date"}), 400
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    min_price = float(body.get("min_price", 15))
    min_liq = float(body.get("min_liquidity_cr", 0))
    min_mcap = float(body.get("min_mcap_cr", 0))
    min_days = int(body.get("min_trading_days", 10))
    min_roc = float(body.get("min_roc", 0))
    sort_by = body.get("sort_by", "roc")
    limit = min(int(body.get("limit", 500)), 1000)

    if sort_by not in VALID_SORT:
        sort_by = "roc"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '30000'")

        t0 = time.time()

        # Build optional filters
        extra_filters = ""
        params = [from_date, to_date, min_days, min_price]
        if min_liq > 0:
            extra_filters += " AND a.avg_turnover_cr >= %s"
            params.append(min_liq)
        if min_mcap > 0:
            extra_filters += " AND COALESCE(su.shares_outstanding, 0) * fc.close / 1e7 >= %s"
            params.append(min_mcap)
        if min_roc > 0:
            extra_filters += " AND ((lc.close - fc.close) / NULLIF(fc.close, 0) * 100) >= %s"
            params.append(min_roc)
        params.append(limit)

        sql = f"""
        WITH agg AS (
            SELECT security_id,
                MIN(date) as first_date, MAX(date) as last_date,
                MAX(close) as max_close, COUNT(*) as trading_days,
                ROUND((AVG(volume::float8 * close) / 1e7)::numeric, 2) as avg_turnover_cr
            FROM candles_daily
            WHERE date >= %s AND date <= %s
              AND date < CURRENT_DATE
            GROUP BY security_id
            HAVING COUNT(*) >= %s
        )
        SELECT su.symbol, su.company_name, a.security_id, su.sector, su.industry,
            su.shares_outstanding,
            fc.close as start_close, a.first_date as start_date,
            lc.close as end_close, a.last_date as end_date,
            a.max_close, a.trading_days, a.avg_turnover_cr,
            ROUND(((lc.close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) as roc,
            ROUND(((a.max_close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) as max_roc,
            ROUND((COALESCE(su.shares_outstanding, 0) * fc.close / 1e7)::numeric, 0) as initial_mcap_cr
        FROM agg a
        JOIN candles_daily fc ON a.security_id = fc.security_id AND a.first_date = fc.date
        JOIN candles_daily lc ON a.security_id = lc.security_id AND a.last_date = lc.date
        JOIN stock_universe su ON a.security_id = su.security_id
        WHERE lc.close > fc.close
            AND fc.close > %s
            AND su.is_etf = false
            {extra_filters}
        ORDER BY {sort_by} DESC
        LIMIT %s
        """

        cur.execute(sql, params)
        rows = cur.fetchall()

        # ─── Benchmark: Smallcap 100 return for same period ───
        benchmark = {}
        try:
            cur.execute("""
                WITH idx AS (
                    SELECT
                        (ARRAY_AGG(close ORDER BY date ASC))[1] as start_close,
                        (ARRAY_AGG(close ORDER BY date DESC))[1] as end_close,
                        (ARRAY_AGG(date ORDER BY date ASC))[1] as start_date,
                        (ARRAY_AGG(date ORDER BY date DESC))[1] as end_date
                    FROM candles_indices
                    WHERE symbol = 'NIFTY SMALLCAP 100'
                      AND date >= %s AND date <= %s
                )
                SELECT *, ROUND(((end_close - start_close) / NULLIF(start_close, 0) * 100)::numeric, 2) as roc
                FROM idx WHERE start_close IS NOT NULL
            """, (from_date, to_date))
            br = cur.fetchone()
            if br and br["roc"] is not None:
                benchmark = {
                    "symbol": "SMALLCAP 100",
                    "roc": float(br["roc"]),
                    "start_close": float(br["start_close"]),
                    "end_close": float(br["end_close"]),
                }
        except Exception:
            pass  # Index data may not exist for old FYs — skip silently

        elapsed = round((time.time() - t0) * 1000)

        stocks = []
        for i, r in enumerate(rows, 1):
            stocks.append({
                "rank": i,
                "security_id": r["security_id"],
                "symbol": r["symbol"],
                "company_name": r["company_name"],
                "sector": r["sector"] or "",
                "industry": r["industry"] or "",
                "start_close": float(r["start_close"]),
                "start_date": r["start_date"].isoformat(),
                "end_close": float(r["end_close"]),
                "end_date": r["end_date"].isoformat(),
                "max_close": float(r["max_close"]),
                "roc": float(r["roc"]) if r["roc"] else 0,
                "max_roc": float(r["max_roc"]) if r["max_roc"] else 0,
                "trading_days": r["trading_days"],
                "avg_turnover_cr": float(r["avg_turnover_cr"]) if r["avg_turnover_cr"] else 0,
                "initial_mcap_cr": float(r["initial_mcap_cr"]) if r["initial_mcap_cr"] else 0,
            })

        return jsonify({
            "stocks": stocks,
            "benchmark": benchmark,
            "meta": {
                "from_date": from_date,
                "to_date": to_date,
                "total_results": len(stocks),
                "query_time_ms": elapsed,
                "filters": {
                    "min_price": min_price,
                    "min_liquidity_cr": min_liq,
                    "min_mcap_cr": min_mcap,
                    "min_trading_days": min_days,
                    "min_roc": min_roc,
                    "sort_by": sort_by,
                },
            },
        })

    except Exception as e:
        err_msg = str(e)
        if "cancel" in err_msg.lower() or "timeout" in err_msg.lower():
            return jsonify({"error": "Query timed out. Try a shorter date range."}), 504
        return jsonify({"error": err_msg}), 500
    finally:
        close_db(conn)


# ─────────────────────────────────────────────────────────────────
# Admin export — rich CSV used to brief an LLM for equity-research
# narratives on past-winners. Pulls every internal signal we have that
# might explain *why* the stock moved (catalysts, ownership, alpha
# decomposition, re-rating, business profile, identity links).
# ─────────────────────────────────────────────────────────────────


def _fmt_num(v):
    """Render a numeric value for CSV — empty string for NULL, plain float otherwise."""
    if v is None:
        return ""
    try:
        return float(v)
    except (TypeError, ValueError):
        return ""


def _yoy_period(period: str) -> str | None:
    """Derive the year-ago period string. e.g. 'Q3FY25' -> 'Q3FY24'.

    Returns None if the format is unexpected.
    """
    if not period or "FY" not in period:
        return None
    try:
        prefix, fy = period.split("FY")
        yr = int(fy)
        return f"{prefix}FY{(yr - 1) % 100:02d}"
    except (ValueError, IndexError):
        return None


def _ttm_eps(quarters: list[dict], as_of: date) -> float | None:
    """Sum the EPS of the latest 4 quarters with period_end_date <= as_of.

    Returns None if fewer than 4 quarters or any EPS is missing.
    """
    eligible = [q for q in quarters if q.get("period_end_date") and q["period_end_date"] <= as_of]
    eligible.sort(key=lambda q: q["period_end_date"], reverse=True)
    if len(eligible) < 4:
        return None
    eps_vals = []
    for q in eligible[:4]:
        v = q.get("eps")
        if v is None:
            return None
        try:
            eps_vals.append(float(v))
        except (TypeError, ValueError):
            return None
    return sum(eps_vals)


@past_winners_bp.route("/api/past-winners/export", methods=["POST"])
@limiter.limit("10 per minute")
def export_winners():
    """Admin-only CSV export of past winners with full equity-research context.

    Body JSON (same as /scan plus optional security_ids):
        from_date, to_date     — date range (required)
        min_price, min_liquidity_cr, min_mcap_cr, min_trading_days, sort_by, limit
        security_ids (list)    — optional; restrict export to these IDs (so
                                 the file matches the admin's filtered view).

    Columns are grouped into:
      Identity / Move metrics / Alpha decomposition (cohort + peer returns)
      / Valuation re-rating / Business profile / In-window catalysts
      (results, filings, corporate actions) / Last 3 pre-move quarters
      / External lookup URLs.
    """
    if not is_admin(g.user_id):
        return jsonify({"error": "Admin only"}), 403

    body = request.get_json(force=True, silent=True) or {}

    from_date = body.get("from_date", "")
    to_date = body.get("to_date", "")
    if not from_date or not to_date:
        return jsonify({"error": "from_date and to_date are required"}), 400

    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
        if fd >= td:
            return jsonify({"error": "from_date must be before to_date"}), 400
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    min_price = float(body.get("min_price", 15))
    min_liq = float(body.get("min_liquidity_cr", 0))
    min_mcap = float(body.get("min_mcap_cr", 0))
    min_days = int(body.get("min_trading_days", 10))
    min_roc = float(body.get("min_roc", 0))
    sort_by = body.get("sort_by", "roc")
    limit = min(int(body.get("limit", 500)), 1000)
    if sort_by not in VALID_SORT:
        sort_by = "roc"

    raw_ids = body.get("security_ids") or []
    security_ids = [str(s) for s in raw_ids if s] if isinstance(raw_ids, list) else []

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '60000'")

        # ── 1. Re-run the scan SQL (mirrors /scan) ──
        extra_filters = ""
        params = [from_date, to_date, min_days, min_price]
        if min_liq > 0:
            extra_filters += " AND a.avg_turnover_cr >= %s"
            params.append(min_liq)
        if min_mcap > 0:
            extra_filters += " AND COALESCE(su.shares_outstanding, 0) * fc.close / 1e7 >= %s"
            params.append(min_mcap)
        if min_roc > 0:
            extra_filters += " AND ((lc.close - fc.close) / NULLIF(fc.close, 0) * 100) >= %s"
            params.append(min_roc)
        if security_ids:
            extra_filters += " AND a.security_id = ANY(%s)"
            params.append(security_ids)
        params.append(limit)

        sql = f"""
        WITH agg AS (
            SELECT security_id,
                MIN(date) as first_date, MAX(date) as last_date,
                MAX(close) as max_close, COUNT(*) as trading_days,
                ROUND((AVG(volume::float8 * close) / 1e7)::numeric, 2) as avg_turnover_cr
            FROM candles_daily
            WHERE date >= %s AND date <= %s
              AND date < CURRENT_DATE
            GROUP BY security_id
            HAVING COUNT(*) >= %s
        )
        SELECT su.symbol, su.company_name, a.security_id, su.sector, su.industry,
            fc.close as start_close, a.first_date as start_date,
            lc.close as end_close, a.last_date as end_date,
            a.max_close, a.trading_days, a.avg_turnover_cr,
            ROUND(((lc.close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) as roc,
            ROUND(((a.max_close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) as max_roc,
            ROUND((COALESCE(su.shares_outstanding, 0) * fc.close / 1e7)::numeric, 0) as initial_mcap_cr
        FROM agg a
        JOIN candles_daily fc ON a.security_id = fc.security_id AND a.first_date = fc.date
        JOIN candles_daily lc ON a.security_id = lc.security_id AND a.last_date = lc.date
        JOIN stock_universe su ON a.security_id = su.security_id
        WHERE lc.close > fc.close
            AND fc.close > %s
            AND su.is_etf = false
            {extra_filters}
        ORDER BY {sort_by} DESC
        LIMIT %s
        """
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

        sec_ids = [r["security_id"] for r in rows]
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()

        # ── 2. Sector + sub-sector + wave + theme (V2 view, V1 fallback) ──
        # sector_map: best display string for the cohort (sub-sector preferred)
        # sub_sector_keys: the V2 sub_sector_name used for cohort grouping
        # theme_keys:      the V2 primary_theme_name used for cohort grouping
        sector_map: dict = {}
        sub_sector_keys: dict = {}
        wave_map: dict = {}
        theme_map: dict = {}
        theme_keys: dict = {}

        if sec_ids:
            try:
                cur.execute("""
                    SELECT security_id, sector, sub_sector_name,
                           primary_theme_name, primary_wave_name
                      FROM v_stock_classification_v2
                     WHERE security_id = ANY(%s)
                """, (sec_ids,))
                for r in cur.fetchall():
                    sid = r["security_id"]
                    sector_map[sid] = r["sub_sector_name"] or r["sector"] or ""
                    sub_sector_keys[sid] = r["sub_sector_name"] or ""
                    wave_map[sid] = r["primary_wave_name"] or ""
                    theme_map[sid] = r["primary_theme_name"] or ""
                    theme_keys[sid] = r["primary_theme_name"] or ""
            except Exception:
                pass

            missing_themes = [s for s in sec_ids if not theme_map.get(s)]
            if missing_themes:
                try:
                    cur.execute("""
                        SELECT st.security_id, t.name AS theme_name
                          FROM stock_themes_v2 st
                          JOIN themes_v2 t ON t.slug = st.theme_slug
                         WHERE st.security_id = ANY(%s)
                           AND st.is_primary = TRUE
                    """, (missing_themes,))
                    for r in cur.fetchall():
                        if not theme_map.get(r["security_id"]):
                            theme_map[r["security_id"]] = r["theme_name"]
                except Exception:
                    pass

            missing_sectors = [s for s in sec_ids if not sector_map.get(s)]
            if missing_sectors:
                try:
                    cur.execute("""
                        SELECT scs.security_id, cs.name AS sector_name
                          FROM stock_custom_sector scs
                          JOIN custom_sectors cs ON cs.id = scs.custom_sector_id
                         WHERE scs.security_id = ANY(%s)
                           AND scs.is_primary = TRUE
                    """, (missing_sectors,))
                    for r in cur.fetchall():
                        if not sector_map.get(r["security_id"]):
                            sector_map[r["security_id"]] = r["sector_name"]
                except Exception:
                    pass

        # ── 3. Last 8 quarters per stock (drives pre-move snapshot, in-window
        # result, YoY comparison, and TTM EPS for re-rating) ──
        all_quarters_map: dict = defaultdict(list)
        if sec_ids:
            try:
                cur.execute("""
                    WITH ranked AS (
                        SELECT fq.security_id, fq.period, fq.period_end_date, fq.filing_date,
                               fq.revenue_cr, fq.operating_profit_cr, fq.opm_percent,
                               fq.net_profit_cr, fq.eps, fq.is_consolidated,
                               ROW_NUMBER() OVER (
                                   PARTITION BY fq.security_id, fq.is_consolidated
                                   ORDER BY fq.period_end_date DESC
                               ) AS rn
                          FROM financials_quarterly fq
                         WHERE fq.security_id = ANY(%s)
                    ),
                    preferred AS (
                        SELECT DISTINCT ON (security_id) security_id, is_consolidated
                          FROM ranked
                         ORDER BY security_id, is_consolidated DESC NULLS LAST
                    )
                    SELECT r.security_id, r.period, r.period_end_date, r.filing_date,
                           r.revenue_cr, r.operating_profit_cr, r.opm_percent,
                           r.net_profit_cr, r.eps
                      FROM ranked r
                      JOIN preferred p ON p.security_id = r.security_id
                                     AND p.is_consolidated IS NOT DISTINCT FROM r.is_consolidated
                     WHERE r.rn <= 8
                  ORDER BY r.security_id, r.period_end_date DESC
                """, (sec_ids,))
                for r in cur.fetchall():
                    all_quarters_map[r["security_id"]].append(dict(r))
            except Exception as e:
                print(f"[past_winners/export] all_quarters fetch error: {e}")

        # ── 4. In-window filings (top 5 per stock by filing_date DESC) ──
        # These are where order wins, JVs, capex announcements, US-client
        # contracts surface — the most direct catalyst evidence we have.
        filings_map: dict = defaultdict(list)
        if sec_ids:
            try:
                cur.execute("""
                    WITH ranked AS (
                        SELECT security_id, filing_type, filing_date, description, period,
                               ROW_NUMBER() OVER (
                                   PARTITION BY security_id
                                   ORDER BY filing_date DESC
                               ) AS rn,
                               COUNT(*) OVER (PARTITION BY security_id) AS total_count
                          FROM filings
                         WHERE security_id = ANY(%s)
                           AND filing_date BETWEEN %s::date AND %s::date
                    )
                    SELECT security_id, filing_type, filing_date, description, period, total_count
                      FROM ranked
                     WHERE rn <= 5
                  ORDER BY security_id, filing_date DESC
                """, (sec_ids, from_date, to_date))
                for r in cur.fetchall():
                    filings_map[r["security_id"]].append(dict(r))
            except Exception as e:
                print(f"[past_winners/export] filings fetch error: {e}")

        # ── 5. In-window corporate actions ──
        corp_actions_map: dict = defaultdict(list)
        if sec_ids:
            try:
                cur.execute("""
                    SELECT security_id, action_type, ex_date, action_date,
                           details, dividend_amount, dividend_type,
                           bonus_ratio, split_ratio
                      FROM corporate_actions
                     WHERE security_id = ANY(%s)
                       AND (
                            (ex_date     BETWEEN %s::date AND %s::date)
                         OR (action_date BETWEEN %s::date AND %s::date)
                       )
                  ORDER BY security_id,
                           COALESCE(ex_date, action_date) DESC
                """, (sec_ids, from_date, to_date, from_date, to_date))
                for r in cur.fetchall():
                    corp_actions_map[r["security_id"]].append(dict(r))
            except Exception as e:
                print(f"[past_winners/export] corp_actions fetch error: {e}")

        # ── 6. 52-week high relative to to_date (last 252 trading days) ──
        high_52w_map: dict = {}
        if sec_ids:
            try:
                cur.execute("""
                    SELECT security_id, MAX(close) AS high_52w
                      FROM candles_daily
                     WHERE security_id = ANY(%s)
                       AND date >  (%s::date - INTERVAL '365 days')
                       AND date <= %s::date
                  GROUP BY security_id
                """, (sec_ids, to_date, to_date))
                for r in cur.fetchall():
                    if r["high_52w"] is not None:
                        high_52w_map[r["security_id"]] = float(r["high_52w"])
            except Exception as e:
                print(f"[past_winners/export] 52w high fetch error: {e}")

        # ── 7. Smallcap 100 ROC for window (single benchmark row) ──
        smallcap_roc: float | None = None
        try:
            cur.execute("""
                WITH idx AS (
                    SELECT
                        (ARRAY_AGG(close ORDER BY date ASC))[1]  AS sc,
                        (ARRAY_AGG(close ORDER BY date DESC))[1] AS ec
                    FROM candles_indices
                    WHERE symbol = 'NIFTY SMALLCAP 100'
                      AND date >= %s AND date <= %s
                )
                SELECT ROUND(((ec - sc) / NULLIF(sc, 0) * 100)::numeric, 2) AS roc
                  FROM idx
                 WHERE sc IS NOT NULL
            """, (from_date, to_date))
            br = cur.fetchone()
            if br and br["roc"] is not None:
                smallcap_roc = float(br["roc"])
        except Exception:
            pass

        # ── 8. Sector + theme cohort average ROC during window ──
        # Cohort = every V2-classified stock sharing the winner's sub-sector
        # or primary theme. Tells us how much of the move was the wave.
        sector_cohort_map: dict = {}
        theme_cohort_map: dict = {}
        unique_sub_sectors = sorted({v for v in sub_sector_keys.values() if v})
        unique_theme_names = sorted({v for v in theme_keys.values() if v})

        def _cohort_query(group_col: str, group_values: list) -> dict:
            """Run a generic cohort-ROC query keyed by a V2 view column."""
            if not group_values:
                return {}
            try:
                cur.execute(f"""
                    WITH cohort AS (
                        SELECT v.security_id, v.{group_col} AS gkey
                          FROM v_stock_classification_v2 v
                         WHERE v.{group_col} = ANY(%s)
                    ),
                    agg AS (
                        SELECT c.security_id, c.gkey,
                               MIN(cd.date) AS fd, MAX(cd.date) AS ld
                          FROM cohort c
                          JOIN candles_daily cd ON cd.security_id = c.security_id
                         WHERE cd.date >= %s AND cd.date <= %s
                           AND cd.date < CURRENT_DATE
                      GROUP BY c.security_id, c.gkey
                        HAVING COUNT(*) >= 5
                    )
                    SELECT a.gkey,
                           ROUND(AVG(((lc.close - fc.close) / NULLIF(fc.close, 0) * 100))::numeric, 2) AS avg_roc,
                           COUNT(*) AS n
                      FROM agg a
                      JOIN candles_daily fc ON a.security_id = fc.security_id AND a.fd = fc.date
                      JOIN candles_daily lc ON a.security_id = lc.security_id AND a.ld = lc.date
                  GROUP BY a.gkey
                """, (group_values, from_date, to_date))
                out: dict = {}
                for r in cur.fetchall():
                    out[r["gkey"]] = {
                        "avg_roc": float(r["avg_roc"]) if r["avg_roc"] is not None else None,
                        "n": int(r["n"] or 0),
                    }
                return out
            except Exception as e:
                print(f"[past_winners/export] cohort {group_col} error: {e}")
                return {}

        sector_cohort_map = _cohort_query("sub_sector_name", unique_sub_sectors)
        theme_cohort_map = _cohort_query("primary_theme_name", unique_theme_names)

        # ── 9. Top 3 peers + their ROC during window ──
        peers_map: dict = defaultdict(list)
        if sec_ids:
            try:
                cur.execute("""
                    WITH peer_list AS (
                        SELECT security_id AS source_id, peer_security_id, peer_symbol, relevance_rank
                          FROM peers
                         WHERE security_id = ANY(%s)
                           AND COALESCE(relevance_rank, 99) <= 3
                    ),
                    peer_agg AS (
                        SELECT pl.source_id, pl.peer_security_id, pl.peer_symbol, pl.relevance_rank,
                               MIN(cd.date) AS fd, MAX(cd.date) AS ld
                          FROM peer_list pl
                          JOIN candles_daily cd ON cd.security_id = pl.peer_security_id
                         WHERE cd.date >= %s AND cd.date <= %s
                           AND cd.date < CURRENT_DATE
                      GROUP BY pl.source_id, pl.peer_security_id, pl.peer_symbol, pl.relevance_rank
                        HAVING COUNT(*) >= 5
                    )
                    SELECT pa.source_id, pa.peer_symbol, pa.relevance_rank,
                           ROUND(((lc.close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) AS peer_roc
                      FROM peer_agg pa
                      JOIN candles_daily fc ON pa.peer_security_id = fc.security_id AND pa.fd = fc.date
                      JOIN candles_daily lc ON pa.peer_security_id = lc.security_id AND pa.ld = lc.date
                  ORDER BY pa.source_id, pa.relevance_rank
                """, (sec_ids, from_date, to_date))
                for r in cur.fetchall():
                    peers_map[r["source_id"]].append(dict(r))
            except Exception as e:
                print(f"[past_winners/export] peers fetch error: {e}")

        # ── 10. fundamentals_overview — identity + business profile ──
        fundamentals_map: dict = {}
        if sec_ids:
            try:
                cur.execute("""
                    SELECT security_id, isin, bse_code, nse_code, website,
                           listing_date, about, industry,
                           promoter_holding_pct, debt_to_equity,
                           sales_growth_3yr_cagr, profit_growth_ttm,
                           roce, week_52_high
                      FROM fundamentals_overview
                     WHERE security_id = ANY(%s)
                """, (sec_ids,))
                for r in cur.fetchall():
                    fundamentals_map[r["security_id"]] = dict(r)
            except Exception as e:
                print(f"[past_winners/export] fundamentals fetch error: {e}")

        # ── 11. Top 3 segments per stock (latest period only) ──
        segments_map: dict = defaultdict(list)
        if sec_ids:
            try:
                cur.execute("""
                    WITH ranked AS (
                        SELECT s.security_id, s.period_end_date, s.segment_name,
                               s.segment_revenue_cr, s.segment_revenue_pct,
                               DENSE_RANK() OVER (
                                   PARTITION BY s.security_id
                                   ORDER BY s.period_end_date DESC
                               ) AS period_rank
                          FROM segments_quarterly s
                         WHERE s.security_id = ANY(%s)
                           AND s.segment_name IS NOT NULL
                           AND s.segment_name <> ''
                    )
                    SELECT security_id, segment_name, segment_revenue_cr, segment_revenue_pct
                      FROM ranked
                     WHERE period_rank = 1
                  ORDER BY security_id, segment_revenue_cr DESC NULLS LAST
                """, (sec_ids,))
                for r in cur.fetchall():
                    segments_map[r["security_id"]].append(dict(r))
            except Exception as e:
                print(f"[past_winners/export] segments fetch error: {e}")

        # ── 12. Build CSV ──
        output = io.StringIO()
        writer = csv.writer(output)
        header = [
            # Identity
            "Rank", "Symbol", "Stock Name", "Industry",
            "Sector", "Sub-Sector / Wave", "Theme", "About",
            # Move metrics
            "Initial MCap (Cr)", "Start Date", "End Date",
            "Start Close", "End Close", "Max Close",
            "ROC %", "Max ROC %", "Trading Days", "Avg Turnover (Cr)",
            # Alpha decomposition
            "Smallcap 100 ROC %",
            "Sector Cohort Avg ROC %", "Sector Cohort N",
            "Theme Cohort Avg ROC %", "Theme Cohort N",
            "Alpha vs Smallcap (pp)",
            "Top Peer 1", "Top Peer 1 ROC %",
            "Top Peer 2", "Top Peer 2 ROC %",
            "Top Peer 3", "Top Peer 3 ROC %",
            # Valuation re-rating
            "PE at Start (TTM)", "PE at End (TTM)", "PE Re-rating %",
            "52w High (at end)", "% from 52w High",
            # Business profile
            "ROCE %", "Promoter Holding %", "Debt/Equity",
            "Sales Growth 3Y CAGR %", "Profit Growth TTM %",
            "Listing Date", "Top 3 Segments",
            # In-window catalysts
            "In-Window Result Period", "In-Window Result Filing Date",
            "In-Window Revenue (Cr)", "In-Window Revenue YoY %",
            "In-Window OPM %", "In-Window Net Profit (Cr)",
            "In-Window PAT YoY %", "In-Window EPS",
            "In-Window Filings Count", "In-Window Top Filings",
            "In-Window Corporate Actions",
        ]
        for n in (1, 2, 3):
            header.extend([
                f"Q-{n} Period",
                f"Q-{n} Revenue (Cr)",
                f"Q-{n} Op Profit (Cr)",
                f"Q-{n} OPM %",
                f"Q-{n} Net Profit (Cr)",
                f"Q-{n} EPS",
            ])
        header.extend([
            # External lookup hooks
            "ISIN", "BSE Code", "NSE Code", "Website",
            "BSE Announcements URL", "Screener URL",
        ])
        writer.writerow(header)

        smallcap_roc_val = smallcap_roc if smallcap_roc is not None else None

        for i, r in enumerate(rows, 1):
            sid = r["security_id"]
            fund = fundamentals_map.get(sid, {})
            quarters_all = list(all_quarters_map.get(sid, []))

            # Pre-move snapshot: 3 quarters with period_end_date <= from_date
            pre_move = [q for q in quarters_all if q.get("period_end_date") and q["period_end_date"] <= from_dt][:3]
            while len(pre_move) < 3:
                pre_move.append({})

            # In-window result: latest quarter where filing_date is in [from, to]
            in_window = next(
                (q for q in quarters_all
                 if q.get("filing_date") and from_dt <= q["filing_date"] <= to_dt),
                None,
            )
            # YoY counterpart for the in-window result
            yoy_rev_pct = yoy_pat_pct = None
            if in_window:
                yoy_str = _yoy_period(in_window.get("period") or "")
                yoy_q = None
                if yoy_str:
                    yoy_q = next((q for q in quarters_all if q.get("period") == yoy_str), None)
                if not yoy_q and in_window.get("period_end_date"):
                    target = in_window["period_end_date"] - timedelta(days=365)
                    yoy_q = min(
                        (q for q in quarters_all
                         if q.get("period_end_date")
                         and abs((q["period_end_date"] - target).days) <= 20
                         and q["period_end_date"] != in_window["period_end_date"]),
                        key=lambda q: abs((q["period_end_date"] - target).days),
                        default=None,
                    )
                if yoy_q:
                    rev_now = in_window.get("revenue_cr")
                    rev_yoy = yoy_q.get("revenue_cr")
                    if rev_now is not None and rev_yoy not in (None, 0):
                        yoy_rev_pct = round((float(rev_now) - float(rev_yoy)) / float(rev_yoy) * 100, 2)
                    pat_now = in_window.get("net_profit_cr")
                    pat_yoy = yoy_q.get("net_profit_cr")
                    if pat_now is not None and pat_yoy not in (None, 0):
                        # When prior PAT is negative, sign flip makes % meaningless
                        if float(pat_yoy) > 0:
                            yoy_pat_pct = round((float(pat_now) - float(pat_yoy)) / float(pat_yoy) * 100, 2)

            # Re-rating: TTM EPS at start vs at end
            ttm_start = _ttm_eps(quarters_all, from_dt)
            ttm_end = _ttm_eps(quarters_all, to_dt)
            start_close = float(r["start_close"]) if r.get("start_close") is not None else None
            end_close = float(r["end_close"]) if r.get("end_close") is not None else None
            pe_start = round(start_close / ttm_start, 2) if ttm_start and ttm_start > 0 and start_close else None
            pe_end = round(end_close / ttm_end, 2) if ttm_end and ttm_end > 0 and end_close else None
            pe_rerating_pct = None
            if pe_start and pe_end:
                pe_rerating_pct = round((pe_end - pe_start) / pe_start * 100, 2)

            # 52w high context
            high_52w = high_52w_map.get(sid)
            pct_from_high = None
            if high_52w and end_close:
                pct_from_high = round((end_close / high_52w - 1) * 100, 2)

            # Cohort returns + alpha
            sector_key = sub_sector_keys.get(sid) or ""
            theme_key = theme_keys.get(sid) or ""
            sector_cohort = sector_cohort_map.get(sector_key) if sector_key else None
            theme_cohort = theme_cohort_map.get(theme_key) if theme_key else None
            stock_roc = float(r["roc"]) if r.get("roc") is not None else None
            alpha_vs_smallcap = None
            if stock_roc is not None and smallcap_roc_val is not None:
                alpha_vs_smallcap = round(stock_roc - smallcap_roc_val, 2)

            # Peers padded to 3
            peer_rows = list(peers_map.get(sid, []))[:3]
            while len(peer_rows) < 3:
                peer_rows.append({})

            # Top segments string ("Defence 60%; Railways 25%; Exports 15%")
            seg_rows = list(segments_map.get(sid, []))[:3]
            seg_strs = []
            for s in seg_rows:
                name = clean_segment_name((s.get("segment_name") or "").strip())
                if not name:
                    continue
                pct = s.get("segment_revenue_pct")
                if pct is not None:
                    try:
                        seg_strs.append(f"{name} {round(float(pct), 1)}%")
                    except (TypeError, ValueError):
                        seg_strs.append(name)
                else:
                    seg_strs.append(name)
            segments_str = "; ".join(seg_strs)

            # Sub-sector / wave display ("Defence Manufacturing / Capex Cycle")
            sub_sector_disp = sub_sector_keys.get(sid) or ""
            wave_disp = wave_map.get(sid) or ""
            sub_sector_wave = " / ".join(p for p in (sub_sector_disp, wave_disp) if p)

            # Filings string (top 5, "YYYY-MM-DD: TYPE — description")
            filings_rows = list(filings_map.get(sid, []))
            filing_strs = []
            for f in filings_rows[:5]:
                fd = f.get("filing_date")
                fdate = fd.isoformat() if fd else ""
                ftype = (f.get("filing_type") or "").replace("_", " ")
                desc = (f.get("description") or "").strip().replace("\n", " ").replace(";", ",")
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                bits = [b for b in (fdate, ftype, desc) if b]
                if bits:
                    filing_strs.append(": ".join(bits[:2]) + (" — " + bits[2] if len(bits) > 2 else ""))
            filings_str = " | ".join(filing_strs)
            filings_count = filings_rows[0].get("total_count") if filings_rows else 0

            # Corporate actions string
            ca_rows = list(corp_actions_map.get(sid, []))
            ca_strs = []
            for ca in ca_rows:
                a_type = ca.get("action_type") or ""
                ex = ca.get("ex_date") or ca.get("action_date")
                ex_str = ex.isoformat() if ex else ""
                detail = ca.get("details") or ca.get("bonus_ratio") or ca.get("split_ratio") or ""
                if ca.get("dividend_amount") and not detail:
                    detail = f"₹{ca['dividend_amount']}"
                bits = [b for b in (a_type, ex_str, str(detail) if detail else "") if b]
                if bits:
                    ca_strs.append(" ".join(bits))
            corp_actions_str = "; ".join(ca_strs)

            # External lookup URLs (only if we have the codes)
            bse_code = fund.get("bse_code") or ""
            nse_code = fund.get("nse_code") or r.get("symbol") or ""
            bse_url = (
                f"https://www.bseindia.com/corporates/ann.html?scrip={bse_code}&dur=A&expandable=0"
                if bse_code else ""
            )
            screener_slug = (nse_code or bse_code or r.get("symbol") or "").upper()
            screener_url = f"https://www.screener.in/company/{screener_slug}/consolidated/" if screener_slug else ""

            row_out = [
                # Identity
                i,
                r.get("symbol") or "",
                r.get("company_name") or "",
                fund.get("industry") or r.get("industry") or "",
                sector_map.get(sid) or r.get("sector") or "",
                sub_sector_wave,
                theme_map.get(sid) or "",
                (fund.get("about") or "").replace("\n", " ").strip()[:500],
                # Move
                _fmt_num(r.get("initial_mcap_cr")),
                r["start_date"].isoformat() if r.get("start_date") else "",
                r["end_date"].isoformat() if r.get("end_date") else "",
                _fmt_num(r.get("start_close")),
                _fmt_num(r.get("end_close")),
                _fmt_num(r.get("max_close")),
                _fmt_num(r.get("roc")),
                _fmt_num(r.get("max_roc")),
                r.get("trading_days") or 0,
                _fmt_num(r.get("avg_turnover_cr")),
                # Alpha
                _fmt_num(smallcap_roc_val),
                _fmt_num(sector_cohort.get("avg_roc")) if sector_cohort else "",
                sector_cohort.get("n") if sector_cohort else "",
                _fmt_num(theme_cohort.get("avg_roc")) if theme_cohort else "",
                theme_cohort.get("n") if theme_cohort else "",
                _fmt_num(alpha_vs_smallcap),
                peer_rows[0].get("peer_symbol") or "",
                _fmt_num(peer_rows[0].get("peer_roc")),
                peer_rows[1].get("peer_symbol") or "",
                _fmt_num(peer_rows[1].get("peer_roc")),
                peer_rows[2].get("peer_symbol") or "",
                _fmt_num(peer_rows[2].get("peer_roc")),
                # Valuation
                _fmt_num(pe_start),
                _fmt_num(pe_end),
                _fmt_num(pe_rerating_pct),
                _fmt_num(high_52w),
                _fmt_num(pct_from_high),
                # Business profile
                _fmt_num(fund.get("roce")),
                _fmt_num(fund.get("promoter_holding_pct")),
                _fmt_num(fund.get("debt_to_equity")),
                _fmt_num(fund.get("sales_growth_3yr_cagr")),
                _fmt_num(fund.get("profit_growth_ttm")),
                fund.get("listing_date").isoformat() if fund.get("listing_date") else "",
                segments_str,
                # In-window catalysts
                in_window.get("period") if in_window else "",
                in_window["filing_date"].isoformat() if in_window and in_window.get("filing_date") else "",
                _fmt_num(in_window.get("revenue_cr")) if in_window else "",
                _fmt_num(yoy_rev_pct),
                _fmt_num(in_window.get("opm_percent")) if in_window else "",
                _fmt_num(in_window.get("net_profit_cr")) if in_window else "",
                _fmt_num(yoy_pat_pct),
                _fmt_num(in_window.get("eps")) if in_window else "",
                filings_count or 0,
                filings_str,
                corp_actions_str,
            ]
            for q in pre_move:
                row_out.extend([
                    q.get("period") or "",
                    _fmt_num(q.get("revenue_cr")),
                    _fmt_num(q.get("operating_profit_cr")),
                    _fmt_num(q.get("opm_percent")),
                    _fmt_num(q.get("net_profit_cr")),
                    _fmt_num(q.get("eps")),
                ])
            row_out.extend([
                fund.get("isin") or "",
                bse_code,
                nse_code,
                fund.get("website") or "",
                bse_url,
                screener_url,
            ])
            writer.writerow(row_out)

        # UTF-8 BOM so Excel recognises the encoding.
        csv_bytes = b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")

        try:
            log_admin_action(
                g.user_id,
                "past_winners_export",
                details={
                    "from_date": from_date,
                    "to_date": to_date,
                    "row_count": len(rows),
                    "filtered": bool(security_ids),
                },
            )
        except Exception:
            pass

        filename = f"past_winners_{from_date}_to_{to_date}.csv"
        return Response(
            csv_bytes,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    except Exception as e:
        err_msg = str(e)
        if "cancel" in err_msg.lower() or "timeout" in err_msg.lower():
            return jsonify({"error": "Export timed out. Try a shorter date range."}), 504
        print(f"[past_winners/export] error: {err_msg}")
        return jsonify({"error": err_msg}), 500
    finally:
        close_db(conn)
