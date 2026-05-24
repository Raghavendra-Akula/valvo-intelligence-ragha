"""
Single-stock dossier builder for Deep Research.

Mirrors the per-stock data assembly in past_winners/export but for a single
symbol over an arbitrary window. The dossier becomes the LLM's only source
of truth — every fact in the generated report should be traceable to a
field in the dict returned here.

Sections collected:
  - identity              symbol, company, sector, industry, isin, listing
  - move_metrics          start/end close, ROC, max ROC, 52w high context
  - benchmark             smallcap-100 ROC + alpha (pp)
  - cohort                sub-sector / theme / wave cohort ROC
  - peers                 top 3 peers + their ROC in the same window
  - valuation             TTM EPS at start vs end → P/E re-rating
  - business_profile      current snapshot from fundamentals_overview
  - business_as_of        as-of-window snapshot from financials_annual + shareholding
  - valvo_score           VALVO 12-param score at setup (day -1) and at end
  - segments              latest-period revenue mix
  - quarters              last 12 quarters of fundamentals (revenue, OPM, EPS)
  - annual                last 5 years P&L + balance sheet + cash flow
  - shareholding          last 8 quarters of promoter / FII / DII / pledge
  - in_window_results     any quarterly result that fell in the window
  - filings               in-window filings (top 25 by date) with descriptions
  - corporate_actions     in-window splits/bonuses/dividends
  - sources               BSE / NSE / screener / website lookup URLs
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Any

from database.database import get_db
from services.historical_scoring import compute_historical_score


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_security_id(cur, symbol: str) -> dict | None:
    """Map a user-typed symbol to a security_id row from stock_universe."""
    cur.execute("""
        SELECT security_id, symbol, company_name, sector, industry, isin
          FROM stock_universe
         WHERE upper(symbol) = upper(%s)
         LIMIT 1
    """, (symbol,))
    r = cur.fetchone()
    if r:
        return dict(r)
    cur.execute("""
        SELECT security_id, symbol, company_name, sector, industry, isin
          FROM stock_universe
         WHERE symbol ILIKE %s OR company_name ILIKE %s
         ORDER BY is_active DESC NULLS LAST, length(symbol) ASC
         LIMIT 1
    """, (f"{symbol}%", f"%{symbol}%"))
    r = cur.fetchone()
    return dict(r) if r else None


def _ttm_eps_as_of(quarters: list[dict], as_of: date) -> float | None:
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


def _pct(value: float | None, base: float | None) -> float | None:
    if value is None or base is None or base == 0:
        return None
    try:
        return round((value - base) / base * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════

def build_dossier(
    *,
    symbol: str,
    from_date: str | None = None,
    to_date: str | None = None,
    mode: str = "retrospective",
) -> dict[str, Any]:
    """Assemble a single-stock dossier.

    For 'retrospective' mode the caller supplies from_date/to_date (the
    move window). For 'forward' mode the window defaults to the last
    180 days so the model has recent context to reason about what's
    coming next.

    Raises ValueError if the symbol can't be resolved.
    """
    today = date.today()
    if not from_date or not to_date:
        to_dt = today - timedelta(days=1)
        from_dt = to_dt - timedelta(days=180)
        from_date = from_dt.isoformat()
        to_date = to_dt.isoformat()

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"Invalid date format: {e}")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '60000'")

        identity = _resolve_security_id(cur, symbol)
        if not identity:
            raise ValueError(f"Symbol '{symbol}' not found in stock_universe")
        sid = identity["security_id"]

        dossier: dict[str, Any] = {
            "mode": mode,
            "window": {"from": from_date, "to": to_date},
            "identity": identity,
        }

        # ── Move metrics over the window ──
        cur.execute("""
            WITH win AS (
                SELECT MIN(date) AS fd, MAX(date) AS ld,
                       MAX(close) AS max_close, COUNT(*) AS trading_days,
                       ROUND((AVG(volume::float8 * close) / 1e7)::numeric, 2) AS avg_turnover_cr
                  FROM candles_daily
                 WHERE security_id = %s
                   AND date >= %s AND date <= %s
                   AND date < CURRENT_DATE
            )
            SELECT w.fd, w.ld, w.max_close, w.trading_days, w.avg_turnover_cr,
                   fc.close AS start_close, lc.close AS end_close
              FROM win w
              LEFT JOIN candles_daily fc ON fc.security_id = %s AND fc.date = w.fd
              LEFT JOIN candles_daily lc ON lc.security_id = %s AND lc.date = w.ld
        """, (sid, from_date, to_date, sid, sid))
        m = cur.fetchone()
        if m and m["start_close"] and m["end_close"]:
            dossier["move_metrics"] = {
                "start_date": str(m["fd"]),
                "end_date": str(m["ld"]),
                "start_close": float(m["start_close"]),
                "end_close": float(m["end_close"]),
                "max_close": float(m["max_close"]) if m["max_close"] is not None else None,
                "trading_days": int(m["trading_days"] or 0),
                "avg_turnover_cr": float(m["avg_turnover_cr"]) if m["avg_turnover_cr"] is not None else None,
                "roc_pct": _pct(float(m["end_close"]), float(m["start_close"])),
                "max_roc_pct": _pct(float(m["max_close"]) if m["max_close"] else None, float(m["start_close"])),
            }
        else:
            dossier["move_metrics"] = None

        # ── 52-week high (relative to to_date) ──
        cur.execute("""
            SELECT MAX(close) AS high_52w
              FROM candles_daily
             WHERE security_id = %s
               AND date > (%s::date - INTERVAL '365 days')
               AND date <= %s::date
        """, (sid, to_date, to_date))
        h = cur.fetchone()
        if h and h["high_52w"] is not None:
            dossier["high_52w"] = float(h["high_52w"])
            if dossier.get("move_metrics") and dossier["move_metrics"].get("end_close"):
                dossier["pct_from_52w_high"] = _pct(
                    dossier["move_metrics"]["end_close"], dossier["high_52w"],
                )

        # ── Smallcap 100 benchmark ROC ──
        try:
            cur.execute("""
                WITH idx AS (
                    SELECT (ARRAY_AGG(close ORDER BY date ASC))[1]  AS sc,
                           (ARRAY_AGG(close ORDER BY date DESC))[1] AS ec
                      FROM candles_indices
                     WHERE symbol = 'NIFTY SMALLCAP 100'
                       AND date >= %s AND date <= %s
                )
                SELECT ROUND(((ec - sc) / NULLIF(sc, 0) * 100)::numeric, 2) AS roc
                  FROM idx WHERE sc IS NOT NULL
            """, (from_date, to_date))
            br = cur.fetchone()
            if br and br["roc"] is not None:
                dossier["benchmark"] = {"smallcap_100_roc_pct": float(br["roc"])}
                if dossier.get("move_metrics") and dossier["move_metrics"].get("roc_pct") is not None:
                    dossier["benchmark"]["alpha_pp"] = round(
                        dossier["move_metrics"]["roc_pct"] - float(br["roc"]), 2,
                    )
        except Exception as e:
            print(f"[deep_research/dossier] benchmark error: {e}")

        # ── Sector / sub-sector / theme classification ──
        try:
            cur.execute("""
                SELECT sector, sub_sector_name, primary_theme_name, primary_wave_name
                  FROM v_stock_classification_v2
                 WHERE security_id = %s
                 LIMIT 1
            """, (sid,))
            c = cur.fetchone()
            if c:
                dossier["classification"] = dict(c)
        except Exception:
            pass

        # ── Cohort ROC (sub-sector + theme + wave, all three) ──
        # Why three? Sub-sector is granular (e.g. "Defence Electronics"), theme
        # is investment narrative (e.g. "Atmanirbhar Bharat"), wave is the macro
        # rotation bucket. A move can be sub-sector-driven, theme-driven, or
        # wave-driven — telling them apart matters for catch/late/miss verdicts.
        klass = dossier.get("classification") or {}
        cohorts: dict[str, Any] = {}
        for label, col, val in (
            ("sub_sector", "sub_sector_name", klass.get("sub_sector_name")),
            ("primary_theme", "primary_theme_name", klass.get("primary_theme_name")),
            ("primary_wave", "primary_wave_name", klass.get("primary_wave_name")),
        ):
            if not val:
                continue
            try:
                cur.execute(f"""
                    WITH cohort AS (
                        SELECT v.security_id
                          FROM v_stock_classification_v2 v
                         WHERE v.{col} = %s
                    ),
                    agg AS (
                        SELECT c.security_id, MIN(cd.date) AS fd, MAX(cd.date) AS ld
                          FROM cohort c
                          JOIN candles_daily cd ON cd.security_id = c.security_id
                         WHERE cd.date >= %s AND cd.date <= %s
                           AND cd.date < CURRENT_DATE
                      GROUP BY c.security_id HAVING COUNT(*) >= 5
                    ),
                    rocs AS (
                        SELECT a.security_id,
                               ((lc.close - fc.close) / NULLIF(fc.close, 0) * 100) AS roc
                          FROM agg a
                          JOIN candles_daily fc ON a.security_id = fc.security_id AND a.fd = fc.date
                          JOIN candles_daily lc ON a.security_id = lc.security_id AND a.ld = lc.date
                    )
                    SELECT ROUND(AVG(roc)::numeric, 2) AS avg_roc,
                           ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY roc))::numeric, 2) AS median_roc,
                           ROUND(MAX(roc)::numeric, 2) AS top_roc,
                           ROUND(MIN(roc)::numeric, 2) AS bot_roc,
                           COUNT(*) AS n
                      FROM rocs WHERE roc IS NOT NULL
                """, (val, from_date, to_date))
                r = cur.fetchone()
                if r and r["avg_roc"] is not None:
                    # Compute the stock's percentile within its own cohort
                    pct_rank = None
                    own_roc = (dossier.get("move_metrics") or {}).get("roc_pct")
                    if own_roc is not None:
                        cur.execute(f"""
                            WITH cohort AS (
                                SELECT v.security_id FROM v_stock_classification_v2 v
                                 WHERE v.{col} = %s
                            ),
                            agg AS (
                                SELECT c.security_id, MIN(cd.date) fd, MAX(cd.date) ld
                                  FROM cohort c
                                  JOIN candles_daily cd ON cd.security_id = c.security_id
                                 WHERE cd.date >= %s AND cd.date <= %s
                                   AND cd.date < CURRENT_DATE
                              GROUP BY c.security_id HAVING COUNT(*) >= 5
                            ),
                            rocs AS (
                                SELECT a.security_id,
                                       ((lc.close - fc.close) / NULLIF(fc.close, 0) * 100) AS roc
                                  FROM agg a
                                  JOIN candles_daily fc ON a.security_id = fc.security_id AND a.fd = fc.date
                                  JOIN candles_daily lc ON a.security_id = lc.security_id AND a.ld = lc.date
                            )
                            SELECT
                                ROUND((100.0 * COUNT(*) FILTER (WHERE roc <= %s) / NULLIF(COUNT(*), 0))::numeric, 1) AS pct
                              FROM rocs WHERE roc IS NOT NULL
                        """, (val, from_date, to_date, own_roc))
                        pr = cur.fetchone()
                        if pr and pr["pct"] is not None:
                            pct_rank = float(pr["pct"])

                    cohorts[label] = {
                        "name": val,
                        "avg_roc_pct": float(r["avg_roc"]),
                        "median_roc_pct": float(r["median_roc"]) if r["median_roc"] is not None else None,
                        "top_roc_pct": float(r["top_roc"]) if r["top_roc"] is not None else None,
                        "bottom_roc_pct": float(r["bot_roc"]) if r["bot_roc"] is not None else None,
                        "n": int(r["n"] or 0),
                        "stock_percentile_in_cohort": pct_rank,
                        "stock_alpha_vs_cohort_pp": (
                            round(own_roc - float(r["avg_roc"]), 2)
                            if own_roc is not None else None
                        ),
                    }
            except Exception as e:
                print(f"[deep_research/dossier] cohort {label} error: {e}")
        if cohorts:
            dossier["cohorts"] = cohorts
            # Back-compat: keep flat sub_sector cohort under old key
            if "sub_sector" in cohorts:
                dossier["cohort"] = {
                    "sub_sector": cohorts["sub_sector"]["name"],
                    "avg_roc_pct": cohorts["sub_sector"]["avg_roc_pct"],
                    "n": cohorts["sub_sector"]["n"],
                }

        # ── Top 3 peers + their ROC during window ──
        peers = []
        try:
            cur.execute("""
                WITH peer_list AS (
                    SELECT peer_security_id, peer_symbol, relevance_rank
                      FROM peers
                     WHERE security_id = %s
                       AND COALESCE(relevance_rank, 99) <= 3
                ),
                peer_agg AS (
                    SELECT pl.peer_security_id, pl.peer_symbol, pl.relevance_rank,
                           MIN(cd.date) AS fd, MAX(cd.date) AS ld
                      FROM peer_list pl
                      JOIN candles_daily cd ON cd.security_id = pl.peer_security_id
                     WHERE cd.date >= %s AND cd.date <= %s
                       AND cd.date < CURRENT_DATE
                  GROUP BY pl.peer_security_id, pl.peer_symbol, pl.relevance_rank
                    HAVING COUNT(*) >= 5
                )
                SELECT pa.peer_symbol, pa.relevance_rank,
                       ROUND(((lc.close - fc.close) / NULLIF(fc.close, 0) * 100)::numeric, 2) AS peer_roc
                  FROM peer_agg pa
                  JOIN candles_daily fc ON pa.peer_security_id = fc.security_id AND pa.fd = fc.date
                  JOIN candles_daily lc ON pa.peer_security_id = lc.security_id AND pa.ld = lc.date
              ORDER BY pa.relevance_rank
            """, (sid, from_date, to_date))
            for r in cur.fetchall():
                peers.append({
                    "symbol": r["peer_symbol"],
                    "rank": int(r["relevance_rank"]) if r["relevance_rank"] is not None else None,
                    "roc_pct": float(r["peer_roc"]) if r["peer_roc"] is not None else None,
                })
        except Exception as e:
            print(f"[deep_research/dossier] peers error: {e}")
        dossier["peers"] = peers

        # ── fundamentals_overview (CURRENT-day snapshot — useful for forward mode) ──
        try:
            cur.execute("""
                SELECT isin, bse_code, nse_code, website, listing_date, about,
                       industry, promoter_holding_pct, debt_to_equity,
                       sales_growth_3yr_cagr, profit_growth_ttm,
                       roce, week_52_high
                  FROM fundamentals_overview
                 WHERE security_id = %s
                 LIMIT 1
            """, (sid,))
            f = cur.fetchone()
            if f:
                fund = dict(f)
                if fund.get("listing_date"):
                    fund["listing_date"] = str(fund["listing_date"])
                dossier["business_profile"] = fund
        except Exception as e:
            print(f"[deep_research/dossier] fundamentals error: {e}")

        # ── AS-OF-DATE business snapshot (what fundamentals looked like at to_date) ──
        # The business_profile block above is "today" — fine for forward mode but
        # misleading when we're researching a 2-year-old move. Here we pull the
        # most-recent annual + shareholding ENDING ON OR BEFORE to_date.
        as_of_block: dict[str, Any] = {"as_of_window_end": to_date}
        try:
            cur.execute("""
                SELECT fiscal_year, period_end_date, is_consolidated,
                       roe, roce, debt_to_equity, current_ratio, interest_coverage,
                       total_borrowings_cr, cash_equivalents_cr, free_cashflow_cr,
                       capex_cr, dividend_per_share, eps,
                       revenue_cr, operating_profit_cr, opm_percent, net_profit_cr
                  FROM financials_annual
                 WHERE security_id = %s
                   AND period_end_date <= %s::date
              ORDER BY period_end_date DESC, is_consolidated DESC NULLS LAST
                 LIMIT 1
            """, (sid, to_date))
            ann = cur.fetchone()
            if ann:
                row = dict(ann)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                row.pop("is_consolidated", None)
                for k in (
                    "roe", "roce", "debt_to_equity", "current_ratio", "interest_coverage",
                    "total_borrowings_cr", "cash_equivalents_cr", "free_cashflow_cr",
                    "capex_cr", "dividend_per_share", "eps",
                    "revenue_cr", "operating_profit_cr", "opm_percent", "net_profit_cr",
                ):
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                as_of_block["latest_annual"] = row
        except Exception as e:
            print(f"[deep_research/dossier] as-of annual error: {e}")

        try:
            cur.execute("""
                SELECT period, period_end_date,
                       promoter_percent, promoter_pledge_percent,
                       fii_percent, dii_percent, mutual_fund_percent,
                       insurance_percent, government_percent, public_percent
                  FROM shareholding_quarterly
                 WHERE security_id = %s
                   AND period_end_date <= %s::date
              ORDER BY period_end_date DESC
                 LIMIT 1
            """, (sid, to_date))
            sh = cur.fetchone()
            if sh:
                row = dict(sh)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                for k in (
                    "promoter_percent", "promoter_pledge_percent",
                    "fii_percent", "dii_percent", "mutual_fund_percent",
                    "insurance_percent", "government_percent", "public_percent",
                ):
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                as_of_block["shareholding"] = row
        except Exception as e:
            print(f"[deep_research/dossier] as-of shareholding error: {e}")

        if len(as_of_block) > 1:
            dossier["business_as_of"] = as_of_block

        # ── VALVO scoring trace ──
        # Compute the score at SETUP (day before window starts) and at END
        # (last day of window) so the analyst can read the delta:
        #
        #     setup score 5.4 → end score 8.1 = the system would have caught
        #     this on day -1 if the operator had been looking.
        #
        # In forward mode the "setup" is the start of the 180-day lookback and
        # "end" is today — the end score is what the system says about the
        # stock right now.
        try:
            move_window_arg = (from_date, to_date)
            setup_anchor = (from_dt - timedelta(days=1)).isoformat()
            score_setup = compute_historical_score(
                security_id=sid,
                symbol=identity.get("symbol") or symbol,
                as_of=setup_anchor,
                move_window=None,           # at setup, the move hasn't happened
                conn=conn,
            )
            score_end = compute_historical_score(
                security_id=sid,
                symbol=identity.get("symbol") or symbol,
                as_of=to_date,
                move_window=move_window_arg,
                conn=conn,
            )
            valvo: dict[str, Any] = {}
            if score_setup:
                valvo["setup"] = score_setup
            if score_end:
                valvo["end"] = score_end
            if score_setup and score_end:
                valvo["delta"] = {
                    "final_score": round(
                        (score_end["final_score"] or 0) - (score_setup["final_score"] or 0), 2,
                    ),
                    "raw_composite": round(
                        (score_end["raw_composite"] or 0) - (score_setup["raw_composite"] or 0), 2,
                    ),
                    "rating_change": (
                        f"{score_setup['rating']} → {score_end['rating']}"
                        if score_setup.get("rating") != score_end.get("rating") else None
                    ),
                }
            if valvo:
                dossier["valvo_score"] = valvo
        except Exception as e:
            print(f"[deep_research/dossier] valvo score error: {e}")
            import traceback as _tb; _tb.print_exc()

        # ── Last 12 quarters of fundamentals (richer columns) ──
        quarters: list[dict] = []
        try:
            cur.execute("""
                WITH ranked AS (
                    SELECT period, period_end_date, filing_date,
                           revenue_cr, expenses_cr,
                           operating_profit_cr, opm_percent,
                           other_income_cr, depreciation_cr, interest_cr,
                           profit_before_tax_cr, tax_cr, net_profit_cr,
                           eps, is_consolidated,
                           has_exceptional_items, exceptional_items_cr,
                           raw_material_cost_cr, employee_cost_cr,
                           ROW_NUMBER() OVER (
                               PARTITION BY is_consolidated
                               ORDER BY period_end_date DESC
                           ) AS rn
                      FROM financials_quarterly
                     WHERE security_id = %s
                ),
                preferred AS (
                    SELECT DISTINCT ON (1) 1 AS k, is_consolidated
                      FROM ranked
                     ORDER BY 1, is_consolidated DESC NULLS LAST
                )
                SELECT r.period, r.period_end_date, r.filing_date,
                       r.revenue_cr, r.expenses_cr,
                       r.operating_profit_cr, r.opm_percent,
                       r.other_income_cr, r.depreciation_cr, r.interest_cr,
                       r.profit_before_tax_cr, r.tax_cr, r.net_profit_cr,
                       r.eps,
                       r.has_exceptional_items, r.exceptional_items_cr,
                       r.raw_material_cost_cr, r.employee_cost_cr
                  FROM ranked r
                  JOIN preferred p ON p.is_consolidated IS NOT DISTINCT FROM r.is_consolidated
                 WHERE r.rn <= 12
              ORDER BY r.period_end_date DESC
            """, (sid,))
            float_cols = (
                "revenue_cr", "expenses_cr", "operating_profit_cr", "opm_percent",
                "other_income_cr", "depreciation_cr", "interest_cr",
                "profit_before_tax_cr", "tax_cr", "net_profit_cr", "eps",
                "exceptional_items_cr", "raw_material_cost_cr", "employee_cost_cr",
            )
            for r in cur.fetchall():
                row = dict(r)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                if row.get("filing_date"):
                    row["filing_date"] = str(row["filing_date"])
                for k in float_cols:
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                quarters.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] quarters error: {e}")
        dossier["quarters"] = quarters

        # ── Last 5 fiscal years: P&L + balance sheet + cash flow ──
        annual: list[dict] = []
        try:
            cur.execute("""
                WITH ranked AS (
                    SELECT fiscal_year, period_end_date, is_consolidated,
                           revenue_cr, operating_profit_cr, opm_percent, net_profit_cr,
                           eps, dividend_per_share,
                           total_equity_cr, total_borrowings_cr,
                           long_term_borrowings_cr, short_term_borrowings_cr,
                           cash_equivalents_cr, investments_cr,
                           trade_receivables_cr, inventory_cr,
                           total_current_assets_cr, total_current_liabilities_cr,
                           total_assets_cr,
                           operating_cashflow_cr, investing_cashflow_cr,
                           financing_cashflow_cr, capex_cr, free_cashflow_cr,
                           roe, roce, debt_to_equity, current_ratio, interest_coverage,
                           ROW_NUMBER() OVER (
                               PARTITION BY is_consolidated
                               ORDER BY period_end_date DESC
                           ) AS rn
                      FROM financials_annual
                     WHERE security_id = %s
                ),
                preferred AS (
                    SELECT DISTINCT ON (1) 1 AS k, is_consolidated
                      FROM ranked
                     ORDER BY 1, is_consolidated DESC NULLS LAST
                )
                SELECT r.*
                  FROM ranked r
                  JOIN preferred p ON p.is_consolidated IS NOT DISTINCT FROM r.is_consolidated
                 WHERE r.rn <= 5
              ORDER BY r.period_end_date DESC
            """, (sid,))
            annual_floats = (
                "revenue_cr", "operating_profit_cr", "opm_percent", "net_profit_cr",
                "eps", "dividend_per_share",
                "total_equity_cr", "total_borrowings_cr",
                "long_term_borrowings_cr", "short_term_borrowings_cr",
                "cash_equivalents_cr", "investments_cr",
                "trade_receivables_cr", "inventory_cr",
                "total_current_assets_cr", "total_current_liabilities_cr",
                "total_assets_cr",
                "operating_cashflow_cr", "investing_cashflow_cr",
                "financing_cashflow_cr", "capex_cr", "free_cashflow_cr",
                "roe", "roce", "debt_to_equity", "current_ratio", "interest_coverage",
            )
            for r in cur.fetchall():
                row = dict(r)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                row.pop("rn", None)
                row.pop("is_consolidated", None)
                for k in annual_floats:
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                annual.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] annual error: {e}")
        dossier["annual"] = annual

        # ── Shareholding pattern history (last 8 quarters) ──
        shareholding: list[dict] = []
        try:
            cur.execute("""
                SELECT period, period_end_date,
                       promoter_percent, promoter_pledge_percent,
                       fii_percent, dii_percent, mutual_fund_percent,
                       insurance_percent, government_percent,
                       public_percent
                  FROM shareholding_quarterly
                 WHERE security_id = %s
              ORDER BY period_end_date DESC
                 LIMIT 8
            """, (sid,))
            sh_floats = (
                "promoter_percent", "promoter_pledge_percent",
                "fii_percent", "dii_percent", "mutual_fund_percent",
                "insurance_percent", "government_percent", "public_percent",
            )
            for r in cur.fetchall():
                row = dict(r)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                for k in sh_floats:
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                shareholding.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] shareholding error: {e}")
        dossier["shareholding"] = shareholding

        # ── In-window result (any quarterly result whose filing fell in window) ──
        in_window = [q for q in quarters if q.get("filing_date")
                     and from_date <= q["filing_date"] <= to_date]
        dossier["in_window_results"] = in_window

        # ── TTM-based valuation (P/E re-rating) ──
        if dossier.get("move_metrics"):
            ttm_start = _ttm_eps_as_of(
                [{**q, "period_end_date": datetime.strptime(q["period_end_date"], "%Y-%m-%d").date()}
                 for q in quarters if q.get("period_end_date")],
                from_dt,
            )
            ttm_end = _ttm_eps_as_of(
                [{**q, "period_end_date": datetime.strptime(q["period_end_date"], "%Y-%m-%d").date()}
                 for q in quarters if q.get("period_end_date")],
                to_dt,
            )
            pe_start = (
                round(dossier["move_metrics"]["start_close"] / ttm_start, 1)
                if ttm_start and ttm_start > 0 else None
            )
            pe_end = (
                round(dossier["move_metrics"]["end_close"] / ttm_end, 1)
                if ttm_end and ttm_end > 0 else None
            )
            re_rating = _pct(pe_end, pe_start) if pe_start and pe_end else None
            dossier["valuation"] = {
                "ttm_eps_start": ttm_start,
                "ttm_eps_end": ttm_end,
                "pe_start": pe_start,
                "pe_end": pe_end,
                "pe_rerating_pct": re_rating,
            }

        # ── In-window filings (top 25 by date, with description text) ──
        filings = []
        try:
            cur.execute("""
                SELECT filing_type, filing_date, description, period
                  FROM filings
                 WHERE security_id = %s
                   AND filing_date BETWEEN %s::date AND %s::date
              ORDER BY filing_date DESC
                 LIMIT 25
            """, (sid, from_date, to_date))
            for r in cur.fetchall():
                row = dict(r)
                if row.get("filing_date"):
                    row["filing_date"] = str(row["filing_date"])
                if row.get("description"):
                    row["description"] = str(row["description"])[:600]
                filings.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] filings error: {e}")
        dossier["filings"] = filings

        # ── In-window corporate actions ──
        actions = []
        try:
            cur.execute("""
                SELECT action_type, ex_date, action_date, details,
                       dividend_amount, dividend_type, bonus_ratio, split_ratio
                  FROM corporate_actions
                 WHERE security_id = %s
                   AND (
                        (ex_date     BETWEEN %s::date AND %s::date)
                     OR (action_date BETWEEN %s::date AND %s::date)
                   )
              ORDER BY COALESCE(ex_date, action_date) DESC
            """, (sid, from_date, to_date, from_date, to_date))
            for r in cur.fetchall():
                row = dict(r)
                for k in ("ex_date", "action_date"):
                    if row.get(k):
                        row[k] = str(row[k])
                actions.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] corp actions error: {e}")
        dossier["corporate_actions"] = actions

        # ── Latest revenue mix (top segments) ──
        segments = []
        try:
            cur.execute("""
                WITH ranked AS (
                    SELECT period_end_date, segment_name,
                           segment_revenue_cr, segment_revenue_pct,
                           DENSE_RANK() OVER (ORDER BY period_end_date DESC) AS period_rank
                      FROM segments_quarterly
                     WHERE security_id = %s
                       AND segment_name IS NOT NULL
                       AND segment_name <> ''
                )
                SELECT period_end_date, segment_name,
                       segment_revenue_cr, segment_revenue_pct
                  FROM ranked
                 WHERE period_rank = 1
              ORDER BY segment_revenue_cr DESC NULLS LAST
                 LIMIT 6
            """, (sid,))
            for r in cur.fetchall():
                row = dict(r)
                if row.get("period_end_date"):
                    row["period_end_date"] = str(row["period_end_date"])
                for k in ("segment_revenue_cr", "segment_revenue_pct"):
                    if row.get(k) is not None:
                        try:
                            row[k] = float(row[k])
                        except (TypeError, ValueError):
                            pass
                segments.append(row)
        except Exception as e:
            print(f"[deep_research/dossier] segments error: {e}")
        dossier["segments"] = segments

        # ── External lookup hooks ──
        bp = dossier.get("business_profile") or {}
        bse_code = bp.get("bse_code")
        nse_code = bp.get("nse_code") or identity.get("symbol")
        dossier["sources"] = {
            "bse_announcements": (
                f"https://www.bseindia.com/stock-share-price/_/_/{bse_code}/" if bse_code else None
            ),
            "screener": (
                f"https://www.screener.in/company/{nse_code}/consolidated/" if nse_code else None
            ),
            "website": bp.get("website"),
        }

        return dossier
    finally:
        conn.close()
