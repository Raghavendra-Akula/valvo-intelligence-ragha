"""
Earnings Tracker — quarterly results dashboard sliced by Sector / Sub-sector / Wave / Theme.

Mirrors multibagg.ai/earnings-tracker but uses Valvo's richer wave/theme classification
as the differentiator. Reads from financials_quarterly + forthcoming_results + filings,
joined to bse_company_master / stock_themes_v2 / stock_custom_sector / index_constituents.

Period bucketing is derived from period_end_date — financials_quarterly.period is
uniformly the literal string "Quarterly" and cannot be used.
"""
from datetime import date, datetime
from functools import lru_cache
import time

from flask import Blueprint, jsonify, request

from extensions import limiter
from database.database import get_db, close_db

earnings_tracker_bp = Blueprint("earnings_tracker", __name__)

NIFTY500_INDEX_SYMBOL = "NIFTY 500"


# ═════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════

def _fy_q_to_dates(fy: str, q: str) -> tuple[date, date]:
    """Indian FY: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (year-of-end).
    fy26 q4 -> (2026-01-01, 2026-03-31)
    fy26 q1 -> (2025-04-01, 2025-06-30)
    """
    yr = int(fy[-2:]) + 2000
    fy = fy.lower()
    q = q.lower()
    starts = {"q1": (yr - 1, 4, 1),  "q2": (yr - 1, 7, 1),  "q3": (yr - 1, 10, 1), "q4": (yr, 1, 1)}
    ends   = {"q1": (yr - 1, 6, 30), "q2": (yr - 1, 9, 30), "q3": (yr - 1, 12, 31),"q4": (yr, 3, 31)}
    return date(*starts[q]), date(*ends[q])


def _yoy_dates(fy: str, q: str) -> tuple[date, date]:
    """Same Q one year earlier."""
    fy_yr = int(fy[-2:])
    prev = f"fy{fy_yr - 1}"
    return _fy_q_to_dates(prev, q)


def _qoq_dates(fy: str, q: str) -> tuple[date, date]:
    """The previous calendar quarter."""
    order = ["q1", "q2", "q3", "q4"]
    yr = int(fy[-2:])
    idx = order.index(q.lower())
    if idx == 0:
        prev_fy, prev_q = f"fy{yr - 1}", "q4"
    else:
        prev_fy, prev_q = fy, order[idx - 1]
    return _fy_q_to_dates(prev_fy, prev_q)


def _period_label(fy: str, q: str) -> str:
    yr = int(fy[-2:])
    return f"{q.upper()} FY{yr}"


def _current_fy_q(today: date | None = None) -> tuple[str, str]:
    """The fiscal quarter that's currently being reported.
    During earnings season for Q4 (Apr-Jun reports), today.month=4-6 -> Q4 of previous CY's FY.
    """
    today = today or date.today()
    m, y = today.month, today.year
    if m >= 4 and m <= 6:
        # Q4 results being reported (Jan-Mar quarter, FY ends in current year)
        return f"fy{y % 100:02d}", "q4"
    if m >= 7 and m <= 9:
        # Q1 results (Apr-Jun, FY ends next year)
        return f"fy{(y + 1) % 100:02d}", "q1"
    if m >= 10 and m <= 12:
        # Q2 results (Jul-Sep)
        return f"fy{(y + 1) % 100:02d}", "q2"
    # Jan-Mar -> Q3 results (Oct-Dec, FY ending current year)
    return f"fy{y % 100:02d}", "q3"


def _serialize(v):
    """JSON-safe coercion for psycopg row values."""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    return v


def _safe_pct(cur_val, prev_val) -> float | None:
    """Compute percent change, guarded against division by ~0 / negative bases."""
    try:
        if cur_val is None or prev_val is None:
            return None
        prev = float(prev_val)
        cur = float(cur_val)
        if abs(prev) < 1e-6:
            return None
        return round((cur - prev) / abs(prev) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _index_clause(index_filter: str) -> tuple[str, list]:
    """Return SQL fragment + params for restricting universe to an index."""
    if not index_filter or index_filter.upper() == "ALL":
        return "", []
    return (
        "AND security_id IN (SELECT security_id FROM index_constituents WHERE index_symbol = %s) ",
        [NIFTY500_INDEX_SYMBOL if index_filter.upper() == "NIFTY500" else index_filter],
    )


def _ttl_bucket(seconds: int = 300) -> int:
    """Bucket key that changes every `seconds` so lru_cache evicts naturally."""
    return int(time.time() // seconds)


# ═════════════════════════════════════════════════════════
# 1. PERIODS — list of last 8 quarters with declared counts + current
# ═════════════════════════════════════════════════════════

@earnings_tracker_bp.route("/api/earnings/periods", methods=["GET"])
@limiter.limit("60 per minute")
def get_periods():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT period_end_date, COUNT(*) AS declared
              FROM financials_quarterly
             WHERE period_end_date IS NOT NULL
             GROUP BY period_end_date
             ORDER BY period_end_date DESC
             LIMIT 12
        """)
        rows = cur.fetchall()
        periods = []
        for r in rows:
            d = r["period_end_date"]
            fy, q = _date_to_fy_q(d)
            periods.append({
                "fy": fy, "q": q,
                "label": _period_label(fy, q),
                "period_end_date": str(d),
                "declared": r["declared"],
            })
        cur_fy, cur_q = _current_fy_q()
        return jsonify({
            "current": {"fy": cur_fy, "q": cur_q, "label": _period_label(cur_fy, cur_q)},
            "periods": periods,
        })
    finally:
        close_db(conn)


def _date_to_fy_q(d: date) -> tuple[str, str]:
    """Inverse of _fy_q_to_dates — given period_end_date, derive fy/q."""
    m = d.month
    if m == 6:  return f"fy{(d.year + 1) % 100:02d}", "q1"
    if m == 9:  return f"fy{(d.year + 1) % 100:02d}", "q2"
    if m == 12: return f"fy{(d.year + 1) % 100:02d}", "q3"
    if m == 3:  return f"fy{d.year % 100:02d}", "q4"
    # Fallback (shouldn't happen for clean quarter-end dates)
    return f"fy{d.year % 100:02d}", "q?"


# ═════════════════════════════════════════════════════════
# 2. SUMMARY — KPI tiles for the active period
# ═════════════════════════════════════════════════════════

@earnings_tracker_bp.route("/api/earnings/summary", methods=["GET"])
@limiter.limit("60 per minute")
def get_summary():
    fy = (request.args.get("fy") or "").lower().strip()
    q = (request.args.get("q") or "").lower().strip()
    index_filter = (request.args.get("index") or "ALL").strip().upper()
    if not fy or not q:
        return jsonify({"error": "fy and q are required"}), 400

    try:
        _fy_q_to_dates(fy, q)
    except (KeyError, ValueError):
        return jsonify({"error": "invalid fy/q"}), 400

    return jsonify(_summary_cached(fy, q, index_filter, _ttl_bucket()))


@lru_cache(maxsize=64)
def _summary_cached(fy: str, q: str, index_filter: str, _bucket: int) -> dict:
    period_start, period_end = _fy_q_to_dates(fy, q)
    yoy_start, yoy_end = _yoy_dates(fy, q)
    idx_sql, idx_params = _index_clause(index_filter)
    universe_sql, universe_params = _index_clause(index_filter)

    conn = get_db()
    try:
        cur = conn.cursor()

        # Pick the latest row per (security_id, is_consolidated) in the window — many
        # companies file revisions across days.
        cur.execute(f"""
            WITH cur AS (
              SELECT DISTINCT ON (security_id)
                     security_id, revenue_cr, net_profit_cr, is_consolidated
                FROM financials_quarterly
               WHERE period_end_date BETWEEN %s AND %s
                 AND revenue_cr IS NOT NULL
                 {idx_sql}
               ORDER BY security_id,
                        is_consolidated DESC,
                        filing_date DESC NULLS LAST,
                        period_end_date DESC
            ),
            prev AS (
              SELECT DISTINCT ON (security_id, is_consolidated)
                     security_id, is_consolidated, revenue_cr, net_profit_cr
                FROM financials_quarterly
               WHERE period_end_date BETWEEN %s AND %s
                 AND revenue_cr IS NOT NULL
               ORDER BY security_id, is_consolidated, filing_date DESC NULLS LAST
            )
            SELECT
              COUNT(*)::int AS declared_count,
              AVG(c.revenue_cr)::float AS avg_revenue_cr,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY CASE WHEN p.revenue_cr > 0 AND c.revenue_cr IS NOT NULL
                              THEN ((c.revenue_cr - p.revenue_cr) / p.revenue_cr * 100.0) END
              )::float AS sales_yoy_median,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY CASE WHEN ABS(p.net_profit_cr) > 1 AND c.net_profit_cr IS NOT NULL
                              THEN ((c.net_profit_cr - p.net_profit_cr) / ABS(p.net_profit_cr) * 100.0) END
              )::float AS profit_yoy_median
              FROM cur c
              LEFT JOIN prev p
                ON p.security_id = c.security_id
               AND p.is_consolidated = c.is_consolidated
        """, [period_start, period_end] + idx_params + [yoy_start, yoy_end])
        agg = cur.fetchone()

        cur.execute(f"SELECT COUNT(DISTINCT security_id)::int AS n FROM stock_universe WHERE is_active = true {universe_sql}", universe_params)
        total_universe = cur.fetchone()["n"]

        cur.execute("""
            SELECT MAX(GREATEST(filing_date::timestamp, updated_at)) AS last_updated
              FROM financials_quarterly
             WHERE period_end_date BETWEEN %s AND %s
        """, [period_start, period_end])
        last_updated_row = cur.fetchone()
        last_updated = last_updated_row["last_updated"] if last_updated_row else None

        return {
            "fy": fy, "q": q,
            "period_label": _period_label(fy, q),
            "period_start": str(period_start),
            "period_end": str(period_end),
            "declared_count": agg["declared_count"] or 0,
            "total_universe": total_universe or 0,
            "sales_growth_yoy_median": _serialize(agg["sales_yoy_median"]),
            "profit_growth_yoy_median": _serialize(agg["profit_yoy_median"]),
            "avg_revenue_cr": _serialize(agg["avg_revenue_cr"]),
            "last_updated": _serialize(last_updated),
            "index": index_filter.upper(),
        }
    finally:
        close_db(conn)


# ═════════════════════════════════════════════════════════
# 3. BY-SLICE — the differentiator. Group by sector / sub-sector / wave / theme
# ═════════════════════════════════════════════════════════

VALID_SLICES = {"sector", "sub_sector", "wave", "theme"}


@earnings_tracker_bp.route("/api/earnings/by-slice", methods=["GET"])
@limiter.limit("60 per minute")
def get_by_slice():
    fy = (request.args.get("fy") or "").lower().strip()
    q = (request.args.get("q") or "").lower().strip()
    slice_by = (request.args.get("slice") or "sector").lower().strip()
    index_filter = (request.args.get("index") or "ALL").strip().upper()
    if not fy or not q:
        return jsonify({"error": "fy and q are required"}), 400
    if slice_by not in VALID_SLICES:
        return jsonify({"error": f"slice must be one of {sorted(VALID_SLICES)}"}), 400
    try:
        _fy_q_to_dates(fy, q)
    except (KeyError, ValueError):
        return jsonify({"error": "invalid fy/q"}), 400

    return jsonify(_by_slice_cached(fy, q, slice_by, index_filter, _ttl_bucket()))


@lru_cache(maxsize=128)
def _by_slice_cached(fy: str, q: str, slice_by: str, index_filter: str, _bucket: int) -> dict:
    period_start, period_end = _fy_q_to_dates(fy, q)
    yoy_start, yoy_end = _yoy_dates(fy, q)
    qoq_start, qoq_end = _qoq_dates(fy, q)
    idx_sql, idx_params = _index_clause(index_filter)
    universe_sql, universe_params = _index_clause(index_filter)

    # Build slice-specific JOIN + key/name SQL
    slice_join, key_expr, name_expr, accent_expr, group_extra = _slice_sql(slice_by)

    conn = get_db()
    try:
        cur = conn.cursor()

        # Universe per slice — total companies that COULD report
        cur.execute(f"""
            SELECT {key_expr} AS k, {name_expr} AS name, {accent_expr} AS accent,
                   COUNT(DISTINCT su.security_id)::int AS total
              FROM stock_universe su
              {slice_join}
             WHERE su.is_active = true
               {universe_sql}
             GROUP BY {group_extra}
        """, universe_params)
        universe_rows = {r["k"]: r for r in cur.fetchall() if r["k"] is not None}

        # Declared per slice + growth medians + top performer
        cur.execute(f"""
            WITH cur AS (
              SELECT DISTINCT ON (fq.security_id)
                     fq.security_id, fq.revenue_cr, fq.net_profit_cr, fq.is_consolidated, fq.symbol
                FROM financials_quarterly fq
               WHERE fq.period_end_date BETWEEN %s AND %s
                 AND fq.revenue_cr IS NOT NULL
                 {idx_sql}
               ORDER BY fq.security_id,
                        fq.is_consolidated DESC,
                        fq.filing_date DESC NULLS LAST,
                        fq.period_end_date DESC
            ),
            prev_yoy AS (
              SELECT DISTINCT ON (security_id, is_consolidated)
                     security_id, is_consolidated, revenue_cr, net_profit_cr
                FROM financials_quarterly
               WHERE period_end_date BETWEEN %s AND %s
                 AND revenue_cr IS NOT NULL
               ORDER BY security_id, is_consolidated, filing_date DESC NULLS LAST
            ),
            prev_qoq AS (
              SELECT DISTINCT ON (security_id, is_consolidated)
                     security_id, is_consolidated, revenue_cr, net_profit_cr
                FROM financials_quarterly
               WHERE period_end_date BETWEEN %s AND %s
                 AND revenue_cr IS NOT NULL
               ORDER BY security_id, is_consolidated, filing_date DESC NULLS LAST
            ),
            joined AS (
              SELECT c.security_id, c.symbol, c.revenue_cr, c.net_profit_cr,
                     CASE WHEN py.revenue_cr > 0 THEN ((c.revenue_cr - py.revenue_cr) / py.revenue_cr * 100.0) END AS sales_yoy,
                     CASE WHEN ABS(py.net_profit_cr) > 1 THEN ((c.net_profit_cr - py.net_profit_cr) / ABS(py.net_profit_cr) * 100.0) END AS profit_yoy,
                     CASE WHEN pq.revenue_cr > 0 THEN ((c.revenue_cr - pq.revenue_cr) / pq.revenue_cr * 100.0) END AS sales_qoq,
                     CASE WHEN ABS(pq.net_profit_cr) > 1 THEN ((c.net_profit_cr - pq.net_profit_cr) / ABS(pq.net_profit_cr) * 100.0) END AS profit_qoq
                FROM cur c
                LEFT JOIN prev_yoy py
                  ON py.security_id = c.security_id AND py.is_consolidated = c.is_consolidated
                LEFT JOIN prev_qoq pq
                  ON pq.security_id = c.security_id AND pq.is_consolidated = c.is_consolidated
            ),
            sliced AS (
              SELECT j.*, {key_expr} AS k, {name_expr} AS name, {accent_expr} AS accent
                FROM joined j
                JOIN stock_universe su USING (security_id)
                {slice_join}
            )
            SELECT k, MAX(name) AS name, MAX(accent) AS accent,
                   COUNT(*)::int AS declared,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY sales_yoy)::float AS sales_yoy_pct,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY profit_yoy)::float AS profit_yoy_pct,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY sales_qoq)::float AS sales_qoq_pct,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY profit_qoq)::float AS profit_qoq_pct,
                   (ARRAY_AGG(symbol ORDER BY sales_yoy DESC NULLS LAST))[1] AS top_symbol,
                   MAX(sales_yoy)::float AS top_sales_yoy
              FROM sliced
             WHERE k IS NOT NULL
             GROUP BY k
        """, [period_start, period_end] + idx_params + [yoy_start, yoy_end] + [qoq_start, qoq_end])

        slice_rows = cur.fetchall()

        out = []
        seen_keys = set()
        for r in slice_rows:
            k = r["k"]
            seen_keys.add(k)
            uni = universe_rows.get(k, {})
            total = uni.get("total") or 0
            declared = r["declared"] or 0
            out.append({
                "key": k,
                "name": r["name"] or uni.get("name") or k,
                "accent": r["accent"] or uni.get("accent") or "#64748b",
                "declared": declared,
                "total": total,
                "pending": max(total - declared, 0),
                "sales_yoy_pct": _serialize(r["sales_yoy_pct"]),
                "profit_yoy_pct": _serialize(r["profit_yoy_pct"]),
                "sales_qoq_pct": _serialize(r["sales_qoq_pct"]),
                "profit_qoq_pct": _serialize(r["profit_qoq_pct"]),
                "top_company": {
                    "symbol": r["top_symbol"],
                    "sales_yoy": _serialize(r["top_sales_yoy"]),
                } if r["top_symbol"] else None,
            })

        # Add slices that have a universe but zero declared yet
        for k, uni in universe_rows.items():
            if k in seen_keys:
                continue
            out.append({
                "key": k,
                "name": uni["name"] or k,
                "accent": uni["accent"] or "#64748b",
                "declared": 0,
                "total": uni["total"] or 0,
                "pending": uni["total"] or 0,
                "sales_yoy_pct": None, "profit_yoy_pct": None,
                "sales_qoq_pct": None, "profit_qoq_pct": None,
                "top_company": None,
            })

        out.sort(key=lambda r: (r["declared"] == 0, -(r["declared"] or 0), -(r["sales_yoy_pct"] or -1e9)))
        return {
            "fy": fy, "q": q, "slice": slice_by,
            "period_label": _period_label(fy, q),
            "rows": out,
            "index": index_filter.upper(),
        }
    finally:
        close_db(conn)


def _slice_sql(slice_by: str):
    """Return (join_sql, key_expr, name_expr, accent_expr, group_expr) for the slice axis."""
    if slice_by == "sector":
        return (
            "LEFT JOIN bse_company_master bcm ON bcm.security_id = su.security_id ",
            "COALESCE(bcm.sector, su.sector, 'Uncategorized')",
            "COALESCE(bcm.sector, su.sector, 'Uncategorized')",
            "'#64748b'::text",
            "COALESCE(bcm.sector, su.sector, 'Uncategorized')",
        )
    if slice_by == "sub_sector":
        return (
            "LEFT JOIN stock_custom_sector scs ON scs.security_id = su.security_id AND scs.is_primary = true "
            "LEFT JOIN custom_sectors cs ON cs.id = scs.custom_sector_id ",
            "COALESCE(cs.slug, 'unclassified')",
            "COALESCE(cs.name, 'Unclassified')",
            "'#64748b'::text",
            "COALESCE(cs.slug, 'unclassified'), COALESCE(cs.name, 'Unclassified')",
        )
    if slice_by == "wave":
        return (
            "LEFT JOIN stock_themes_v2 st ON st.security_id = su.security_id AND st.is_primary = true "
            "LEFT JOIN themes_v2 t ON t.slug = st.theme_slug "
            "LEFT JOIN waves_v2 w ON w.slug = t.wave_slug ",
            "w.slug",
            "w.name",
            "COALESCE(w.accent_color, '#64748b')",
            "w.slug, w.name, w.accent_color",
        )
    if slice_by == "theme":
        return (
            "LEFT JOIN stock_themes_v2 st ON st.security_id = su.security_id AND st.is_primary = true "
            "LEFT JOIN themes_v2 t ON t.slug = st.theme_slug "
            "LEFT JOIN waves_v2 w ON w.slug = t.wave_slug ",
            "t.slug",
            "t.name",
            "COALESCE(t.accent_color, w.accent_color, '#64748b')",
            "t.slug, t.name, t.accent_color, w.accent_color",
        )
    raise ValueError(f"unknown slice {slice_by}")


# ═════════════════════════════════════════════════════════
# 4. DECLARED — list of reported companies (Declared grid + Leaderboard)
# ═════════════════════════════════════════════════════════

VALID_SORTS = {
    "sales_yoy_desc": "sales_yoy DESC NULLS LAST",
    "sales_yoy_asc":  "sales_yoy ASC NULLS LAST",
    "profit_yoy_desc": "profit_yoy DESC NULLS LAST",
    "profit_yoy_asc":  "profit_yoy ASC NULLS LAST",
    "filing_date_desc": "filing_date DESC NULLS LAST",
    "revenue_desc":    "revenue_cr DESC NULLS LAST",
}


@earnings_tracker_bp.route("/api/earnings/declared", methods=["GET"])
@limiter.limit("60 per minute")
def get_declared():
    fy = (request.args.get("fy") or "").lower().strip()
    q = (request.args.get("q") or "").lower().strip()
    index_filter = (request.args.get("index") or "ALL").strip()
    slice_by = (request.args.get("slice") or "").lower().strip()
    slice_key = (request.args.get("slice_key") or "").strip()
    sort = (request.args.get("sort") or "filing_date_desc").lower().strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 500))
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return jsonify({"error": "invalid limit/offset"}), 400
    if not fy or not q:
        return jsonify({"error": "fy and q are required"}), 400
    if sort not in VALID_SORTS:
        return jsonify({"error": f"sort must be one of {sorted(VALID_SORTS)}"}), 400
    try:
        period_start, period_end = _fy_q_to_dates(fy, q)
        yoy_start, yoy_end = _yoy_dates(fy, q)
    except (KeyError, ValueError):
        return jsonify({"error": "invalid fy/q"}), 400

    idx_sql, idx_params = _index_clause(index_filter)

    # Slice-key filter
    slice_filter_sql = ""
    slice_filter_params: list = []
    if slice_by and slice_key:
        if slice_by not in VALID_SLICES:
            return jsonify({"error": f"slice must be one of {sorted(VALID_SLICES)}"}), 400
        if slice_by == "sector":
            slice_filter_sql = (
                "AND fq.security_id IN ("
                "  SELECT security_id FROM stock_universe su "
                "  LEFT JOIN bse_company_master bcm ON bcm.security_id = su.security_id "
                "  WHERE COALESCE(bcm.sector, su.sector, 'Uncategorized') = %s) "
            )
            slice_filter_params = [slice_key]
        elif slice_by == "sub_sector":
            slice_filter_sql = (
                "AND fq.security_id IN ("
                "  SELECT scs.security_id FROM stock_custom_sector scs "
                "  JOIN custom_sectors cs ON cs.id = scs.custom_sector_id "
                "  WHERE scs.is_primary = true AND cs.slug = %s) "
            )
            slice_filter_params = [slice_key]
        elif slice_by == "wave":
            slice_filter_sql = (
                "AND fq.security_id IN ("
                "  SELECT st.security_id FROM stock_themes_v2 st "
                "  JOIN themes_v2 t ON t.slug = st.theme_slug "
                "  WHERE st.is_primary = true AND t.wave_slug = %s) "
            )
            slice_filter_params = [slice_key]
        elif slice_by == "theme":
            slice_filter_sql = (
                "AND fq.security_id IN ("
                "  SELECT st.security_id FROM stock_themes_v2 st "
                "  WHERE st.is_primary = true AND st.theme_slug = %s) "
            )
            slice_filter_params = [slice_key]

    sort_sql = VALID_SORTS[sort]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            WITH cur AS (
              SELECT DISTINCT ON (fq.security_id)
                     fq.security_id, fq.symbol, fq.revenue_cr, fq.net_profit_cr,
                     fq.eps, fq.is_consolidated, fq.filing_date,
                     fq.bse_filing_id, fq.source_url
                FROM financials_quarterly fq
               WHERE fq.period_end_date BETWEEN %s AND %s
                 AND fq.revenue_cr IS NOT NULL
                 {idx_sql}
                 {slice_filter_sql}
               ORDER BY fq.security_id,
                        fq.is_consolidated DESC,
                        fq.filing_date DESC NULLS LAST,
                        fq.period_end_date DESC
            ),
            prev_yoy AS (
              SELECT DISTINCT ON (security_id, is_consolidated)
                     security_id, is_consolidated, revenue_cr, net_profit_cr, eps
                FROM financials_quarterly
               WHERE period_end_date BETWEEN %s AND %s
                 AND revenue_cr IS NOT NULL
               ORDER BY security_id, is_consolidated, filing_date DESC NULLS LAST
            )
            SELECT c.security_id, c.symbol, c.revenue_cr, c.net_profit_cr,
                   c.eps, c.is_consolidated, c.filing_date,
                   c.bse_filing_id, c.source_url,
                   su.company_name, COALESCE(bcm.sector, su.sector) AS sector,
                   py.revenue_cr AS prev_revenue_cr, py.net_profit_cr AS prev_net_profit_cr,
                   CASE WHEN py.revenue_cr > 0 THEN ((c.revenue_cr - py.revenue_cr) / py.revenue_cr * 100.0) END AS sales_yoy,
                   CASE WHEN ABS(py.net_profit_cr) > 1 THEN ((c.net_profit_cr - py.net_profit_cr) / ABS(py.net_profit_cr) * 100.0) END AS profit_yoy,
                   (SELECT pdf_url FROM filings f
                     WHERE f.security_id = c.security_id
                       AND f.filing_type = 'QUARTERLY_RESULT'
                       AND f.filing_date >= %s
                     ORDER BY f.filing_date DESC NULLS LAST LIMIT 1) AS pdf_url,
                   (SELECT t.slug FROM stock_themes_v2 st JOIN themes_v2 t ON t.slug = st.theme_slug
                     WHERE st.security_id = c.security_id AND st.is_primary = true
                     LIMIT 1) AS theme_slug,
                   (SELECT w.slug FROM stock_themes_v2 st
                       JOIN themes_v2 t ON t.slug = st.theme_slug
                       JOIN waves_v2 w ON w.slug = t.wave_slug
                     WHERE st.security_id = c.security_id AND st.is_primary = true
                     LIMIT 1) AS wave_slug,
                   (SELECT w.accent_color FROM stock_themes_v2 st
                       JOIN themes_v2 t ON t.slug = st.theme_slug
                       JOIN waves_v2 w ON w.slug = t.wave_slug
                     WHERE st.security_id = c.security_id AND st.is_primary = true
                     LIMIT 1) AS wave_accent
              FROM cur c
              JOIN stock_universe su ON su.security_id = c.security_id
              LEFT JOIN bse_company_master bcm ON bcm.security_id = c.security_id
              LEFT JOIN prev_yoy py
                ON py.security_id = c.security_id AND py.is_consolidated = c.is_consolidated
             ORDER BY {sort_sql}
             LIMIT %s OFFSET %s
        """,
        [period_start, period_end] + idx_params + slice_filter_params
        + [yoy_start, yoy_end, period_end, limit, offset])

        rows = cur.fetchall()

        out = []
        for r in rows:
            out.append({
                "symbol": r["symbol"],
                "company_name": r["company_name"],
                "sector": r["sector"],
                "wave_slug": r["wave_slug"],
                "theme_slug": r["theme_slug"],
                "accent": r["wave_accent"] or "#64748b",
                "is_consolidated": r["is_consolidated"],
                "filing_date": _serialize(r["filing_date"]),
                "revenue_cr": _serialize(r["revenue_cr"]),
                "prev_revenue_cr": _serialize(r["prev_revenue_cr"]),
                "net_profit_cr": _serialize(r["net_profit_cr"]),
                "prev_net_profit_cr": _serialize(r["prev_net_profit_cr"]),
                "eps": _serialize(r["eps"]),
                "sales_yoy_pct": _serialize(r["sales_yoy"]),
                "profit_yoy_pct": _serialize(r["profit_yoy"]),
                "pdf_url": r["pdf_url"] or r["source_url"],
                "bse_filing_id": r["bse_filing_id"],
            })

        # Total count for pagination
        cur.execute(f"""
            SELECT COUNT(*)::int AS n FROM (
              SELECT DISTINCT ON (fq.security_id) fq.security_id
                FROM financials_quarterly fq
               WHERE fq.period_end_date BETWEEN %s AND %s
                 AND fq.revenue_cr IS NOT NULL
                 {idx_sql}
                 {slice_filter_sql}
               ORDER BY fq.security_id, fq.is_consolidated DESC, fq.filing_date DESC NULLS LAST
            ) z
        """, [period_start, period_end] + idx_params + slice_filter_params)
        total = cur.fetchone()["n"]

        return jsonify({
            "fy": fy, "q": q,
            "period_label": _period_label(fy, q),
            "rows": out,
            "total": total,
            "limit": limit, "offset": offset,
            "sort": sort, "index": index_filter.upper(),
            "slice": slice_by or None, "slice_key": slice_key or None,
        })
    finally:
        close_db(conn)


# ═════════════════════════════════════════════════════════
# 5. UPCOMING — board meetings declaring results in next N days
# ═════════════════════════════════════════════════════════

@earnings_tracker_bp.route("/api/earnings/upcoming", methods=["GET"])
@limiter.limit("60 per minute")
def get_upcoming():
    fy = (request.args.get("fy") or "").lower().strip()
    q = (request.args.get("q") or "").lower().strip()
    index_filter = (request.args.get("index") or "ALL").strip()
    try:
        days = max(1, min(int(request.args.get("days") or 21), 90))
    except ValueError:
        return jsonify({"error": "invalid days"}), 400

    period_start = period_end = None
    if fy and q:
        try:
            period_start, period_end = _fy_q_to_dates(fy, q)
        except (KeyError, ValueError):
            return jsonify({"error": "invalid fy/q"}), 400

    idx_filter_sql = ""
    idx_filter_params: list = []
    if index_filter and index_filter.upper() != "ALL":
        idx_filter_sql = (
            "AND fr.security_id IN (SELECT security_id FROM index_constituents WHERE index_symbol = %s) "
        )
        idx_filter_params = [
            NIFTY500_INDEX_SYMBOL if index_filter.upper() == "NIFTY500" else index_filter
        ]

    declared_filter = ""
    declared_params: list = []
    if period_start and period_end:
        declared_filter = (
            "AND (fr.security_id IS NULL OR fr.security_id NOT IN ("
            "  SELECT security_id FROM financials_quarterly "
            "  WHERE period_end_date BETWEEN %s AND %s)) "
        )
        declared_params = [period_start, period_end]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT fr.security_id, fr.symbol, fr.long_name AS company_name,
                   fr.meeting_date, fr.purpose, fr.bse_code,
                   COALESCE(bcm.sector, su.sector) AS sector,
                   (SELECT t.slug FROM stock_themes_v2 st JOIN themes_v2 t ON t.slug = st.theme_slug
                     WHERE st.security_id = fr.security_id AND st.is_primary = true
                     LIMIT 1) AS theme_slug,
                   (SELECT w.slug FROM stock_themes_v2 st
                       JOIN themes_v2 t ON t.slug = st.theme_slug
                       JOIN waves_v2 w ON w.slug = t.wave_slug
                     WHERE st.security_id = fr.security_id AND st.is_primary = true
                     LIMIT 1) AS wave_slug,
                   (SELECT w.accent_color FROM stock_themes_v2 st
                       JOIN themes_v2 t ON t.slug = st.theme_slug
                       JOIN waves_v2 w ON w.slug = t.wave_slug
                     WHERE st.security_id = fr.security_id AND st.is_primary = true
                     LIMIT 1) AS wave_accent
              FROM forthcoming_results fr
              LEFT JOIN stock_universe su ON su.security_id = fr.security_id
              LEFT JOIN bse_company_master bcm ON bcm.security_id = fr.security_id
             WHERE fr.meeting_date BETWEEN CURRENT_DATE AND CURRENT_DATE + %s
               AND fr.purpose ILIKE '%%result%%'
               {idx_filter_sql}
               {declared_filter}
             ORDER BY fr.meeting_date ASC, fr.long_name ASC
             LIMIT 500
        """, [days] + idx_filter_params + declared_params)
        rows = cur.fetchall()

        out = []
        for r in rows:
            out.append({
                "symbol": r["symbol"],
                "company_name": r["company_name"],
                "sector": r["sector"],
                "wave_slug": r["wave_slug"],
                "theme_slug": r["theme_slug"],
                "accent": r["wave_accent"] or "#64748b",
                "meeting_date": _serialize(r["meeting_date"]),
                "purpose": r["purpose"],
                "bse_code": r["bse_code"],
            })
        return jsonify({
            "fy": fy or None, "q": q or None,
            "days": days,
            "rows": out,
            "count": len(out),
            "index": index_filter.upper(),
        })
    finally:
        close_db(conn)
