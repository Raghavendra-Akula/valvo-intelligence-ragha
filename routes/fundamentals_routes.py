"""
Fundamentals routes — Company financials, shareholding, corporate actions, segments

Serves data from 7 populated Supabase tables:
  - fundamentals_overview (pre-computed daily overview)
  - financials_quarterly (29K rows, 1800 companies)
  - financials_annual (6.4K rows, 1400 companies)
  - shareholding_quarterly (37K rows, 2100 companies)
  - corporate_actions (13K rows)
  - segments_quarterly (55K rows, 978 companies)
  - bse_company_master (5K rows)
"""
from flask import Blueprint, jsonify, request, g
from extensions import limiter
from database.database import get_db, close_db
from config.settings import trading_days_until
from services.segment_name_utils import clean_segment_name as _clean_segment_name

fundamentals_bp = Blueprint("fundamentals", __name__)


# ═══════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════


def _resolve(symbol, cur):
    """Resolve symbol → (security_id, symbol, company_name, is_etf, ...) or None."""
    cur.execute("""
        SELECT security_id, symbol, company_name, shares_outstanding,
               COALESCE(is_etf, false) AS is_etf
        FROM stock_universe
        WHERE UPPER(symbol) = %s AND is_active = true
        LIMIT 1
    """, (symbol.upper().strip(),))
    return cur.fetchone()


def _fmt(val, decimals=2):
    """Format numeric value, return None if null."""
    if val is None:
        return None
    try:
        return round(float(val), decimals)
    except (ValueError, TypeError):
        return None


def _cons_filter(sid, cur):
    """Return 'true' if company has consolidated data, else 'false' for standalone.
    Many MNCs (Colgate, P&G, Honeywell) file only standalone."""
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM financials_quarterly
            WHERE security_id = %s AND is_consolidated = true AND revenue_cr IS NOT NULL
            LIMIT 1
        ) as has_cons
    """, (sid,))
    return cur.fetchone()["has_cons"]


# ═══════════════════════════════════════════════════
# 1. OVERVIEW — Company identity + key metrics
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>", methods=["GET"])
@limiter.limit("60 per minute")
def get_overview(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        sid = stock["security_id"]
        result = {
            "symbol": stock["symbol"],
            "company_name": stock["company_name"],
            "security_id": sid,
            "is_etf": bool(stock.get("is_etf")),
        }

        # Try pre-computed overview first
        cur.execute("SELECT * FROM fundamentals_overview WHERE security_id = %s", (sid,))
        overview = cur.fetchone()

        if overview:
            from decimal import Decimal
            import datetime
            def _serialize(v):
                if v is None: return None
                if isinstance(v, Decimal): return float(v)
                if isinstance(v, (datetime.date, datetime.datetime)): return str(v)
                return v
            result["overview"] = {k: _serialize(v)
                                  for k, v in dict(overview).items()
                                  if k not in ("security_id",)}
        else:
            # Fallback: compute live from raw tables
            result["overview"] = _compute_overview_live(sid, stock, cur)

        # ── Always overlay fresh TTM metrics on top of the stored snapshot ──
        # The fundamentals_overview table is refreshed by an external nightly
        # ETL, so its eps_ttm / margin / TTM revenue + profit fields go stale
        # the moment a company files new quarterly results. Recompute them
        # from financials_quarterly on every request — always reflects the
        # most recent 4 reported quarters within ~5ms (indexed by security_id).
        ttm = _compute_ttm_overlay(sid, cur)
        if ttm:
            result["overview"].update(ttm)

        # Identity: merge bse_company_master + stock_universe for best coverage
        cur.execute("""
            SELECT COALESCE(su.sector, bcm.sector) as sector,
                   COALESCE(su.industry, bcm.industry, su.sector) as industry,
                   bcm.face_value, bcm.listing_date, bcm.isin, bcm.bse_code, bcm.status
            FROM stock_universe su
            LEFT JOIN bse_company_master bcm ON su.security_id = bcm.security_id
            WHERE su.security_id = %s
            LIMIT 1
        """, (sid,))
        ident = cur.fetchone()
        if ident:
            result["identity"] = dict(ident)

        # Latest price + 52w from stock_daily_summary
        cur.execute("""
            SELECT prev_close, high_52w, low_52w, liq_cr, ma50, ma200
            FROM stock_daily_summary
            WHERE security_id = %s
            LIMIT 1
        """, (sid,))
        summary = cur.fetchone()
        if summary:
            result["market"] = {k: _fmt(v) for k, v in dict(summary).items()}

        # Latest candle for current price
        cur.execute("""
            SELECT close, open, high, low, volume, date
            FROM candles_daily
            WHERE security_id = %s
            ORDER BY date DESC LIMIT 1
        """, (sid,))
        candle = cur.fetchone()
        if candle:
            result["current_price"] = _fmt(candle["close"])
            result["price_date"] = str(candle["date"])
            shares = stock.get("shares_outstanding")
            if shares and candle["close"]:
                result["market_cap_cr"] = _fmt(float(shares) * float(candle["close"]) / 1e7, 0)

        # ── Overlay fresh annual ratios (ROE, ROCE, D/E, current ratio,
        # interest coverage, dividend, book value) from the latest annual
        # filing. Same staleness story as the TTM overlay but on annual cadence:
        # when a company files FY26 results, fundamentals_overview.roe
        # still reflects FY25 until the nightly ETL runs. Refreshing from
        # financials_annual on every request closes that gap. ──
        annual = _compute_annual_overlay(sid, stock, cur, result.get("current_price"))
        if annual:
            result["overview"].update(annual)

        # Nearest upcoming results board meeting — same filter as watchlist.
        cur.execute("""
            SELECT fr.meeting_date AS next_result_date,
                   fr.purpose AS next_result_purpose,
                   (fr.meeting_date - CURRENT_DATE)::int AS next_result_days_left
            FROM forthcoming_results fr
            WHERE fr.security_id = %s
              AND fr.meeting_date >= CURRENT_DATE
              AND (fr.purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s)
            ORDER BY fr.meeting_date ASC,
                     (fr.purpose ILIKE %s) DESC,
                     (fr.purpose ILIKE %s) DESC
            LIMIT 1
        """, (sid, "%result%", "%financial result%", "%audited%", "%quarterly result%",
              "%financial result%", "%result%"))
        forth = cur.fetchone()
        if forth and forth["next_result_date"]:
            result["next_result"] = {
                "date": str(forth["next_result_date"]),
                "purpose": forth["next_result_purpose"],
                "days_left": forth["next_result_days_left"],
                "trading_days_left": trading_days_until(forth["next_result_date"]),
            }

        # Most recent past results board meeting within the last 10 days —
        # mirror of next_result so the UI can render a "RESULTS Xd ago" pill
        # whenever a print just dropped.
        cur.execute("""
            SELECT fr.meeting_date AS recent_result_date,
                   fr.purpose AS recent_result_purpose,
                   (CURRENT_DATE - fr.meeting_date)::int AS recent_result_days_ago
            FROM forthcoming_results fr
            WHERE fr.security_id = %s
              AND fr.meeting_date < CURRENT_DATE
              AND fr.meeting_date >= CURRENT_DATE - INTERVAL '10 days'
              AND (fr.purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s)
            ORDER BY fr.meeting_date DESC,
                     (fr.purpose ILIKE %s) DESC,
                     (fr.purpose ILIKE %s) DESC
            LIMIT 1
        """, (sid, "%result%", "%financial result%", "%audited%", "%quarterly result%",
              "%financial result%", "%result%"))
        recent = cur.fetchone()
        if recent and recent["recent_result_date"]:
            result["recent_result"] = {
                "date": str(recent["recent_result_date"]),
                "purpose": recent["recent_result_purpose"],
                "days_ago": recent["recent_result_days_ago"],
            }

        # Fallback for stocks with no BSE forthcoming-intimation row (smaller
        # caps that file results without an advance board-meeting notice in
        # BSE's API window). Use financials_quarterly: the date our pipeline
        # first ingested the latest quarter is a tight proxy for "results
        # dropped." Only fires when the primary lookup above didn't hit.
        if "recent_result" not in result:
            cur.execute("""
                WITH latest AS (
                    SELECT MAX(period_end_date) AS q
                    FROM financials_quarterly
                    WHERE security_id = %s
                )
                SELECT MIN(fq.created_at)::date AS reported_at,
                       (SELECT q FROM latest)::text AS period_end
                FROM financials_quarterly fq, latest
                WHERE fq.security_id = %s
                  AND fq.period_end_date = latest.q
                  AND fq.created_at >= CURRENT_DATE - INTERVAL '10 days'
            """, (sid, sid))
            fb = cur.fetchone()
            if fb and fb["reported_at"]:
                from datetime import date as _date
                result["recent_result"] = {
                    "date": str(fb["reported_at"]),
                    "purpose": f"Quarterly Results ({fb['period_end']})" if fb.get("period_end") else "Quarterly Results",
                    "days_ago": (_date.today() - fb["reported_at"]).days,
                }

        # Top revenue-contributing segment in the latest reported period.
        # Filters out the "Total" / "Unallocated" buckets that some filings
        # include alongside real business segments.
        cur.execute("""
            WITH latest AS (
                SELECT MAX(period_end_date) AS p
                FROM segments_quarterly
                WHERE security_id = %s
            )
            SELECT segment_name,
                   segment_revenue_cr,
                   segment_revenue_pct,
                   period_end_date
            FROM segments_quarterly
            WHERE security_id = %s
              AND period_end_date = (SELECT p FROM latest)
              AND segment_name IS NOT NULL
              AND segment_name NOT ILIKE '%%total%%'
              AND segment_name NOT ILIKE '%%unallocat%%'
              AND segment_name NOT ILIKE '%%eliminat%%'
              AND segment_revenue_cr IS NOT NULL
            ORDER BY segment_revenue_cr DESC
            LIMIT 1
        """, (sid, sid))
        seg = cur.fetchone()
        if seg and seg["segment_name"]:
            result["top_segment"] = {
                "name": _clean_segment_name(seg["segment_name"]),
                "revenue_cr": _fmt(seg["segment_revenue_cr"], 0),
                "revenue_pct": _fmt(seg["segment_revenue_pct"], 1),
                "period": str(seg["period_end_date"]) if seg["period_end_date"] else None,
            }

        return jsonify(result)

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


def _compute_ttm_overlay(sid, cur):
    """Fresh TTM metrics from the last 4 reported quarters.

    Returns a dict of overrides to merge on top of the stored fundamentals_overview
    row. Empty dict if there's no quarterly data we can use. Prefers consolidated
    rows; falls back to standalone for companies that file standalone-only.
    """
    overlay = {}
    for consolidated in (True, False):
        cur.execute("""
            SELECT SUM(eps)                  AS eps_ttm,
                   SUM(revenue_cr)           AS revenue_ttm,
                   SUM(net_profit_cr)        AS net_profit_ttm,
                   SUM(operating_profit_cr)  AS operating_profit_ttm,
                   CASE WHEN SUM(revenue_cr) > 0
                        THEN ROUND(SUM(net_profit_cr) / SUM(revenue_cr) * 100, 2)
                   END AS npm,
                   CASE WHEN SUM(revenue_cr) > 0
                        THEN ROUND(SUM(operating_profit_cr) / SUM(revenue_cr) * 100, 2)
                   END AS opm,
                   MAX(period_end_date) AS last_result_date,
                   COUNT(*) AS quarters_used
            FROM (
                SELECT eps, revenue_cr, net_profit_cr, operating_profit_cr,
                       period_end_date
                FROM financials_quarterly
                WHERE security_id = %s AND is_consolidated = %s
                ORDER BY period_end_date DESC
                LIMIT 4
            ) q
        """, (sid, consolidated))
        ttm = cur.fetchone()
        if ttm and ttm["quarters_used"] and ttm["quarters_used"] > 0:
            # Only emit fields we actually computed — keeps stored values intact
            # when the quarterly table is missing a column for some quarters.
            if ttm["eps_ttm"] is not None:
                overlay["eps_ttm"] = _fmt(ttm["eps_ttm"])
            if ttm["revenue_ttm"] is not None:
                overlay["revenue_ttm_cr"] = _fmt(ttm["revenue_ttm"], 0)
            if ttm["net_profit_ttm"] is not None:
                overlay["net_profit_ttm_cr"] = _fmt(ttm["net_profit_ttm"], 0)
            if ttm["operating_profit_ttm"] is not None:
                overlay["operating_profit_ttm_cr"] = _fmt(ttm["operating_profit_ttm"], 0)
            if ttm["npm"] is not None:
                overlay["net_profit_margin"] = _fmt(ttm["npm"])
            if ttm["opm"] is not None:
                overlay["operating_profit_margin"] = _fmt(ttm["opm"])
            if ttm["last_result_date"]:
                overlay["last_result_date"] = str(ttm["last_result_date"])
            break
    return overlay


def _compute_annual_overlay(sid, stock, cur, current_price=None):
    """Fresh balance-sheet ratios from the latest annual filing.

    Returns a dict of overrides to merge on top of the stored fundamentals_overview
    row. Empty dict if there's no annual data we can use. Prefers consolidated
    rows; falls back to standalone for companies that file standalone-only.

    `current_price` is optional — when present we recompute dividend_yield from
    the fresh dividend_per_share so the page never shows a stale yield.
    """
    overlay = {}
    for consolidated in (True, False):
        cur.execute("""
            SELECT roe, roce, debt_to_equity, current_ratio, interest_coverage,
                   total_equity_cr, dividend_per_share,
                   period_end_date
            FROM financials_annual
            WHERE security_id = %s AND is_consolidated = %s
            ORDER BY period_end_date DESC
            LIMIT 1
        """, (sid, consolidated))
        annual = cur.fetchone()
        if not annual:
            continue
        for k in ("roe", "roce", "debt_to_equity", "current_ratio", "interest_coverage"):
            if annual[k] is not None:
                overlay[k] = _fmt(annual[k])
        if annual.get("dividend_per_share") is not None:
            dps = _fmt(annual["dividend_per_share"])
            overlay["dividend_per_share"] = dps
            # Recompute the yield from the fresh dividend + live price so a
            # newly-declared dividend reflects immediately.
            if current_price is not None and dps is not None and float(current_price) > 0:
                overlay["dividend_yield"] = _fmt(float(dps) / float(current_price) * 100)
        # Book value per share = total_equity (in crores) × 1e7 / shares
        shares = stock.get("shares_outstanding")
        if annual.get("total_equity_cr") and shares:
            bv = float(annual["total_equity_cr"]) * 1e7 / float(shares)
            overlay["book_value_per_share"] = _fmt(bv)
            overlay["book_value"] = _fmt(bv)  # legacy field name some callers read
        break
    return overlay


def _compute_overview_live(sid, stock, cur):
    """Fallback: compute key metrics live when fundamentals_overview is empty."""
    overview = {}

    # TTM from last 4 quarters
    cur.execute("""
        SELECT SUM(eps) as eps_ttm,
               SUM(revenue_cr) as revenue_ttm,
               SUM(net_profit_cr) as net_profit_ttm,
               SUM(operating_profit_cr) as operating_profit_ttm,
               CASE WHEN SUM(revenue_cr) > 0
                    THEN ROUND(SUM(net_profit_cr) / SUM(revenue_cr) * 100, 2) END as npm,
               CASE WHEN SUM(revenue_cr) > 0
                    THEN ROUND(SUM(operating_profit_cr) / SUM(revenue_cr) * 100, 2) END as opm,
               MAX(period_end_date) as last_result_date
        FROM (
            SELECT eps, revenue_cr, net_profit_cr, operating_profit_cr, period_end_date
            FROM financials_quarterly
            WHERE security_id = %s AND is_consolidated = true
            ORDER BY period_end_date DESC
            LIMIT 4
        ) q
    """, (sid,))
    ttm = cur.fetchone()
    if ttm:
        overview["eps_ttm"] = _fmt(ttm["eps_ttm"])
        overview["revenue_ttm_cr"] = _fmt(ttm["revenue_ttm"], 0)
        overview["net_profit_ttm_cr"] = _fmt(ttm["net_profit_ttm"], 0)
        overview["net_profit_margin"] = _fmt(ttm["npm"])
        overview["operating_profit_margin"] = _fmt(ttm["opm"])
        overview["last_result_date"] = str(ttm["last_result_date"]) if ttm["last_result_date"] else None

    # Latest annual for balance sheet ratios
    cur.execute("""
        SELECT roe, roce, debt_to_equity, current_ratio, interest_coverage,
               total_equity_cr, total_borrowings_cr, total_assets_cr,
               dividend_per_share, eps as annual_eps
        FROM financials_annual
        WHERE security_id = %s AND is_consolidated = true
        ORDER BY period_end_date DESC LIMIT 1
    """, (sid,))
    annual = cur.fetchone()
    if annual:
        for k in ("roe", "roce", "debt_to_equity", "current_ratio", "interest_coverage"):
            overview[k] = _fmt(annual[k])
        # Book value per share = total_equity_cr * 1e7 / shares_outstanding
        if annual.get("total_equity_cr") and stock.get("shares_outstanding"):
            bv_per_share = float(annual["total_equity_cr"]) * 1e7 / float(stock["shares_outstanding"])
            overview["book_value"] = _fmt(bv_per_share)
        overview["total_equity_cr"] = _fmt(annual.get("total_equity_cr"), 0)
        if annual.get("dividend_per_share"):
            overview["dividend_per_share"] = _fmt(annual["dividend_per_share"])

    # Latest shareholding
    cur.execute("""
        SELECT promoter_percent, fii_percent, dii_percent, public_percent
        FROM shareholding_quarterly
        WHERE security_id = %s
        ORDER BY period_end_date DESC LIMIT 1
    """, (sid,))
    sh = cur.fetchone()
    if sh:
        overview["promoter_holding_pct"] = _fmt(sh["promoter_percent"])
        overview["fii_pct"] = _fmt(sh["fii_percent"])
        overview["dii_pct"] = _fmt(sh["dii_percent"])
        overview["public_pct"] = _fmt(sh["public_percent"])

    return overview


# ═══════════════════════════════════════════════════
# 2. QUARTERLY RESULTS — P&L by quarter
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/quarterly", methods=["GET"])
@limiter.limit("60 per minute")
def get_quarterly(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        limit = min(int(request.args.get("limit", 20)), 40)
        sid = stock["security_id"]

        # Per-period preference: consolidated when available, standalone fallback.
        # Q4 audited results often land as standalone-only first; consolidated
        # XBRL follows weeks later. A company-wide is_consolidated filter would
        # hide the latest quarter in that window.
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (period_end_date)
                       period, period_end_date,
                       revenue_cr, expenses_cr, operating_profit_cr, opm_percent,
                       other_income_cr, depreciation_cr, interest_cr,
                       profit_before_tax_cr, tax_cr, net_profit_cr,
                       eps, eps_diluted,
                       is_consolidated, has_exceptional_items, exceptional_items_cr,
                       raw_material_cost_cr, employee_cost_cr
                FROM financials_quarterly
                WHERE security_id = %s
                ORDER BY period_end_date DESC, is_consolidated DESC
            ) q
            ORDER BY period_end_date DESC
            LIMIT %s
        """, (sid, limit))

        rows = cur.fetchall()
        return jsonify({
            "symbol": stock["symbol"],
            "company_name": stock["company_name"],
            "quarters": [dict(r) for r in rows],
            "count": len(rows),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# 3. ANNUAL FINANCIALS — P&L + Balance Sheet + Cash Flow
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/annual", methods=["GET"])
@limiter.limit("60 per minute")
def get_annual(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        limit = min(int(request.args.get("limit", 12)), 20)

        # Per-period preference: consolidated when available, standalone fallback.
        # Mirrors the per-period dedupe in get_quarterly so a freshly filed
        # standalone FY26 row isn't hidden by a company-wide is_consolidated
        # filter while its consolidated twin is still pending.
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (period_end_date)
                       fiscal_year, period_end_date,
                       -- P&L
                       revenue_cr, expenses_cr, operating_profit_cr, opm_percent,
                       other_income_cr, depreciation_cr, interest_cr,
                       profit_before_tax_cr, tax_cr, net_profit_cr,
                       eps, eps_diluted, dividend_per_share,
                       -- Balance Sheet — Liabilities
                       equity_capital_cr, reserves_cr, total_equity_cr,
                       total_borrowings_cr, long_term_borrowings_cr, short_term_borrowings_cr,
                       trade_payables_cr, other_current_liabilities_cr, total_current_liabilities_cr,
                       other_liabilities_cr, total_liabilities_cr,
                       -- Balance Sheet — Assets
                       fixed_assets_cr, cwip_cr, goodwill_cr, investments_cr,
                       trade_receivables_cr, inventory_cr, cash_equivalents_cr,
                       other_current_assets_cr, total_current_assets_cr,
                       other_assets_cr, total_assets_cr,
                       -- Cash Flow
                       operating_cashflow_cr, investing_cashflow_cr, financing_cashflow_cr,
                       capex_cr, free_cashflow_cr, net_cashflow_cr,
                       -- Ratios
                       roe, roce, debt_to_equity, current_ratio, interest_coverage,
                       is_consolidated
                FROM financials_annual
                WHERE security_id = %s
                ORDER BY period_end_date DESC, is_consolidated DESC
            ) y
            ORDER BY period_end_date DESC
            LIMIT %s
        """, (stock["security_id"], limit))

        rows = cur.fetchall()
        return jsonify({
            "symbol": stock["symbol"],
            "company_name": stock["company_name"],
            "years": [dict(r) for r in rows],
            "count": len(rows),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# 4. SHAREHOLDING PATTERN — Ownership trends
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/shareholding", methods=["GET"])
@limiter.limit("60 per minute")
def get_shareholding(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        cur.execute("""
            SELECT period, period_end_date,
                   promoter_percent, promoter_pledge_percent,
                   fii_percent, dii_percent, mutual_fund_percent,
                   insurance_percent, government_percent,
                   public_percent, other_percent,
                   total_shares, number_of_shareholders
            FROM shareholding_quarterly
            WHERE security_id = %s
            ORDER BY period_end_date DESC
            LIMIT 40
        """, (stock["security_id"],))

        rows = cur.fetchall()
        return jsonify({
            "symbol": stock["symbol"],
            "shareholding": [dict(r) for r in rows],
            "count": len(rows),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# 5. CORPORATE ACTIONS — Dividends, splits, bonus
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/corporate-actions", methods=["GET"])
@limiter.limit("60 per minute")
def get_corporate_actions(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        action_type = request.args.get("type")

        if action_type:
            cur.execute("""
                SELECT action_type, ex_date, record_date, payment_date,
                       details, dividend_amount, dividend_type,
                       bonus_ratio, split_ratio,
                       face_value_before, face_value_after
                FROM corporate_actions
                WHERE security_id = %s AND action_type = %s
                ORDER BY ex_date DESC NULLS LAST
                LIMIT 50
            """, (stock["security_id"], action_type.upper()))
        else:
            cur.execute("""
                SELECT action_type, ex_date, record_date, payment_date,
                       details, dividend_amount, dividend_type,
                       bonus_ratio, split_ratio,
                       face_value_before, face_value_after
                FROM corporate_actions
                WHERE security_id = %s
                ORDER BY ex_date DESC NULLS LAST
                LIMIT 50
            """, (stock["security_id"],))

        rows = cur.fetchall()
        return jsonify({
            "symbol": stock["symbol"],
            "actions": [dict(r) for r in rows],
            "count": len(rows),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# 6. BUSINESS SEGMENTS — Revenue/profit by segment
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/segments", methods=["GET"])
@limiter.limit("60 per minute")
def get_segments(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        cur.execute("""
            SELECT segment_name, segment_order, period_end_date,
                   segment_revenue_cr, segment_profit_cr,
                   segment_assets_cr, segment_liabilities_cr,
                   segment_revenue_pct, segment_margin_pct,
                   is_consolidated
            FROM segments_quarterly
            WHERE security_id = %s
            ORDER BY period_end_date DESC, segment_order ASC
            LIMIT 100
        """, (stock["security_id"],))

        rows = cur.fetchall()
        cleaned = []
        for r in rows:
            d = dict(r)
            d["segment_name"] = _clean_segment_name(d.get("segment_name"))
            cleaned.append(d)
        return jsonify({
            "symbol": stock["symbol"],
            "segments": cleaned,
            "count": len(cleaned),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# 7. PEERS — Industry peer comparison
# ═══════════════════════════════════════════════════

@fundamentals_bp.route("/api/fundamentals/<symbol>/peers", methods=["GET"])
@limiter.limit("60 per minute")
def get_peers(symbol):
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve(symbol, cur)
        if not stock:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404

        sid = stock["security_id"]

        # Try peers table first
        cur.execute("""
            SELECT p.peer_symbol as symbol, p.industry, p.relevance_rank,
                   su.company_name, su.shares_outstanding
            FROM peers p
            JOIN stock_universe su ON p.peer_security_id = su.security_id
            WHERE p.security_id = %s
            ORDER BY p.relevance_rank ASC
            LIMIT 15
        """, (sid,))
        rows = cur.fetchall()

        if not rows:
            # Fallback: same industry from bse_company_master
            cur.execute("""
                SELECT su.symbol, su.company_name, su.security_id, su.shares_outstanding,
                       bcm.industry, bcm.sector
                FROM bse_company_master bcm
                JOIN stock_universe su ON bcm.security_id = su.security_id
                WHERE bcm.industry = (
                    SELECT industry FROM bse_company_master WHERE security_id = %s LIMIT 1
                )
                AND bcm.security_id != %s
                AND su.is_active = true
                LIMIT 15
            """, (sid, sid))
            rows = cur.fetchall()

        # Get current industry
        cur.execute("""
            SELECT sector, industry FROM bse_company_master WHERE security_id = %s LIMIT 1
        """, (sid,))
        ind = cur.fetchone()

        return jsonify({
            "symbol": stock["symbol"],
            "sector": ind["sector"] if ind else None,
            "industry": ind["industry"] if ind else None,
            "peers": [dict(r) for r in rows],
            "count": len(rows),
        })

    except Exception as e:
        print(f"[fundamentals] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
